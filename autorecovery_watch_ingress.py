"""WATCH-only runtime ingress for one canonical JAYTEC autorecovery cycle."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from typing import Any, Callable, Mapping

from autorecovery_components import (
    CALLABLE_ROUTE_ID,
    EXPECTED_REPO,
    JsonLogRecoveryNotifier,
    ManusLiteHealthProbe,
    ManusLiteRecoveryInvoker,
    ObservedRefsCheckpointVerifier,
    assignment_owner_redirect_handoff_id,
    master_gate_handoff_id,
    master_gate_result_has_receipt,
)
from autorecovery_runtime import runtime_status, schema_probe
from manus_governance import validate_specialist_request
from watch_specialist_broker import (
    ALLOWED_MANUS_MODEL_SPECIALISTS,
    SpecialistBrokerError,
    normalize_manus_model_request,
    safe_result_summary,
)
from autorecovery_supervisor import (
    AssignmentCheckpoint,
    AutoRecoverySupervisor,
    PostgresAssignmentStore,
    StopReason,
    WorkerKind,
)

FORGE_TASK_ID = "FORGE-GENESIS-ACTIVATION-001"
MAX_REQUEST_REFS = 128
MAX_BROKER_CONTEXT_BYTES = 4500
MAX_BROKER_REQUESTS = 2
MAX_HELP_REQUESTS = 5
MASTER_GATE_SCHEMA = "FORGE_MASTER_GATE_DIRECTIVE_V1"
MASTER_GATE_FIELDS = frozenset({
    "schema_version",
    "gate_id",
    "phase",
    "title",
    "status",
    "depends_on",
    "evidence",
    "graph_sha256",
    "checkpoint_number",
})
ASSIGNMENT_OWNER_DIRECTIVE_SCHEMA = "JAYTEC_ASSIGNMENT_CONTROLLER_DIRECTIVE_V1"
ASSIGNMENT_OWNER_DIRECTIVE_FIELDS = frozenset({
    "schema_version",
    "task_id",
    "assignment_owner",
    "gate_id",
    "checkpoint_number",
    "review_id",
    "request_id",
    "jaytec_review_id",
    "decision",
    "direction",
    "result_receipt",
    "result_sha256",
    "manifest_sha256",
    "worker_id",
    "fencing_token",
})

# WATCH cadence is 15 minutes. Require more than two missed cadence windows
# before classifying a RUNNING callable worker as lost, so one transient
# provider-status failure cannot consume a recovery fence/attempt.
WATCH_HEARTBEAT_TIMEOUT_SECONDS = 35 * 60
WATCH_HEALTH_REFRESH_TIMEOUT_SECONDS = 30

# One-time compatibility bridge from the original canonical Forge assignment
# checkpoint namespace into the evidence-gated master-gate namespace. This is
# deliberately exact-state scoped; every other checkpoint gap still fails closed.
LEGACY_FORGE_CHECKPOINT_NUMBER = 1
LEGACY_FORGE_PHASE = "pre-activation software convergence and evidence hardening"
LEGACY_FORGE_OBJECTIVE_PREFIX = (
    "Continue Forge/Genesis preparation from the exact saved state."
)
FIRST_MASTER_GATE_ID = "G03"
FIRST_MASTER_GATE_CHECKPOINT = 103

# One-time exact-state repair for the live G03 recovery-budget accounting defect.
# Attempts 1-3 all failed before a replacement provider worker was accepted; the
# final failure was the deterministic local preflight ceiling. Keep fence 11
# monotonic, refund exactly one miscounted attempt, then let the fixed runtime use
# the legitimate third provider-recovery slot at fence 12.
LOCAL_PREFLIGHT_REFUND_CHECKPOINT = 103
LOCAL_PREFLIGHT_REFUND_FENCE = 11
LOCAL_PREFLIGHT_REFUND_ATTEMPTS = 3
LOCAL_PREFLIGHT_REFUND_ERROR = (
    "RECOVERY_INVOCATION_REJECTED:MANUS_RECOVERY_PREFLIGHT_MESSAGE_TOO_LARGE"
)

BROKER_READ_OPERATIONS = frozenset({
    "read_file",
    "list_path",
    "read_issue",
    "read_pr",
    "read_workflow_runs",
})
BROKER_WRITE_OPERATIONS = frozenset({
    "create_branch",
    "write_file",
    "create_pr",
})
BROKER_OPERATIONS = BROKER_READ_OPERATIONS | BROKER_WRITE_OPERATIONS
_BROKER_WORKER_BRANCH = re.compile(r"^watch/worker-[0-9]+-[a-z0-9._-]{1,80}$")
_BROKER_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/@+-][A-Za-z0-9._/@+ -]{0,299}$")
_BROKER_BLOCKED_PATH = re.compile(
    r"(^|/)(?:\.env(?:\.|$)|.*(?:secret|credential|private[_-]?key).*|"
    r"id_rsa(?:\.|$)|.*\.(?:pem|key|p12|pfx))",
    re.I,
)
_BROKER_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|credential|token)",
    re.I,
)
_BROKER_SECRET_VALUE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)"
)


class WatchIngressError(RuntimeError):
    pass


def _bool_env(env: Mapping[str, str], name: str) -> bool:
    value = str(env.get(name, "0")).strip()
    if value == "1":
        return True
    if value in {"", "0"}:
        return False
    raise WatchIngressError(name + "_INVALID_BOOLEAN")


def _observed_refs(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise WatchIngressError("OBSERVED_REFS_REQUIRED")
    if len(value) > MAX_REQUEST_REFS:
        raise WatchIngressError("OBSERVED_REFS_TOO_MANY")
    refs: dict[str, str] = {}
    for key, raw in value.items():
        ref = str(key or "").strip()
        sha = str(raw or "").strip().lower()
        if not ref or len(ref) > 300:
            raise WatchIngressError("OBSERVED_REF_NAME_INVALID")
        refs[ref] = sha
    return refs



def _master_gate_context(
    value: Any,
    checkpoint: AssignmentCheckpoint,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WatchIngressError("MASTER_GATE_REQUIRED")
    gate = dict(value)
    if set(gate) != MASTER_GATE_FIELDS:
        raise WatchIngressError("MASTER_GATE_FIELDS_INVALID")
    if gate.get("schema_version") != MASTER_GATE_SCHEMA:
        raise WatchIngressError("MASTER_GATE_SCHEMA_INVALID")
    gate_id = str(gate.get("gate_id") or "").strip()
    if not re.fullmatch(r"G[0-9]{2,4}", gate_id):
        raise WatchIngressError("MASTER_GATE_ID_INVALID")
    number = int(gate.get("checkpoint_number") or 0)
    if number < 1 or number > 1000000:
        raise WatchIngressError("MASTER_GATE_CHECKPOINT_NUMBER_INVALID")
    if number != checkpoint.checkpoint_number:
        raise WatchIngressError("MASTER_GATE_CHECKPOINT_MISMATCH")
    phase = str(gate.get("phase") or "").strip()
    title = str(gate.get("title") or "").strip()
    status = str(gate.get("status") or "").strip()
    if not phase or len(phase) > 80:
        raise WatchIngressError("MASTER_GATE_PHASE_INVALID")
    if not title or len(title) > 240:
        raise WatchIngressError("MASTER_GATE_TITLE_INVALID")
    if status not in {"IN_PROGRESS", "NOT_STARTED"}:
        raise WatchIngressError("MASTER_GATE_STATUS_INVALID")
    deps = gate.get("depends_on")
    evidence = gate.get("evidence")
    if not isinstance(deps, list) or not all(
        isinstance(x, str) and re.fullmatch(r"G[0-9]{2,4}", x) for x in deps
    ):
        raise WatchIngressError("MASTER_GATE_DEPENDENCIES_INVALID")
    if (
        not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 32
        or not all(isinstance(x, str) and 0 < len(x.strip()) <= 500 for x in evidence)
    ):
        raise WatchIngressError("MASTER_GATE_EVIDENCE_INVALID")
    graph_sha = str(gate.get("graph_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", graph_sha):
        raise WatchIngressError("MASTER_GATE_GRAPH_DIGEST_INVALID")
    if not checkpoint.current_phase.startswith(gate_id + "/"):
        raise WatchIngressError("MASTER_GATE_PHASE_CHECKPOINT_MISMATCH")
    if gate_id not in checkpoint.objective:
        raise WatchIngressError("MASTER_GATE_OBJECTIVE_CHECKPOINT_MISMATCH")
    return {
        "schema_version": MASTER_GATE_SCHEMA,
        "kind": "MASTER_GATE_DIRECTIVE",
        "gate_id": gate_id,
        "phase": phase,
        "title": title,
        "status": status,
        "depends_on": list(deps),
        "evidence": [str(x).strip() for x in evidence],
        "graph_sha256": graph_sha,
        "checkpoint_number": number,
        "instruction": checkpoint.next_intended_action,
    }


def _legacy_master_gate_namespace_migration_allowed(
    state: Any,
    incoming_checkpoint: AssignmentCheckpoint,
    master_gate: Mapping[str, Any],
) -> bool:
    """Allow exactly the historical checkpoint-1 -> G03/103 namespace migration."""

    current = state.checkpoint
    if current.checkpoint_number != LEGACY_FORGE_CHECKPOINT_NUMBER:
        return False
    if incoming_checkpoint.checkpoint_number != FIRST_MASTER_GATE_CHECKPOINT:
        return False
    if str(master_gate.get("gate_id") or "") != FIRST_MASTER_GATE_ID:
        return False
    if int(master_gate.get("checkpoint_number") or 0) != FIRST_MASTER_GATE_CHECKPOINT:
        return False
    if tuple(master_gate.get("depends_on") or ()) != ("G02",):
        return False
    if current.current_phase != LEGACY_FORGE_PHASE:
        return False
    if not current.objective.startswith(LEGACY_FORGE_OBJECTIVE_PREFIX):
        return False
    if incoming_checkpoint.repo != current.repo:
        return False
    if incoming_checkpoint.branch != current.branch:
        return False
    if incoming_checkpoint.open_pr != current.open_pr:
        return False
    if state.worker_kind is not WorkerKind.JAYTEC_CALLABLE:
        return False
    if state.worker_route not in {None, CALLABLE_ROUTE_ID}:
        return False
    if not str(state.worker_id or "").strip():
        return False
    if type(state.fencing_token) is not int or state.fencing_token < 1:
        return False
    if state.recovery_attempts != 0:
        return False
    if state.completed:
        return False
    if state.stop_reason is not StopReason.RUNNING:
        return False
    if str(state.progress_marker or "") != "MANUS_PENDING":
        return False
    if state.lease_owner is not None:
        return False
    return True


def _reconcile_known_local_preflight_exhaustion(
    store: PostgresAssignmentStore,
    state: Any,
    incoming_checkpoint: AssignmentCheckpoint,
    master_gate: Mapping[str, Any],
) -> tuple[Any, bool]:
    """Refund only the exact known G03 local-preflight miscount.

    This is intentionally narrower than a generic retry reset: it cannot alter
    the fence, worker, checkpoint, lease, authority, or any other task.
    """
    if (
        state.checkpoint.checkpoint_number != LOCAL_PREFLIGHT_REFUND_CHECKPOINT
        or incoming_checkpoint.checkpoint_number != LOCAL_PREFLIGHT_REFUND_CHECKPOINT
        or str(master_gate.get("gate_id") or "") != FIRST_MASTER_GATE_ID
        or state.fencing_token != LOCAL_PREFLIGHT_REFUND_FENCE
        or state.recovery_attempts != LOCAL_PREFLIGHT_REFUND_ATTEMPTS
        or state.stop_reason is not StopReason.RECOVERY_EXHAUSTED
        or str(state.last_error or "") != LOCAL_PREFLIGHT_REFUND_ERROR
        or state.lease_owner is not None
        or state.worker_kind is not WorkerKind.JAYTEC_CALLABLE
        or state.worker_route not in {None, CALLABLE_ROUTE_ID}
        or not str(state.worker_id or "").strip()
    ):
        return state, False

    repaired = store.reconcile_exhausted_local_preflight(
        state.task_id,
        expected_fencing_token=LOCAL_PREFLIGHT_REFUND_FENCE,
        expected_recovery_attempts=LOCAL_PREFLIGHT_REFUND_ATTEMPTS,
        expected_error=LOCAL_PREFLIGHT_REFUND_ERROR,
    )
    if not repaired:
        return state, False
    refreshed = store.get(state.task_id)
    if refreshed is None:
        raise WatchIngressError("LOCAL_PREFLIGHT_REFUND_STATE_MISSING")
    if (
        refreshed.fencing_token != LOCAL_PREFLIGHT_REFUND_FENCE
        or refreshed.recovery_attempts != LOCAL_PREFLIGHT_REFUND_ATTEMPTS - 1
        or refreshed.stop_reason is not StopReason.TRANSIENT_PROVIDER_FAILURE
        or str(refreshed.worker_id or "") != str(state.worker_id or "")
        or refreshed.checkpoint.checkpoint_number != state.checkpoint.checkpoint_number
    ):
        raise WatchIngressError("LOCAL_PREFLIGHT_REFUND_POSTCONDITION_FAILED")
    return refreshed, True


def _assignment_owner_directive(
    value: Any,
    *,
    task_id: str,
    state: Any,
    master_gate: Mapping[str, Any],
    terminal_result: Mapping[str, Any],
    required_receipt: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_INVALID")
    directive = dict(value)
    if set(directive) != ASSIGNMENT_OWNER_DIRECTIVE_FIELDS:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_FIELDS_INVALID")
    if directive.get("schema_version") != ASSIGNMENT_OWNER_DIRECTIVE_SCHEMA:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_SCHEMA_INVALID")
    if directive.get("task_id") != task_id:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_TASK_MISMATCH")
    if directive.get("assignment_owner") != "CHATGPT_ASSIGNMENT_OWNER":
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_OWNER_INVALID")

    gate_id = str(directive.get("gate_id") or "").strip()
    if gate_id != str(master_gate.get("gate_id") or ""):
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_GATE_MISMATCH")
    checkpoint_number = directive.get("checkpoint_number")
    if (
        type(checkpoint_number) is not int
        or checkpoint_number != state.checkpoint.checkpoint_number
    ):
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_CHECKPOINT_MISMATCH")

    worker_id = str(directive.get("worker_id") or "").strip()
    if not worker_id or worker_id != str(state.worker_id or ""):
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_WORKER_MISMATCH")
    fencing_token = directive.get("fencing_token")
    if type(fencing_token) is not int or fencing_token != state.fencing_token:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_FENCE_MISMATCH")

    for field in (
        "review_id",
        "request_id",
        "jaytec_review_id",
        "result_sha256",
        "manifest_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(directive.get(field) or "").lower()):
            raise WatchIngressError(
                "ASSIGNMENT_OWNER_DIRECTIVE_DIGEST_INVALID:" + field
            )

    decision = str(directive.get("decision") or "").strip()
    if decision not in {"REDIRECT", "REJECT_EVIDENCE"}:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_DECISION_INVALID")
    direction = str(directive.get("direction") or "").strip()
    if not direction or len(direction) > 1600:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_DIRECTION_INVALID")

    receipt = str(directive.get("result_receipt") or "").strip()
    if receipt != required_receipt:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_RECEIPT_MISMATCH")
    if not master_gate_result_has_receipt(terminal_result, receipt):
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_RESULT_RECEIPT_MISSING")

    result_sha = hashlib.sha256(
        json.dumps(
            dict(terminal_result),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if str(directive.get("result_sha256") or "").lower() != result_sha:
        raise WatchIngressError("ASSIGNMENT_OWNER_DIRECTIVE_RESULT_MISMATCH")

    return {
        "schema_version": ASSIGNMENT_OWNER_DIRECTIVE_SCHEMA,
        "task_id": task_id,
        "assignment_owner": "CHATGPT_ASSIGNMENT_OWNER",
        "gate_id": gate_id,
        "checkpoint_number": checkpoint_number,
        "review_id": str(directive["review_id"]).lower(),
        "request_id": str(directive["request_id"]).lower(),
        "jaytec_review_id": str(directive["jaytec_review_id"]).lower(),
        "decision": decision,
        "direction": direction,
        "result_receipt": receipt,
        "result_sha256": result_sha,
        "manifest_sha256": str(directive["manifest_sha256"]).lower(),
        "worker_id": worker_id,
        "fencing_token": fencing_token,
    }


def _broker_has_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _BROKER_SECRET_KEY.search(str(key)):
                return True
            if _broker_has_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_broker_has_secret_key(item) for item in value)
    elif isinstance(value, str) and _BROKER_SECRET_VALUE.search(value):
        return True
    return False


def _broker_context(value: Any, refs: Mapping[str, str]) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_INVALID")
    context = dict(value)
    kind = context.get("kind")
    base_required = {
        "schema_version",
        "kind",
        "repo",
        "refs",
        "authority",
        "sha256",
    }
    if kind == "PRIVATE_REPO_BOOTSTRAP":
        bootstrap_required = base_required | {
            "issues",
            "pull_requests",
            "open_pull_requests",
        }
        context_mode = context.get("context_mode")
        if context_mode is None:
            required = bootstrap_required
        elif context_mode in {"COMPACT_AUTHORITY", "MINIMAL_AUTHORITY"}:
            required = bootstrap_required | {"context_mode", "open_pr_window"}
        else:
            raise WatchIngressError("GITHUB_BROKER_CONTEXT_MODE_INVALID")
    elif kind == "SPECIALIST_REQUEST_RESULTS":
        required = base_required | {"request_results"}
    else:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_KIND_INVALID")
    if set(context) != required:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_FIELDS_INVALID")

    if kind == "PRIVATE_REPO_BOOTSTRAP" and context.get("context_mode") is not None:
        window = context.get("open_pr_window")
        if not isinstance(window, Mapping):
            raise WatchIngressError("GITHUB_BROKER_OPEN_PR_WINDOW_INVALID")
        if set(window) != {"limit", "complete"}:
            raise WatchIngressError("GITHUB_BROKER_OPEN_PR_WINDOW_FIELDS_INVALID")
        limit = window.get("limit")
        complete = window.get("complete")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit != 5:
            raise WatchIngressError("GITHUB_BROKER_OPEN_PR_WINDOW_LIMIT_INVALID")
        if not isinstance(complete, bool):
            raise WatchIngressError("GITHUB_BROKER_OPEN_PR_WINDOW_COMPLETE_INVALID")
        open_prs = context.get("open_pull_requests")
        if not isinstance(open_prs, list):
            raise WatchIngressError("GITHUB_BROKER_OPEN_PRS_INVALID")
        if len(open_prs) > limit:
            raise WatchIngressError("GITHUB_BROKER_OPEN_PR_WINDOW_OVERFLOW")
        # The producer checks pages 1..5 only. If all five rows are occupied it
        # deliberately cannot claim the collision window is exhaustive.
        if complete and len(open_prs) >= limit:
            raise WatchIngressError("GITHUB_BROKER_OPEN_PR_WINDOW_COMPLETENESS_INVALID")
    if context.get("schema_version") != "JAYTEC_GITHUB_BROKER_CONTEXT_V1":
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_VERSION_INVALID")
    if context.get("repo") != "jayagius99/jaytec-work-engine-v2-g1":
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_REPO_MISMATCH")
    if context.get("authority") != "READ_EVIDENCE_ONLY_NO_TOKEN_EXPORT":
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_AUTHORITY_INVALID")
    if _broker_has_secret_key(context):
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_SECRET_FIELD_FORBIDDEN")

    context_refs = context.get("refs")
    if not isinstance(context_refs, Mapping):
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_REFS_INVALID")
    normalized_refs = {str(k): str(v).lower() for k, v in context_refs.items()}
    for ref, sha in refs.items():
        if normalized_refs.get(ref) != sha:
            raise WatchIngressError("GITHUB_BROKER_CONTEXT_REF_MISMATCH")

    supplied_digest = str(context.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_digest):
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_DIGEST_INVALID")
    unsigned = {k: v for k, v in context.items() if k != "sha256"}
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_BROKER_CONTEXT_BYTES:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_TOO_LARGE")
    if hashlib.sha256(encoded).hexdigest() != supplied_digest:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_DIGEST_MISMATCH")
    return context


def _gate_result_ready_state(state: Any) -> bool:
    if state is None or not getattr(state, "worker_id", None):
        return False
    marker = str(getattr(state, "last_error", "") or "")
    phase = str(getattr(getattr(state, "checkpoint", None), "current_phase", "") or "")
    return (
        getattr(state, "stop_reason", None) is StopReason.WAITING_FOR_DEPENDENCY
        and marker.startswith("MANUS_TERMINAL:SUCCESS:")
        and re.match(r"^G[0-9]{2,4}/", phase) is not None
    )


def _specialist_protocol_repair_context() -> dict[str, Any]:
    """Content-minimized instruction for one same-worker specialist reissue."""
    return {
        "schema_version": "JAYTEC_SPECIALIST_PROTOCOL_REPAIR_V1",
        "kind": "SPECIALIST_PROTOCOL_REPAIR",
        "reason_code": "MANUS_LEGACY_SPECIALIST_INTENT_REQUIRES_REISSUE",
        "authority": "NO_AUTHORITY_EXPANSION_SAME_TASK_ONLY",
        "target_intent": {
            "type": "SPECIALIST_INTENT_V1",
            "exact_fields": [
                "type",
                "specialist",
                "objective",
                "reason",
                "required_context",
            ],
            "allowed_specialists": ["sol", "deepseek", "nemo", "github_broker"],
            "instruction": (
                "Reissue only the same bounded help request from your existing task "
                "context. Do not invent request_id, parent_task_id, directive_version, "
                "authority, or packet_sha256; JAYTEC creates them."
            ),
        },
        "github_broker": {
            "allowed_operations": [
                "read_file",
                "list_path",
                "read_issue",
                "read_pr",
                "read_workflow_runs",
                "create_branch",
                "write_file",
                "create_pr",
            ],
            "instruction": (
                "If the intended specialist is github_broker, required_context must "
                "contain one exact operation and only its required bounded arguments. "
                "Do not include credentials, secrets, sealed provenance, or unrelated context."
            ),
        },
    }


def _needs_jaytec_state(state: Any) -> bool:
    if state is None or not getattr(state, "worker_id", None):
        return False
    marker = str(getattr(state, "last_error", "") or "")
    return marker.startswith(
        ("MANUS_TERMINAL:NEEDS_JAYTEC:", "MANUS_TERMINAL:PARTIAL_SUCCESS:")
    )




def _safe_broker_path(value: Any) -> str:
    path = str(value or "").strip()
    if (
        not path
        or not _BROKER_SAFE_PATH.fullmatch(path)
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or _BROKER_BLOCKED_PATH.search(path)
    ):
        raise WatchIngressError("GITHUB_BROKER_PATH_INVALID")
    return path


def _safe_broker_ref(value: Any, refs: Mapping[str, str], fencing_token: int) -> str:
    ref = str(value or "").strip()
    if ref in refs:
        return ref
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        if ref not in set(refs.values()):
            raise WatchIngressError("GITHUB_BROKER_SHA_NOT_ATTESTED")
        return ref
    if _BROKER_WORKER_BRANCH.fullmatch(ref):
        expected = f"watch/worker-{fencing_token}-"
        if not ref.startswith(expected):
            raise WatchIngressError("GITHUB_BROKER_BRANCH_FENCE_MISMATCH")
        return ref
    raise WatchIngressError("GITHUB_BROKER_REF_INVALID")


def _broker_request_field_shape_error(
    op: str,
    context: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str] | None = None,
) -> WatchIngressError:
    """Return a content-free broker field-shape diagnostic.

    Only operation and field names are exposed. Values are never included.
    Unusual field names are replaced with a bounded digest label so diagnostics
    cannot become an exfiltration channel.
    """

    def safe_name(value: Any) -> str:
        name = str(value or "")
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
            return name
        return "sha256-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]

    present = {str(key) for key in context.keys()}
    extras = sorted(present - allowed)
    missing = sorted(set(required or ()) - present)
    parts = [
        "GITHUB_BROKER_REQUEST_FIELDS_INVALID",
        "op=" + safe_name(op),
    ]
    if extras:
        parts.append("extra=" + ",".join(safe_name(key) for key in extras[:8]))
    if missing:
        parts.append("missing=" + ",".join(safe_name(key) for key in missing[:8]))
    return WatchIngressError(":".join(parts))


def _normalize_broker_operation(
    request: Mapping[str, Any],
    *,
    refs: Mapping[str, str],
    fencing_token: int,
    mutation_authorized: bool,
) -> dict[str, Any]:
    validate_specialist_request(request)
    if request.get("specialist") != "github_broker":
        raise WatchIngressError("GITHUB_BROKER_SPECIALIST_INVALID")
    parent = str(request.get("parent_task_id") or "")
    if not parent.startswith(FORGE_TASK_ID):
        raise WatchIngressError("GITHUB_BROKER_PARENT_TASK_INVALID")
    raw_context = request.get("required_context")
    if not isinstance(raw_context, Mapping):
        raise WatchIngressError("GITHUB_BROKER_REQUEST_CONTEXT_INVALID")

    # lite may include a descriptive repository scope in untrusted intent.
    # Repository selection is JAYTEC authority, not a broker operation argument.
    # Accept only an exact assertion of the already-canonical repository, then
    # remove it before the strict operation-specific field contract is applied.
    context = dict(raw_context)
    asserted_repo = context.pop("repository", None)
    if asserted_repo is not None:
        if not isinstance(asserted_repo, str) or asserted_repo.strip() != EXPECTED_REPO:
            raise WatchIngressError("GITHUB_BROKER_REPOSITORY_SCOPE_MISMATCH")

    op = str(context.get("operation") or "").strip()
    if op not in BROKER_OPERATIONS:
        raise WatchIngressError("GITHUB_BROKER_OPERATION_INVALID")
    if op in BROKER_WRITE_OPERATIONS and not mutation_authorized:
        raise WatchIngressError("GITHUB_BROKER_MUTATION_NOT_AUTHORIZED")

    request_id = str(request.get("request_id") or "").strip()
    args: dict[str, Any] = {}

    if op == "read_file":
        allowed = {"operation", "path", "ref", "start_line", "end_line"}
        if set(context) - allowed:
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed
            )
        args["path"] = _safe_broker_path(context.get("path"))
        args["ref"] = _safe_broker_ref(context.get("ref"), refs, fencing_token)
        start_line = int(context.get("start_line") or 1)
        end_line = int(context.get("end_line") or min(start_line + 119, 5000))
        if start_line < 1 or end_line < start_line or end_line - start_line > 199:
            raise WatchIngressError("GITHUB_BROKER_LINE_RANGE_INVALID")
        args["start_line"] = start_line
        args["end_line"] = end_line
    elif op == "list_path":
        allowed = {"operation", "path", "ref"}
        if set(context) - allowed:
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed
            )
        raw_path = str(context.get("path") or "").strip()
        args["path"] = _safe_broker_path(raw_path) if raw_path else ""
        args["ref"] = _safe_broker_ref(context.get("ref"), refs, fencing_token)
    elif op in {"read_issue", "read_pr"}:
        allowed = {"operation", "number"}
        if set(context) - allowed:
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed
            )
        number = int(context.get("number") or 0)
        if number < 1 or number > 1000000:
            raise WatchIngressError("GITHUB_BROKER_NUMBER_INVALID")
        args["number"] = number
    elif op == "read_workflow_runs":
        allowed = {"operation", "branch"}
        if set(context) - allowed:
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed
            )
        branch_value = str(context.get("branch") or "").strip()
        if branch_value:
            args["branch"] = _safe_broker_ref(branch_value, refs, fencing_token)
    elif op == "create_branch":
        allowed = {"operation", "base_ref", "new_branch"}
        if set(context) != allowed:
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed, required=allowed
            )
        args["base_ref"] = _safe_broker_ref(
            context.get("base_ref"), refs, fencing_token
        )
        new_branch = str(context.get("new_branch") or "").strip()
        if (
            not _BROKER_WORKER_BRANCH.fullmatch(new_branch)
            or not new_branch.startswith(f"watch/worker-{fencing_token}-")
        ):
            raise WatchIngressError("GITHUB_BROKER_NEW_BRANCH_INVALID")
        args["new_branch"] = new_branch
    elif op == "write_file":
        allowed = {
            "operation",
            "branch",
            "path",
            "content",
            "commit_message",
            "expected_sha",
        }
        required = {"operation", "branch", "path", "content"}
        if set(context) - allowed or not required <= set(context):
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed, required=required
            )
        branch_name = _safe_broker_ref(
            context.get("branch"), refs, fencing_token
        )
        if not _BROKER_WORKER_BRANCH.fullmatch(branch_name):
            raise WatchIngressError("GITHUB_BROKER_WRITE_BRANCH_INVALID")
        content = str(context.get("content") or "")
        if not content or len(content.encode("utf-8")) > 12000:
            raise WatchIngressError("GITHUB_BROKER_WRITE_CONTENT_INVALID")
        commit_message = str(
            context.get("commit_message") or "JAYTEC broker update"
        ).strip()
        if not commit_message or len(commit_message) > 200:
            raise WatchIngressError("GITHUB_BROKER_COMMIT_MESSAGE_INVALID")
        expected_sha = str(context.get("expected_sha") or "").strip().lower()
        if expected_sha and not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
            raise WatchIngressError("GITHUB_BROKER_EXPECTED_SHA_INVALID")
        args.update(
            {
                "branch": branch_name,
                "path": _safe_broker_path(context.get("path")),
                "content": content,
                "commit_message": commit_message,
                "expected_sha": expected_sha or None,
            }
        )
    elif op == "create_pr":
        allowed = {"operation", "head", "base", "title", "body"}
        if set(context) != allowed:
            raise _broker_request_field_shape_error(
                op, context, allowed=allowed, required=allowed
            )
        head = _safe_broker_ref(context.get("head"), refs, fencing_token)
        if not _BROKER_WORKER_BRANCH.fullmatch(head):
            raise WatchIngressError("GITHUB_BROKER_PR_HEAD_INVALID")
        base = str(context.get("base") or "").strip()
        if base not in refs or _BROKER_WORKER_BRANCH.fullmatch(base):
            raise WatchIngressError("GITHUB_BROKER_PR_BASE_INVALID")
        title = str(context.get("title") or "").strip()
        body = str(context.get("body") or "").strip()
        if not title or len(title) > 240 or len(body) > 4000:
            raise WatchIngressError("GITHUB_BROKER_PR_TEXT_INVALID")
        args.update({"head": head, "base": base, "title": title, "body": body})

    return {
        "request_id": request_id,
        "operation": op,
        "args": args,
    }


def _broker_requests(
    terminal: Mapping[str, Any],
    *,
    refs: Mapping[str, str],
    fencing_token: int,
    mutation_authorized: bool,
) -> list[dict[str, Any]]:
    raw_requests = terminal.get("specialist_requests")
    if raw_requests in (None, []):
        return []
    if not isinstance(raw_requests, list) or len(raw_requests) > MAX_BROKER_REQUESTS:
        raise WatchIngressError("GITHUB_BROKER_REQUEST_COUNT_INVALID")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_requests:
        if not isinstance(raw, str) or not raw.strip():
            raise WatchIngressError("GITHUB_BROKER_REQUEST_INVALID")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WatchIngressError("GITHUB_BROKER_REQUEST_JSON_INVALID") from exc
        if not isinstance(decoded, Mapping):
            raise WatchIngressError("GITHUB_BROKER_REQUEST_INVALID")
        normalized = _normalize_broker_operation(
            decoded,
            refs=refs,
            fencing_token=fencing_token,
            mutation_authorized=mutation_authorized,
        )
        rid = normalized["request_id"]
        if rid in seen:
            raise WatchIngressError("GITHUB_BROKER_DUPLICATE_REQUEST_ID")
        seen.add(rid)
        out.append(normalized)
    return out


def _split_help_requests(
    terminal: Mapping[str, Any],
    *,
    refs: Mapping[str, str],
    fencing_token: int,
    mutation_authorized: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Split one Manus help request batch into GitHub and model assistance.

    Every request remains request-only. Unknown specialists fail closed rather
    than being silently routed to another model/provider.
    """
    raw_requests = terminal.get("specialist_requests")
    if raw_requests in (None, []):
        return [], [], []
    if not isinstance(raw_requests, list) or len(raw_requests) > MAX_HELP_REQUESTS:
        raise WatchIngressError("JAYTEC_HELP_REQUEST_COUNT_INVALID")

    github_requests: list[dict[str, Any]] = []
    model_requests: list[dict[str, Any]] = []
    order: list[str] = []
    seen: set[str] = set()

    for raw in raw_requests:
        if not isinstance(raw, str) or not raw.strip():
            raise WatchIngressError("JAYTEC_HELP_REQUEST_INVALID")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WatchIngressError("JAYTEC_HELP_REQUEST_JSON_INVALID") from exc
        if not isinstance(decoded, Mapping):
            raise WatchIngressError("JAYTEC_HELP_REQUEST_INVALID")

        validate_specialist_request(decoded)
        specialist = str(decoded.get("specialist") or "").strip().casefold()
        if specialist == "github_broker":
            if len(github_requests) >= MAX_BROKER_REQUESTS:
                raise WatchIngressError("GITHUB_BROKER_REQUEST_COUNT_INVALID")
            normalized = _normalize_broker_operation(
                decoded,
                refs=refs,
                fencing_token=fencing_token,
                mutation_authorized=mutation_authorized,
            )
            github_requests.append(normalized)
            rid = str(normalized["request_id"])
        elif specialist in ALLOWED_MANUS_MODEL_SPECIALISTS:
            try:
                normalized_model = normalize_manus_model_request(
                    decoded,
                    parent_task_prefix=FORGE_TASK_ID,
                )
            except SpecialistBrokerError as exc:
                raise WatchIngressError(str(exc)) from exc
            model_requests.append(normalized_model)
            rid = str(normalized_model["request_id"])
        else:
            raise WatchIngressError("JAYTEC_HELP_SPECIALIST_NOT_ALLOWLISTED:" + specialist)

        if rid in seen:
            raise WatchIngressError("JAYTEC_HELP_DUPLICATE_REQUEST_ID")
        seen.add(rid)
        order.append(rid)

    return github_requests, model_requests, order


