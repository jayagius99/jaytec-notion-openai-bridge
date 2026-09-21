"""WATCH-only runtime ingress for one canonical JAYTEC autorecovery cycle."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from typing import Any, Mapping

from autorecovery_components import (
    CALLABLE_ROUTE_ID,
    JsonLogRecoveryNotifier,
    ManusLiteHealthProbe,
    ManusLiteRecoveryInvoker,
    ObservedRefsCheckpointVerifier,
    master_gate_handoff_id,
    master_gate_result_has_receipt,
)
from autorecovery_runtime import runtime_status, schema_probe
from manus_governance import validate_specialist_request
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
# WATCH cadence is 15 minutes. Require more than two missed cadence windows
# before classifying a RUNNING callable worker as lost, so one transient
# provider-status failure cannot consume a recovery fence/attempt.
WATCH_HEARTBEAT_TIMEOUT_SECONDS = 35 * 60
WATCH_HEALTH_REFRESH_TIMEOUT_SECONDS = 30
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
    if not re.fullmatch(r"G[0-9]{2}", gate_id):
        raise WatchIngressError("MASTER_GATE_ID_INVALID")
    number = int(gate.get("checkpoint_number") or 0)
    if number != 100 + int(gate_id[1:]):
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
        isinstance(x, str) and re.fullmatch(r"G[0-9]{2}", x) for x in deps
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
        required = base_required | {
            "issues",
            "pull_requests",
            "open_pull_requests",
        }
    elif kind == "SPECIALIST_REQUEST_RESULTS":
        required = base_required | {"request_results"}
    else:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_KIND_INVALID")
    if set(context) != required:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_FIELDS_INVALID")
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
        and re.match(r"^G[0-9]{2}/", phase) is not None
    )


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
    context = request.get("required_context")
    if not isinstance(context, Mapping):
        raise WatchIngressError("GITHUB_BROKER_REQUEST_CONTEXT_INVALID")
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
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
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
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
        raw_path = str(context.get("path") or "").strip()
        args["path"] = _safe_broker_path(raw_path) if raw_path else ""
        args["ref"] = _safe_broker_ref(context.get("ref"), refs, fencing_token)
    elif op in {"read_issue", "read_pr"}:
        allowed = {"operation", "number"}
        if set(context) - allowed:
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
        number = int(context.get("number") or 0)
        if number < 1 or number > 1000000:
            raise WatchIngressError("GITHUB_BROKER_NUMBER_INVALID")
        args["number"] = number
    elif op == "read_workflow_runs":
        allowed = {"operation", "branch"}
        if set(context) - allowed:
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
        branch_value = str(context.get("branch") or "").strip()
        if branch_value:
            args["branch"] = _safe_broker_ref(branch_value, refs, fencing_token)
    elif op == "create_branch":
        allowed = {"operation", "base_ref", "new_branch"}
        if set(context) != allowed:
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
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
        if set(context) - allowed or not {"operation", "branch", "path", "content"} <= set(context):
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
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
            raise WatchIngressError("GITHUB_BROKER_REQUEST_FIELDS_INVALID")
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
    if incoming_checkpoint.checkpoint_number < state.checkpoint.checkpoint_number:
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "task_id": task_id,
            "reason": "MASTER_GATE_CHECKPOINT_ROLLBACK_FORBIDDEN",
        }
    if incoming_checkpoint.checkpoint_number > state.checkpoint.checkpoint_number:
        checkpoint_advanced = store.advance_checkpoint_preserving_runtime(
            incoming_checkpoint,
            expected_current_checkpoint_number=state.checkpoint.checkpoint_number,
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

    if _gate_result_ready_state(state):
        readonly = manus_runtime.task_status_readonly(state.worker_id)
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

    # NEEDS_JAYTEC is an internal orchestration handoff, not an owner boundary.
    # Continue the SAME fenced worker with evidence supplied by GitHub Actions.
    # A cycle that is already resolving this transition must not also inject a
    # new master-gate directive; the terminal/broker transition owns the cycle.
    had_internal_dependency = _needs_jaytec_state(state)
    if had_internal_dependency:
        if not broker_context:
            github_broker = "CONTEXT_REQUIRED"
        else:
            readonly = manus_runtime.task_status_readonly(state.worker_id)
            terminal = (
                readonly.get("result")
                if isinstance(readonly, Mapping)
                and isinstance(readonly.get("result"), Mapping)
                else {}
            )
            readonly_status = str(readonly.get("status") or "")
            terminal_status = str(terminal.get("status") or "")
            dependency_superseded = False

            # A successful handoff may make the same provider task PENDING
            # before the next WATCH cycle. The durable state can still carry
            # the previous NEEDS_JAYTEC marker from an earlier poll. Treat
            # PENDING on the SAME fenced worker as resumed work, not as an
            # invalid terminal result.
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
                # The provider has already advanced to a new terminal result.
                # Re-open the durable state on the same fence so the canonical
                # supervisor below can classify SUCCESS / NEEDS_OWNER /
                # FAILED_CLOSED using one implementation of terminal policy.
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
                requests = _broker_requests(
                    terminal,
                    refs=refs,
                    fencing_token=state.fencing_token,
                    mutation_authorized=mutation_authorized,
                )
                if requests and broker_context.get("kind") != "SPECIALIST_REQUEST_RESULTS":
                    final = store.get(task_id)
                    return {
                        "status": "PASS",
                        "task_id": task_id,
                        "bootstrapped": bootstrapped,
                        "health_refreshed": False,
                        "github_broker": "REQUESTS_REQUIRED",
                        "github_broker_requests": requests,
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
                            "recovery_attempts": (
                                final.recovery_attempts if final else None
                            ),
                            "fencing_token": final.fencing_token if final else None,
                            "progress_marker": (
                                final.progress_marker if final else None
                            ),
                            "completed": final.completed if final else None,
                        },
                    }
                if requests and not _broker_results_match_requests(
                    broker_context, requests
                ):
                    return {
                        "status": "BLOCKED_FAIL_CLOSED",
                        "task_id": task_id,
                        "reason": "GITHUB_BROKER_RESULTS_REQUEST_MISMATCH",
                    }

                handoff_digest = str(broker_context.get("sha256") or "")[:16]
                delivered_marker = "JAYTEC_BROKER_HANDOFF:" + handoff_digest
                if state.progress_marker == delivered_marker:
                    github_broker = "WAITING_FOR_NEW_CONTEXT"
                else:
                    handoff = invoker.continue_existing(
                        checkpoint=state.checkpoint,
                        worker_id=state.worker_id,
                        fencing_token=state.fencing_token,
                        broker_context=broker_context,
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
                        github_broker = "HANDOFF_CONTINUED"
                        final = store.get(task_id)
                        return {
                            "status": "PASS",
                            "task_id": task_id,
                            "bootstrapped": bootstrapped,
                            "health_refreshed": False,
                            "github_broker": github_broker,
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
        readonly = manus_runtime.task_status_readonly(refreshed_state.worker_id)
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