def _model_results_match_requests(
    package: Mapping[str, Any],
    requests: list[Mapping[str, Any]],
) -> bool:
    rows = package.get("request_results")
    if not isinstance(rows, list) or len(rows) != len(requests):
        return False
    expected = [
        (str(req.get("request_id") or ""), str(req.get("specialist") or ""))
        for req in requests
    ]
    observed = [
        (str(row.get("request_id") or ""), str(row.get("specialist") or ""))
        for row in rows
        if isinstance(row, Mapping)
    ]
    return observed == expected


def _compose_assistance_context(
    *,
    github_context: Mapping[str, Any],
    github_requests: list[Mapping[str, Any]],
    model_package: Mapping[str, Any],
    model_requests: list[Mapping[str, Any]],
    request_order: list[str],
) -> dict[str, Any]:
    """Create one bounded result packet returned to the same Manus task."""
    rows_by_id: dict[str, dict[str, Any]] = {}

    if github_requests:
        if not _broker_results_match_requests(github_context, github_requests):
            raise WatchIngressError("GITHUB_BROKER_RESULTS_REQUEST_MISMATCH")
        raw_rows = github_context.get("request_results")
        assert isinstance(raw_rows, list)
        for request, row in zip(github_requests, raw_rows):
            if not isinstance(row, Mapping):
                raise WatchIngressError("GITHUB_BROKER_RESULT_INVALID")
            rid = str(request.get("request_id") or "")
            rows_by_id[rid] = {
                "request_id": rid,
                "specialist": "github_broker",
                "status": str(row.get("status") or "FAILED_CLOSED"),
                "operation": str(request.get("operation") or ""),
                "evidence": row.get("evidence"),
            }

    if model_requests:
        if not _model_results_match_requests(model_package, model_requests):
            raise WatchIngressError("MODEL_SPECIALIST_RESULTS_REQUEST_MISMATCH")
        raw_rows = model_package.get("request_results")
        assert isinstance(raw_rows, list)
        for row in raw_rows:
            assert isinstance(row, Mapping)
            rid = str(row.get("request_id") or "")
            rows_by_id[rid] = dict(row)

    ordered_rows = [rows_by_id[rid] for rid in request_order if rid in rows_by_id]
    if len(ordered_rows) != len(request_order):
        raise WatchIngressError("JAYTEC_ASSISTANCE_RESULT_SET_INCOMPLETE")

    unsigned = {
        "schema_version": "JAYTEC_ASSISTANCE_RESULTS_V1",
        "kind": "SPECIALIST_REQUEST_RESULTS",
        "authority": "RESULTS_ONLY_NO_AUTHORITY_EXPANSION",
        "request_results": ordered_rows,
    }
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > 4500:
        raise WatchIngressError("JAYTEC_ASSISTANCE_CONTEXT_TOO_LARGE")
    return {
        **unsigned,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _broker_results_match_requests(
    context: Mapping[str, Any],
    requests: list[Mapping[str, Any]],
) -> bool:
    if context.get("kind") != "SPECIALIST_REQUEST_RESULTS":
        return False
    results = context.get("request_results")
    if not isinstance(results, list) or len(results) != len(requests):
        return False
    expected = [str(r.get("request_id") or "") for r in requests]
    observed = [
        str(row.get("request_id") or "")
        for row in results
        if isinstance(row, Mapping)
    ]
    return observed == expected



def _decision_dict(decision) -> dict[str, Any]:
    return {
        "action": decision.action.value,
        "effective_stop_reason": decision.effective_stop_reason.value,
        "reason": decision.reason,
        "recovery_route": (
            decision.recovery_route.value if decision.recovery_route else None
        ),
    }


def prepare_schema_if_authorized(
    database_url: str,
    *,
    env: Mapping[str, str],
) -> dict[str, Any]:
    if _bool_env(env, "JAYTEC_AUTORECOVERY_SCHEMA_BOOTSTRAP"):
        PostgresAssignmentStore(database_url).ensure_schema()
    return schema_probe(database_url)


def execute_watch_cycle(
    payload: Mapping[str, Any],
    *,
    database_url: str,
    manus_runtime: Any | None,
    registry: Any,
    runtime_components_registered: bool,
    specialist_runner: Callable[[list[Mapping[str, Any]]], Mapping[str, Any]] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source = dict(os.environ if env is None else env)
    task_id = str(payload.get("task_id") or "").strip()
    if task_id != FORGE_TASK_ID:
        raise WatchIngressError("TASK_ID_NOT_WATCH_AUTHORIZED")

    refs = _observed_refs(payload.get("observed_refs"))
    broker_context = _broker_context(payload.get("github_broker_context"), refs)
    verifier = ObservedRefsCheckpointVerifier(refs)

    raw_checkpoint = payload.get("bootstrap_checkpoint")
    if not isinstance(raw_checkpoint, Mapping):
        raise WatchIngressError("BOOTSTRAP_CHECKPOINT_REQUIRED")
    incoming_checkpoint = AssignmentCheckpoint.from_mapping(raw_checkpoint)
    if incoming_checkpoint.task_id != task_id:
        raise WatchIngressError("BOOTSTRAP_TASK_ID_MISMATCH")
    master_gate = _master_gate_context(
        payload.get("master_gate"),
        incoming_checkpoint,
    )
    raw_assignment_owner_directive = payload.get("assignment_owner_directive")

    schema = prepare_schema_if_authorized(database_url, env=source)
    if schema.get("status") != "PASS" or schema.get("schema_present") is not True:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "AUTORECOVERY_SCHEMA_NOT_READY",
            "schema_probe": schema,
        }

    status = runtime_status(
        env=source,
        database_url=database_url,
        runtime_components_registered=runtime_components_registered,
    )
    if not status.active:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "AUTORECOVERY_RUNTIME_NOT_ACTIVE",
            "runtime": status.to_dict(),
        }
    if tuple(status.callable_worker_routes) != (CALLABLE_ROUTE_ID,):
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "CALLABLE_ROUTE_SET_MISMATCH",
        }

    if manus_runtime is None:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "CALLABLE_RUNTIME_COMPONENTS_UNAVAILABLE",
        }

    store = PostgresAssignmentStore(database_url)
    state = store.get(task_id)
    bootstrapped = False
    if state is not None:
        verified_current, current_detail = verifier.verify(state.checkpoint)
        if not verified_current:
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "CURRENT_CHECKPOINT_NOT_ATTESTED",
                "detail": current_detail,
            }

    verified_incoming, incoming_detail = verifier.verify(incoming_checkpoint)
    if not verified_incoming:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "BOOTSTRAP_CHECKPOINT_NOT_ATTESTED",
            "detail": incoming_detail,
        }

    if state is None:
        store.upsert_checkpoint(
            incoming_checkpoint,
            stop_reason=StopReason.RUNNING,
            worker_kind=WorkerKind.JAYTEC_CALLABLE,
            worker_id=None,
            worker_route=CALLABLE_ROUTE_ID,
            heartbeat_at=None,
            progress_marker=None,
            completed=False,
        )
        state = store.get(task_id)
        bootstrapped = True

    assert state is not None

    verified_current, current_detail = verifier.verify(state.checkpoint)
    if not verified_current:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "CURRENT_CHECKPOINT_NOT_ATTESTED",
            "detail": current_detail,
        }

    checkpoint_advanced = False
    current_checkpoint_number = state.checkpoint.checkpoint_number
    incoming_checkpoint_number = incoming_checkpoint.checkpoint_number
    if incoming_checkpoint_number < current_checkpoint_number:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "MASTER_GATE_CHECKPOINT_ROLLBACK_FORBIDDEN",
        }
    if incoming_checkpoint_number > current_checkpoint_number:
        namespace_migration = _legacy_master_gate_namespace_migration_allowed(
            state,
            incoming_checkpoint,
            master_gate,
        )
        if (
            incoming_checkpoint_number != current_checkpoint_number + 1
            and not namespace_migration
        ):
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "MASTER_GATE_CHECKPOINT_GAP_FORBIDDEN",
            }
        if not namespace_migration and not _gate_result_ready_state(state):
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "MASTER_GATE_ADVANCE_REQUIRES_VERIFIED_PRIOR_SUCCESS",
            }
        checkpoint_advanced = store.advance_checkpoint_preserving_runtime(
            incoming_checkpoint,
            expected_current_checkpoint_number=current_checkpoint_number,
        )
        state = store.get(task_id)
        assert state is not None
        if state.worker_id:
            progress_marker = (
                "MASTER_GATE_NAMESPACE_MIGRATED:"
                if namespace_migration
                else "MASTER_GATE_ADVANCED:"
            )
            store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                progress_marker=(
                    progress_marker
                    + str(master_gate.get("gate_id") or "UNKNOWN")
                    + ":"
                    + str(incoming_checkpoint_number)
                ),
            )
            state = store.get(task_id)
            assert state is not None
    if state.worker_kind is not WorkerKind.JAYTEC_CALLABLE:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "ASSIGNMENT_NOT_JAYTEC_CALLABLE",
            "worker_kind": state.worker_kind.value,
        }
    if state.worker_route not in {None, CALLABLE_ROUTE_ID}:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "ASSIGNMENT_WORKER_ROUTE_MISMATCH",
            "worker_route": state.worker_route,
        }

    verified_state, verified_detail = verifier.verify(state.checkpoint)
    if not verified_state:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "CURRENT_CHECKPOINT_NOT_ATTESTED",
            "detail": verified_detail,
        }

    state, local_preflight_refunded = _reconcile_known_local_preflight_exhaustion(
        store,
        state,
        incoming_checkpoint,
        master_gate,
    )

    invoker = ManusLiteRecoveryInvoker(
        manus_runtime,
        registry,
        broker_context=broker_context,
    )
    github_broker = "AVAILABLE" if broker_context else "NONE"
    required_gate_receipt = (
        master_gate_handoff_id(
            state.checkpoint,
            state.fencing_token,
            master_gate,
        )
        if state.worker_id
        else None
    )

    # Protocol repair is not worker recovery. If a historical Lite result
    # uses the observed non-canonical six-field specialist intent, give the SAME
    # worker/fence one deterministic correction turn before health/recovery logic.
    # No old request values are copied into the handoff.
    if (
        state.worker_id
        and state.stop_reason is StopReason.RUNNING
        and str(state.progress_marker or "") in {
            "MANUS_PENDING",
            "SPECIALIST_PROTOCOL_REPAIR_PENDING",
        }
    ):
        protocol_probe = manus_runtime.task_status_readonly(
            state.worker_id,
            parent_task_id=task_id,
        )
        if (
            isinstance(protocol_probe, Mapping)
            and str(protocol_probe.get("status") or "") == "FAILED_CLOSED"
            and str(protocol_probe.get("reason") or "")
            == "MANUS_RUNTIME_GOVERNANCE_REJECTED:"
               "MANUS_LEGACY_SPECIALIST_INTENT_REQUIRES_REISSUE"
        ):
            if str(state.progress_marker or "") == "SPECIALIST_PROTOCOL_REPAIR_PENDING":
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
                    "protocol_repair": {
                        "schema_version": "JAYTEC_SPECIALIST_PROTOCOL_REPAIR_V1",
                        "worker_replaced": False,
                        "recovery_attempt_consumed": False,
                        "duplicate_handoff_sent": False,
                    },
                }
            repair_context = _specialist_protocol_repair_context()
            handoff = invoker.continue_existing(
                checkpoint=state.checkpoint,
                worker_id=state.worker_id,
                fencing_token=state.fencing_token,
                broker_context=repair_context,
            )
            if not handoff.accepted:
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": "SPECIALIST_PROTOCOL_REPAIR_HANDOFF_FAILED:"
                    + str(handoff.detail or "unknown")[:220],
                }

            marker = "SPECIALIST_PROTOCOL_REPAIR_PENDING"
            store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                progress_marker=marker,
            )
            final = store.get(task_id)
            return {
                "status": "PASS",
                "task_id": task_id,
                "bootstrapped": bootstrapped,
                "checkpoint_advanced": checkpoint_advanced,
                "master_gate": master_gate,
                "gate_handoff": "NOT_NEEDED",
                "health_refreshed": False,
                "github_broker": "NONE",
                "protocol_repair": {
                    "status": (
                        "IDEMPOTENT_REPLAY_WAITING"
                        if handoff.detail == "MANUS_JAYTEC_HANDOFF_REPLAY"
                        else "SAME_WORKER_CONTINUED"
                    ),
                    "schema_version": "JAYTEC_SPECIALIST_PROTOCOL_REPAIR_V1",
                    "worker_replaced": False,
                    "recovery_attempt_consumed": False,
                },
                "decision": {
                    "action": "NOOP_HEALTHY",
                    "effective_stop_reason": StopReason.RUNNING.value,
                    "reason": "SPECIALIST_PROTOCOL_REPAIR_CONTINUED",
                    "recovery_route": None,
                },
                "assignment": {
                    "stop_reason": final.stop_reason.value if final else None,
                    "worker_kind": final.worker_kind.value if final else None,
                    "worker_id": final.worker_id if final else None,
                    "worker_route": final.worker_route if final else None,
                    "checkpoint_number": final.checkpoint.checkpoint_number if final else None,
                    "repo": final.checkpoint.repo if final else None,
                    "branch": final.checkpoint.branch if final else None,
                    "verified_head": final.checkpoint.commit_head if final else None,
                    "recovery_attempts": final.recovery_attempts if final else None,
                    "fencing_token": final.fencing_token if final else None,
                    "progress_marker": final.progress_marker if final else None,
                    "completed": final.completed if final else None,
                    "last_error": final.last_error if final else None,
                },
            }

    if _gate_result_ready_state(state):
        readonly = manus_runtime.task_status_readonly(state.worker_id, parent_task_id=task_id)
        terminal = (
            readonly.get("result")
            if isinstance(readonly, Mapping)
            and isinstance(readonly.get("result"), Mapping)
            else {}
        )
        readonly_ok = str(readonly.get("status") or "") == "VERIFIED_COMPLETE"
        terminal_success = str(terminal.get("status") or "") == "SUCCESS"
        receipt_matches = bool(
            required_gate_receipt
            and master_gate_result_has_receipt(
                terminal,
                required_gate_receipt,
            )
        )
        if readonly_ok and terminal_success and receipt_matches:
            if raw_assignment_owner_directive is not None:
                try:
                    owner_directive = _assignment_owner_directive(
                        raw_assignment_owner_directive,
                        task_id=task_id,
                        state=state,
                        master_gate=master_gate,
                        terminal_result=terminal,
                        required_receipt=required_gate_receipt,
                    )
                except WatchIngressError as exc:
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": str(exc),
                    }
                redirect = invoker.continue_assignment_owner_directive(
                    checkpoint=state.checkpoint,
                    worker_id=state.worker_id,
                    fencing_token=state.fencing_token,
                    directive=owner_directive,
                )
                if not redirect.accepted:
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": redirect.detail,
                    }
                store.heartbeat(
                    task_id,
                    fencing_token=state.fencing_token,
                    worker_id=state.worker_id,
                    progress_marker=(
                        "ASSIGNMENT_OWNER_REDIRECT:"
                        + owner_directive["review_id"][:16]
                    ),
                )
                final = store.get(task_id)
                return {
                    "status": "PASS",
                    "task_id": task_id,
                    "bootstrapped": bootstrapped,
                    "checkpoint_advanced": checkpoint_advanced,
                    "master_gate": master_gate,
                    "gate_handoff": "OWNER_REDIRECT_CONTINUED",
                    "owner_redirect": {
                        "status": redirect.detail,
                        "review_id": owner_directive["review_id"],
                        "request_id": owner_directive["request_id"],
                        "decision": owner_directive["decision"],
                    },
                    "health_refreshed": True,
                    "github_broker": github_broker,
                    "decision": {
                        "action": "NOOP_HEALTHY",
                        "effective_stop_reason": StopReason.RUNNING.value,
                        "reason": "ASSIGNMENT_OWNER_REDIRECT_CONTINUED",
                        "recovery_route": None,
                    },
                    "assignment": {
                        "stop_reason": final.stop_reason.value if final else None,
                        "worker_kind": final.worker_kind.value if final else None,
                        "worker_id": final.worker_id if final else None,
                        "worker_route": final.worker_route if final else None,
                        "checkpoint_number": final.checkpoint.checkpoint_number if final else None,
                        "repo": final.checkpoint.repo if final else None,
                        "branch": final.checkpoint.branch if final else None,
                        "verified_head": final.checkpoint.commit_head if final else None,
                        "recovery_attempts": final.recovery_attempts if final else None,
                        "fencing_token": final.fencing_token if final else None,
                        "progress_marker": final.progress_marker if final else None,
                        "completed": final.completed if final else None,
                        "last_error": final.last_error if final else None,
                    },
                }
            final = store.get(task_id)
            return {
                "status": "PASS",
                "task_id": task_id,
                "bootstrapped": bootstrapped,
                "checkpoint_advanced": checkpoint_advanced,
                "master_gate": master_gate,
                "gate_handoff": "RESULT_READY",
                "health_refreshed": False,
                "github_broker": github_broker,
                "gate_result": terminal,
                "decision": {
                    "action": "HOLD",
                    "effective_stop_reason": StopReason.WAITING_FOR_DEPENDENCY.value,
                    "reason": "MASTER_GATE_RESULT_READY",
                    "recovery_route": None,
                },
                "assignment": {
                    "stop_reason": final.stop_reason.value if final else None,
                    "worker_kind": final.worker_kind.value if final else None,
                    "worker_id": final.worker_id if final else None,
                    "worker_route": final.worker_route if final else None,
                    "checkpoint_number": final.checkpoint.checkpoint_number if final else None,
                    "repo": final.checkpoint.repo if final else None,
                    "branch": final.checkpoint.branch if final else None,
                    "verified_head": final.checkpoint.commit_head if final else None,
                    "recovery_attempts": final.recovery_attempts if final else None,
                    "fencing_token": final.fencing_token if final else None,
                    "progress_marker": final.progress_marker if final else None,
                    "completed": final.completed if final else None,
                    "last_error": final.last_error if final else None,
                },
            }
        if readonly_ok and terminal_success and not receipt_matches:
            # A completed result from the prior master gate is valid evidence
            # for that prior gate, but it has no authority over the newly
            # selected gate. Re-open the SAME fenced worker and continue below.
            store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                progress_marker="MASTER_GATE_STALE_RESULT_IGNORED",
            )
            state = store.get(task_id)
        else:
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "MASTER_GATE_RESULT_NOT_VERIFIED",
            }

    if raw_assignment_owner_directive is not None:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "ASSIGNMENT_OWNER_REDIRECT_REQUIRES_CURRENT_GATE_SUCCESS",
        }

    # NEEDS_JAYTEC is an internal orchestration handoff, not an owner boundary.
    # Keep one canonical Manus worker/fence while JAYTEC services bounded help.
    # GitHub broker work and model-specialist work are correlated into one result
    # package before being returned to that SAME Manus task.
    had_internal_dependency = _needs_jaytec_state(state)
    if had_internal_dependency:
        readonly = manus_runtime.task_status_readonly(state.worker_id, parent_task_id=task_id)
        terminal = (
            readonly.get("result")
            if isinstance(readonly, Mapping)
            and isinstance(readonly.get("result"), Mapping)
            else {}
        )
        readonly_status = str(readonly.get("status") or "")
        terminal_status = str(terminal.get("status") or "")
        dependency_superseded = False

        # A successful handoff may make the same provider task PENDING before the
        # next WATCH cycle. The durable state can still carry the previous
        # NEEDS_JAYTEC marker. Preserve the SAME worker/fence.
        if readonly_status == "PENDING":
            store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                progress_marker="MANUS_PENDING",
            )
            state = store.get(task_id)
            github_broker = "WORKER_RESUMED_PENDING"
            dependency_superseded = True
        elif (
            readonly_status == "VERIFIED_COMPLETE"
            and terminal_status not in {"NEEDS_JAYTEC", "PARTIAL_SUCCESS"}
        ):
            store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                progress_marker=None,
            )
            state = store.get(task_id)
            github_broker = "WORKER_TERMINAL_ADVANCED"
            dependency_superseded = True
        elif (
            readonly_status != "VERIFIED_COMPLETE"
            or terminal_status not in {"NEEDS_JAYTEC", "PARTIAL_SUCCESS"}
        ):
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "NEEDS_JAYTEC_TERMINAL_RESULT_NOT_VERIFIED",
            }

        if not dependency_superseded:
            envelope = dict(state.checkpoint.authority_envelope or {})
            connector_purposes = envelope.get("connector_purposes")
            mutation_authorized = bool(
                envelope.get("connector_mutation_authorized") is True
                and isinstance(connector_purposes, Mapping)
                and str(connector_purposes.get("github") or "").casefold() == "write"
            )
            try:
                github_requests, model_requests, request_order = _split_help_requests(
                    terminal,
                    refs=refs,
                    fencing_token=state.fencing_token,
                    mutation_authorized=mutation_authorized,
                )
            except (WatchIngressError, SpecialistBrokerError) as exc:
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": str(exc),
                }

            if not request_order:
                # Compatibility for a pre-specialist-fabric Manus task that reached
                # NEEDS_JAYTEC before returning explicit SPECIALIST_REQUEST packets.
                # A prevalidated private-GitHub bootstrap context may still be
                # returned to the SAME worker/fence exactly once. New model help
                # never uses this compatibility path.
                if broker_context:
                    handoff = invoker.continue_existing(
                        checkpoint=state.checkpoint,
                        worker_id=state.worker_id,
                        fencing_token=state.fencing_token,
                        broker_context=broker_context,
                    )
                    if handoff.accepted:
                        digest = str(broker_context.get("sha256") or "")[:16]
                        store.heartbeat(
                            task_id,
                            fencing_token=state.fencing_token,
                            worker_id=state.worker_id,
                            progress_marker="JAYTEC_BROKER_HANDOFF:" + digest,
                        )
                        final = store.get(task_id)
                        return {
                            "status": "PASS",
                            "task_id": task_id,
                            "bootstrapped": bootstrapped,
                            "health_refreshed": False,
                            "github_broker": "HANDOFF_CONTINUED",
                            "decision": {
                                "action": "NOOP_HEALTHY",
                                "effective_stop_reason": StopReason.RUNNING.value,
                                "reason": "JAYTEC_INTERNAL_HANDOFF_CONTINUED",
                                "recovery_route": None,
                            },
                            "assignment": {
                                "stop_reason": final.stop_reason.value if final else None,
                                "worker_kind": final.worker_kind.value if final else None,
                                "worker_id": final.worker_id if final else None,
                                "worker_route": final.worker_route if final else None,
                                "checkpoint_number": final.checkpoint.checkpoint_number if final else None,
                                "repo": final.checkpoint.repo if final else None,
                                "branch": final.checkpoint.branch if final else None,
                                "verified_head": final.checkpoint.commit_head if final else None,
                                "recovery_attempts": final.recovery_attempts if final else None,
                                "fencing_token": final.fencing_token if final else None,
                                "progress_marker": final.progress_marker if final else None,
                                "completed": final.completed if final else None,
                                "last_error": final.last_error if final else None,
                            },
                        }
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": str(handoff.detail or "LEGACY_BROKER_HANDOFF_FAILED"),
                    }

                final = store.get(task_id)
                return {
                    "status": "PASS",
                    "task_id": task_id,
                    "bootstrapped": bootstrapped,
                    "health_refreshed": False,
                    "github_broker": "NONE",
                    "decision": {
                        "action": "HOLD",
                        "effective_stop_reason": StopReason.WAITING_FOR_DEPENDENCY.value,
                        "reason": "JAYTEC_INTERNAL_HELP_REQUEST_REQUIRED",
                        "recovery_route": None,
                    },
                    "assignment": {
                        "stop_reason": final.stop_reason.value if final else None,
                        "worker_kind": final.worker_kind.value if final else None,
                        "worker_id": final.worker_id if final else None,
                        "worker_route": final.worker_route if final else None,
                        "checkpoint_number": final.checkpoint.checkpoint_number if final else None,
                        "repo": final.checkpoint.repo if final else None,
                        "branch": final.checkpoint.branch if final else None,
                        "verified_head": final.checkpoint.commit_head if final else None,
                        "recovery_attempts": final.recovery_attempts if final else None,
                        "fencing_token": final.fencing_token if final else None,
                        "progress_marker": final.progress_marker if final else None,
                        "completed": final.completed if final else None,
                    },
                }

            # GitHub operations must still be executed by the separately bounded
            # GitHub Actions broker. Model specialists never receive GitHub tokens.
            if github_requests and broker_context.get("kind") != "SPECIALIST_REQUEST_RESULTS":
                final = store.get(task_id)
                return {
                    "status": "PASS",
                    "task_id": task_id,
                    "bootstrapped": bootstrapped,
                    "health_refreshed": False,
                    "github_broker": "REQUESTS_REQUIRED",
                    "github_broker_requests": github_requests,
                    "model_specialist_requests_pending": [
                        {
                            "request_id": str(req.get("request_id") or ""),
                            "specialist": str(req.get("specialist") or ""),
                        }
                        for req in model_requests
                    ],
                    "decision": {
                        "action": "HOLD",
                        "effective_stop_reason": StopReason.WAITING_FOR_DEPENDENCY.value,
                        "reason": "JAYTEC_GITHUB_BROKER_REQUESTS_REQUIRED",
                        "recovery_route": None,
                    },
                    "assignment": {
                        "stop_reason": final.stop_reason.value if final else None,
                        "worker_kind": final.worker_kind.value if final else None,
                        "worker_id": final.worker_id if final else None,
                        "worker_route": final.worker_route if final else None,
                        "checkpoint_number": (
                            final.checkpoint.checkpoint_number if final else None
                        ),
                        "repo": final.checkpoint.repo if final else None,
                        "branch": final.checkpoint.branch if final else None,
                        "verified_head": (
                            final.checkpoint.commit_head if final else None
                        ),
                        "recovery_attempts": final.recovery_attempts if final else None,
                        "fencing_token": final.fencing_token if final else None,
                        "progress_marker": final.progress_marker if final else None,
                        "completed": final.completed if final else None,
                    },
                }

            if github_requests and not _broker_results_match_requests(
                broker_context, github_requests
            ):
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": "GITHUB_BROKER_RESULTS_REQUEST_MISMATCH",
                }
            if not github_requests and broker_context.get("kind") == "SPECIALIST_REQUEST_RESULTS":
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": "UNEXPECTED_GITHUB_BROKER_RESULTS",
                }

            model_package: Mapping[str, Any] = {
                "schema_version": "JAYTEC_MANUS_SPECIALIST_RESULTS_V1",
                "authority": "RESULTS_ONLY_NO_DISPATCH_AUTHORITY",
                "request_results": [],
                "sha256": hashlib.sha256(b"{}").hexdigest(),
            }
            if model_requests:
                if specialist_runner is None:
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": "MODEL_SPECIALIST_RUNNER_UNAVAILABLE",
                    }
                try:
                    model_package = dict(specialist_runner(model_requests))
                except Exception as exc:
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": "MODEL_SPECIALIST_RUNNER_FAILED:" + type(exc).__name__,
                    }
                if not _model_results_match_requests(model_package, model_requests):
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": "MODEL_SPECIALIST_RESULTS_REQUEST_MISMATCH",
                    }

            try:
                assistance_context = _compose_assistance_context(
                    github_context=broker_context,
                    github_requests=github_requests,
                    model_package=model_package,
                    model_requests=model_requests,
                    request_order=request_order,
                )
            except WatchIngressError as exc:
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": str(exc),
                }

            handoff_digest = str(assistance_context.get("sha256") or "")[:16]
            delivered_marker = "JAYTEC_ASSISTANCE_HANDOFF:" + handoff_digest
            if state.progress_marker == delivered_marker:
                github_broker = "WAITING_FOR_NEW_CONTEXT"
            else:
                handoff = invoker.continue_existing(
                    checkpoint=state.checkpoint,
                    worker_id=state.worker_id,
                    fencing_token=state.fencing_token,
                    broker_context=assistance_context,
                )
                if handoff.accepted and handoff.detail == "MANUS_JAYTEC_HANDOFF_REPLAY":
                    store.mark_stop(
                        task_id,
                        fencing_token=state.fencing_token,
                        stop_reason=StopReason.STALLED_RECOVERABLE,
                        error="JAYTEC_INTERNAL_HANDOFF_REPLAY_NO_PROGRESS",
                    )
                    github_broker = "HANDOFF_REPLAY_NO_PROGRESS"
                    state = store.get(task_id)
                elif handoff.accepted:
                    store.heartbeat(
                        task_id,
                        fencing_token=state.fencing_token,
                        worker_id=state.worker_id,
                        progress_marker=delivered_marker,
                    )
                    github_broker = "HANDOFF_CONTINUED" if github_requests else "NONE"
                    continuation_reason = (
                        "JAYTEC_INTERNAL_HANDOFF_CONTINUED"
                        if github_requests and not model_requests
                        else "JAYTEC_INTERNAL_ASSISTANCE_CONTINUED"
                    )
                    final = store.get(task_id)
                    return {
                        "status": "PASS",
                        "task_id": task_id,
                        "bootstrapped": bootstrapped,
                        "health_refreshed": False,
                        "github_broker": github_broker,
                        "specialist_results": safe_result_summary(model_package),
                        "decision": {
                            "action": "NOOP_HEALTHY",
                            "effective_stop_reason": StopReason.RUNNING.value,
                            "reason": continuation_reason,
                            "recovery_route": None,
                        },
                        "assignment": {
                            "stop_reason": final.stop_reason.value if final else None,
                            "worker_kind": final.worker_kind.value if final else None,
                            "worker_id": final.worker_id if final else None,
                            "worker_route": final.worker_route if final else None,
                            "checkpoint_number": (
                                final.checkpoint.checkpoint_number if final else None
                            ),
                            "repo": final.checkpoint.repo if final else None,
                            "branch": final.checkpoint.branch if final else None,
                            "verified_head": (
                                final.checkpoint.commit_head if final else None
                            ),
                            "recovery_attempts": (
                                final.recovery_attempts if final else None
                            ),
                            "fencing_token": final.fencing_token if final else None,
                            "progress_marker": (
                                final.progress_marker if final else None
                            ),
                            "completed": final.completed if final else None,
                            "last_error": final.last_error if final else None,
                        },
                    }
                else:
                    original_marker = str(
                        state.last_error
                        or "MANUS_TERMINAL:NEEDS_JAYTEC:unknown"
                    )
                    store.mark_stop(
                        task_id,
                        fencing_token=state.fencing_token,
                        stop_reason=StopReason.WAITING_FOR_DEPENDENCY,
                        error=(
                            original_marker
                            + "|JAYTEC_HANDOFF_FAILED:"
                            + str(handoff.detail or "unknown")[:500]
                        ),
                    )
                    github_broker = "HANDOFF_FAILED_CLOSED"
                state = store.get(task_id)

    gate_handoff = "NOT_NEEDED"
    if (
        not had_internal_dependency
        and state.worker_id
        and state.stop_reason is StopReason.RUNNING
    ):
        handoff = invoker.continue_gate_directive(
            checkpoint=state.checkpoint,
            worker_id=state.worker_id,
            fencing_token=state.fencing_token,
            gate_context=master_gate,
        )
        if not handoff.accepted:
            store.mark_stop(
                task_id,
                fencing_token=state.fencing_token,
                stop_reason=StopReason.WAITING_FOR_DEPENDENCY,
                error="MASTER_GATE_HANDOFF_FAILED:" + str(handoff.detail or "unknown")[:400],
            )
            state = store.get(task_id)
            gate_handoff = "FAILED_CLOSED"
        elif handoff.detail == "MANUS_GATE_HANDOFF_REPLAY":
            gate_handoff = "IDEMPOTENT_REPLAY"
        else:
            gate_handoff = "CONTINUED"
            store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                progress_marker=(
                    "MASTER_GATE:" + str(master_gate.get("gate_id"))
                    + ":" + str(master_gate.get("graph_sha256"))[:12]
                ),
            )
            state = store.get(task_id)

    supervisor = AutoRecoverySupervisor(
        store=store,
        verifier=verifier,
        invoker=invoker,
        health_probe=ManusLiteHealthProbe(
            manus_runtime,
            required_result_receipt=required_gate_receipt,
        ),
        notifier=JsonLogRecoveryNotifier(),
        instance_id="github-watch-cycle",
        heartbeat_timeout_seconds=WATCH_HEARTBEAT_TIMEOUT_SECONDS,
        health_refresh_timeout_seconds=WATCH_HEALTH_REFRESH_TIMEOUT_SECONDS,
    )

    refreshed = supervisor.refresh_worker_health(task_id)
    refreshed_state = store.get(task_id)
    if _gate_result_ready_state(refreshed_state):
        readonly = manus_runtime.task_status_readonly(refreshed_state.worker_id, parent_task_id=task_id)
        terminal = (
            readonly.get("result")
            if isinstance(readonly, Mapping)
            and isinstance(readonly.get("result"), Mapping)
            else {}
        )
        if (
            str(readonly.get("status") or "") != "VERIFIED_COMPLETE"
            or str(terminal.get("status") or "") != "SUCCESS"
            or not required_gate_receipt
            or not master_gate_result_has_receipt(
                terminal,
                required_gate_receipt,
            )
        ):
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "MASTER_GATE_RESULT_NOT_VERIFIED",
            }
        final = refreshed_state
        return {
            "status": "PASS",
            "task_id": task_id,
            "bootstrapped": bootstrapped,
            "checkpoint_advanced": checkpoint_advanced,
            "master_gate": master_gate,
            "gate_handoff": gate_handoff,
            "health_refreshed": refreshed,
            "github_broker": github_broker,
            "gate_result": terminal,
            "decision": {
                "action": "HOLD",
                "effective_stop_reason": StopReason.WAITING_FOR_DEPENDENCY.value,
                "reason": "MASTER_GATE_RESULT_READY",
                "recovery_route": None,
            },
            "assignment": {
                "stop_reason": final.stop_reason.value,
                "worker_kind": final.worker_kind.value,
                "worker_id": final.worker_id,
                "worker_route": final.worker_route,
                "checkpoint_number": final.checkpoint.checkpoint_number,
                "repo": final.checkpoint.repo,
                "branch": final.checkpoint.branch,
                "verified_head": final.checkpoint.commit_head,
                "recovery_attempts": final.recovery_attempts,
                "fencing_token": final.fencing_token,
                "progress_marker": final.progress_marker,
                "completed": final.completed,
                "last_error": final.last_error,
            },
        }

    decision = supervisor.tick(task_id)
    final = store.get(task_id)
    return {
        "status": "PASS",
        "task_id": task_id,
        "bootstrapped": bootstrapped,
        "checkpoint_advanced": checkpoint_advanced,
        "master_gate": master_gate,
        "gate_handoff": gate_handoff,
        "health_refreshed": refreshed,
        "github_broker": github_broker,
        "local_preflight_refunded": local_preflight_refunded,
        "decision": _decision_dict(decision),
        "assignment": {
            "stop_reason": final.stop_reason.value if final else None,
            "worker_kind": final.worker_kind.value if final else None,
            "worker_id": final.worker_id if final else None,
            "worker_route": final.worker_route if final else None,
            "checkpoint_number": (
                final.checkpoint.checkpoint_number if final else None
            ),
            "repo": final.checkpoint.repo if final else None,
            "branch": final.checkpoint.branch if final else None,
            "verified_head": final.checkpoint.commit_head if final else None,
            "recovery_attempts": final.recovery_attempts if final else None,
            "fencing_token": final.fencing_token if final else None,
            "progress_marker": final.progress_marker if final else None,
            "completed": final.completed if final else None,
            "last_error": final.last_error if final else None,
        },
    }


__all__ = [
    "FORGE_TASK_ID",
    "WatchIngressError",
    "execute_watch_cycle",
    "prepare_schema_if_authorized",
]
