"""Concrete JAYTEC_CALLABLE runtime components for WATCH + AUTORECOVERY."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from autorecovery_supervisor import (
    AssignmentCheckpoint,
    RecoveryRoute,
    WorkerHealth,
    WorkerInvocation,
)
from manus_runtime import ManusLiteRuntime

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
CALLABLE_ROUTE_ID = "jaytec-manus-lite-v1"
EXPECTED_REPO = "jayagius99/jaytec-work-engine-v2-g1"

HARD_RECOVERY_CONSTRAINTS = (
    "Resume — do not recreate completed work.",
    "Use GitHub only for this recovery task; do not use Notion Agent.",
    "Do not spend money, buy credits, top up, subscribe, or enable paid fallback.",
    "Manus profile must remain Lite and must be provider-observed as Lite.",
    "Do not merge any pull request.",
    "Do not modify or force-update security/root-owner-control-v1.",
    "Do not modify the head branch of Genesis PR #58.",
    "Do not activate Forge and do not create GENESIS_EVENT_0001.",
    "Do not change ROOT_OWNER identity, recovery credentials, hardware-key state, secrets, or credential stores.",
    "Do not weaken security gates, audit gates, fencing, leases, or owner authority.",
    "Prefer isolated feature branches and pull requests for every code mutation.",
    "Append meaningful execution evidence to issue #66 and canonical progress/checkpoints to issue #59 when GitHub access permits.",
    "Continue all safely possible software-only preparation until a genuine owner, physical, credential, spend, or inaccessible-source boundary is reached.",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ObservedRefsCheckpointVerifier:
    """Verify a checkpoint against refs observed by the OIDC-authenticated WATCH run."""

    def __init__(
        self,
        observed_refs: Mapping[str, str],
        *,
        expected_repo: str = EXPECTED_REPO,
    ) -> None:
        refs: dict[str, str] = {}
        for raw_ref, raw_sha in dict(observed_refs).items():
            ref = str(raw_ref or "").strip()
            sha = str(raw_sha or "").strip().lower()
            if not ref or len(ref) > 300 or not SHA40_RE.fullmatch(sha):
                raise ValueError("OBSERVED_REF_INVALID")
            refs[ref] = sha
        if not refs:
            raise ValueError("OBSERVED_REFS_EMPTY")
        self.observed_refs = refs
        self.expected_repo = expected_repo

    def verify(self, checkpoint: AssignmentCheckpoint) -> tuple[bool, str]:
        if checkpoint.repo != self.expected_repo:
            return False, "CHECKPOINT_REPOSITORY_MISMATCH"
        observed = self.observed_refs.get(checkpoint.branch)
        if observed is None:
            return False, "CHECKPOINT_BRANCH_NOT_ATTESTED"
        if observed != checkpoint.commit_head.lower():
            return False, "CHECKPOINT_HEAD_MISMATCH"
        return True, "GITHUB_OIDC_ATTESTED_EXACT_HEAD"


def _authority(checkpoint: AssignmentCheckpoint) -> tuple[list[str], dict[str, str], bool]:
    envelope = dict(checkpoint.authority_envelope or {})
    actions = envelope.get("allowed_actions")
    connectors = envelope.get("connector_purposes")
    mutation = envelope.get("connector_mutation_authorized")

    if not isinstance(actions, list) or not actions or not all(
        isinstance(item, str) and item.strip() for item in actions
    ):
        raise ValueError("RECOVERY_ALLOWED_ACTIONS_INVALID")
    if not isinstance(connectors, Mapping):
        raise ValueError("RECOVERY_CONNECTOR_PURPOSES_INVALID")
    normalized = {str(k).strip(): str(v).strip().casefold() for k, v in connectors.items()}
    if set(normalized) - {"github"}:
        raise ValueError("RECOVERY_CONNECTOR_SCOPE_TOO_BROAD")
    if normalized.get("github") not in {"read", "inspect", "diagnose", "test", "write"}:
        raise ValueError("RECOVERY_GITHUB_PURPOSE_INVALID")
    if type(mutation) is not bool:
        raise ValueError("RECOVERY_MUTATION_AUTH_INVALID")
    if normalized.get("github") == "write" and mutation is not True:
        raise ValueError("RECOVERY_WRITE_REQUIRES_MUTATION_AUTH")
    return [item.strip() for item in actions], normalized, mutation


def master_gate_handoff_id(
    checkpoint: AssignmentCheckpoint,
    fencing_token: int,
    gate_context: Mapping[str, Any],
) -> str:
    gate_id = str(gate_context.get("gate_id") or "").strip()
    graph_sha = str(gate_context.get("graph_sha256") or "").strip().lower()
    checkpoint_number = int(gate_context.get("checkpoint_number") or 0)
    if (
        not re.fullmatch(r"G[0-9]{2,4}", gate_id)
        or not re.fullmatch(r"[0-9a-f]{64}", graph_sha)
        or checkpoint_number != checkpoint.checkpoint_number
        or fencing_token < 0
    ):
        raise ValueError("MASTER_GATE_CONTEXT_INVALID")
    controller_review_id = str(
        gate_context.get("controller_review_id") or ""
    ).strip().lower()
    if controller_review_id and not re.fullmatch(r"[0-9a-f]{64}", controller_review_id):
        raise ValueError("MASTER_GATE_CONTROLLER_REVIEW_ID_INVALID")
    suffix = (
        ":review:" + controller_review_id[:16]
        if controller_review_id
        else ""
    )
    return (
        f"{checkpoint.task_id}:fence:{fencing_token}:"
        f"master-gate:{gate_id}:{graph_sha[:16]}"
        + suffix
    )


def master_gate_result_has_receipt(
    result: Mapping[str, Any],
    receipt: str,
) -> bool:
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        return False
    for raw in evidence:
        if not isinstance(raw, Mapping):
            continue
        if (
            str(raw.get("kind") or "") == "audit_record"
            and str(raw.get("source") or "") == "JAYTEC_MASTER_GATE_HANDOFF"
            and str(raw.get("reference") or "") == receipt
        ):
            return True
    return False


class ManusLiteRecoveryInvoker:
    """Start one idempotent, fenced Manus Lite continuation worker."""

    def __init__(
        self,
        runtime: ManusLiteRuntime,
        registry: Any,
        *,
        broker_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.broker_context = dict(broker_context or {})

    def invoke(
        self,
        *,
        checkpoint: AssignmentCheckpoint,
        continuation_packet: Mapping[str, Any],
        route: RecoveryRoute,
        fencing_token: int,
    ) -> WorkerInvocation:
        try:
            actions, connectors, mutation = _authority(checkpoint)
            worker_task_id = (
                f"{checkpoint.task_id}:recovery:{fencing_token}:"
                f"{route.value.casefold()}"
            )[:200]
            checkpoint_digest = hashlib.sha256(
                json.dumps(
                    dict(continuation_packet),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            required_context = {
                "parent_task_id": checkpoint.task_id,
                "checkpoint_number": checkpoint.checkpoint_number,
                "repo": checkpoint.repo,
                "branch": checkpoint.branch,
                "verified_head": checkpoint.commit_head,
                "open_pr": checkpoint.open_pr,
                "current_phase": checkpoint.current_phase[:500],
                "last_safe_checkpoint": checkpoint.last_safe_checkpoint[:700],
                "next_intended_action": checkpoint.next_intended_action[:700],
                "completed_work_count": len(checkpoint.completed_work),
                "remaining_work_count": len(checkpoint.remaining_work),
                "known_failures_count": len(checkpoint.known_failures),
                "dependencies_count": len(checkpoint.dependencies),
                "continuation_packet_sha256": checkpoint_digest,
                "fencing_token": fencing_token,
                "recovery_route": route.value,
            }
            if self.broker_context:
                # Do not embed the full private-repository snapshot in a fresh
                # recovery task. The provider message ceiling is intentionally
                # small, and the full broker context is delivered later through
                # the proven NEEDS_JAYTEC same-task handoff path if required.
                required_context["jaytec_private_github_broker"] = {
                    "available": True,
                    "kind": str(self.broker_context.get("kind") or "")[:80],
                    "repo": str(self.broker_context.get("repo") or checkpoint.repo)[:200],
                    "sha256": str(self.broker_context.get("sha256") or "")[:64],
                    "handoff_policy": (
                        "If direct private GitHub evidence is insufficient, "
                        "return NEEDS_JAYTEC. JAYTEC will provide exact bounded "
                        "broker evidence to this same task."
                    ),
                }
            constraints = list(dict.fromkeys(
                [*checkpoint.active_constraints, *HARD_RECOVERY_CONSTRAINTS]
            ))
            request = {
                "task_id": worker_task_id,
                "objective": (
                    checkpoint.objective
                    + "\n\nContinue from the exact checkpoint. Complete as much "
                    "safe software-only preparation as possible before stopping."
                ),
                "scope": "jaytec_delegated_task",
                "authority_source": "chatgpt",
                "current_task_authorized": True,
                "allowed_actions": actions,
                "connector_purposes": connectors,
                "connector_mutation_authorized": mutation,
                "required_context": required_context,
                "constraints": constraints,
                "reference_ids": [
                    f"{checkpoint.repo}@{checkpoint.commit_head}",
                    f"issue:{checkpoint.repo}#59",
                    f"issue:{checkpoint.repo}#66",
                ],
                "title": f"JAYTEC recovery {checkpoint.task_id} fence {fencing_token}",
            }
            result = self.runtime.start_task_idempotent(
                json.dumps(request, sort_keys=True),
                self.registry,
            )
        except Exception as exc:
            return WorkerInvocation(
                accepted=False,
                worker_id=None,
                route=route,
                detail="MANUS_RECOVERY_INVOKE_FAILED:" + type(exc).__name__,
            )

        if result.get("status") != "STARTED":
            status = str(result.get("status") or "UNKNOWN")[:80]
            error = str(result.get("error") or "").strip()
            safe_error = re.sub(r"[^A-Za-z0-9_.:/-]", "_", error)[:240]
            detail = "MANUS_RECOVERY_NOT_STARTED:" + status
            if safe_error:
                detail += ":" + safe_error
            return WorkerInvocation(
                accepted=False,
                worker_id=None,
                route=route,
                detail=detail,
            )
        worker_id = str(result.get("provider_task_id") or "").strip()
        if not worker_id:
            return WorkerInvocation(
                accepted=False,
                worker_id=None,
                route=route,
                detail="MANUS_RECOVERY_WORKER_ID_MISSING",
            )
        if result.get("requested_profile") != "lite" or result.get("observed_profile_verified") is not True:
            return WorkerInvocation(
                accepted=False,
                worker_id=None,
                route=route,
                detail="MANUS_RECOVERY_LITE_IDENTITY_UNVERIFIED",
            )
        return WorkerInvocation(
            accepted=True,
            worker_id=worker_id,
            route=route,
            detail="MANUS_LITE_RECOVERY_STARTED",
        )


    def continue_existing(
        self,
        *,
        checkpoint: AssignmentCheckpoint,
        worker_id: str,
        fencing_token: int,
        broker_context: Mapping[str, Any],
    ) -> WorkerInvocation:
        """Continue one NEEDS_JAYTEC task without consuming recovery budget."""

        try:
            actions, connectors, mutation = _authority(checkpoint)
            digest = hashlib.sha256(
                json.dumps(
                    dict(broker_context),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            result = self.runtime.continue_task_handoff(
                worker_id,
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                connector_purposes=connectors,
                connector_mutation_authorized=mutation,
                handoff_id=(
                    f"{checkpoint.task_id}:fence:{fencing_token}:"
                    f"github-broker:{digest[:16]}"
                ),
                handoff_context=dict(broker_context),
            )
        except Exception as exc:
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_JAYTEC_HANDOFF_FAILED:" + type(exc).__name__,
            )

        if result.get("status") != "CONTINUED":
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_JAYTEC_HANDOFF_NOT_CONTINUED",
            )
        if result.get("provider_task_id") != worker_id:
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_JAYTEC_HANDOFF_WORKER_MISMATCH",
            )
        if (
            result.get("requested_profile") != "lite"
            or result.get("observed_profile_verified") is not True
        ):
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_JAYTEC_HANDOFF_LITE_IDENTITY_UNVERIFIED",
            )
        return WorkerInvocation(
            accepted=True,
            worker_id=worker_id,
            route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
            detail=(
                "MANUS_JAYTEC_HANDOFF_REPLAY"
                if result.get("idempotent_replay") is True
                else "MANUS_JAYTEC_HANDOFF_CONTINUED"
            ),
        )


    def continue_gate_directive(
        self,
        *,
        checkpoint: AssignmentCheckpoint,
        worker_id: str,
        fencing_token: int,
        gate_context: Mapping[str, Any],
    ) -> WorkerInvocation:
        """Steer the same fenced worker onto one canonical master gate.

        The handoff is deterministic/idempotent and consumes no recovery
        attempt. An idempotent replay is healthy: the provider must not receive
        the same directive twice.
        """
        try:
            _actions, connectors, mutation = _authority(checkpoint)
            gate_id = str(gate_context.get("gate_id") or "").strip()
            graph_sha = str(gate_context.get("graph_sha256") or "").strip().lower()
            hid = master_gate_handoff_id(
                checkpoint,
                fencing_token,
                gate_context,
            )
            enriched_context = dict(gate_context)
            enriched_context["result_receipt_requirement"] = {
                "kind": "audit_record",
                "source": "JAYTEC_MASTER_GATE_HANDOFF",
                "reference": hid,
                "instruction": (
                    "Every terminal structured result for this master gate MUST "
                    "include an evidence item with exactly this kind, source and "
                    "reference. Without this receipt JAYTEC treats the result as "
                    "pre-gate/stale and will not accept it."
                ),
            }
            enriched_context["gate_evidence_manifest_requirement"] = {
                "kind": "artifact",
                "source": "JAYTEC_GATE_EVIDENCE_MANIFEST",
                "reference_format": (
                    "github://jayagius99/jaytec-work-engine-v2-g1/"
                    "<40-char-commit-sha>/gate_evidence/"
                    + gate_id
                    + ".json"
                ),
                "instruction": (
                    "Every terminal SUCCESS for this gate MUST include exactly "
                    "one artifact evidence item with this source and a reference "
                    "matching reference_format. The manifest must use "
                    "FORGE_GATE_EVIDENCE_V1 and cover every gate evidence requirement."
                ),
            }
            controller_review_id = str(
                enriched_context.get("controller_review_id") or ""
            ).strip()
            if controller_review_id:
                enriched_context["assignment_controller_redirect"] = {
                    "action": str(
                        enriched_context.get("controller_action") or ""
                    )[:40],
                    "review_id": controller_review_id,
                    "direction": str(
                        enriched_context.get("controller_direction") or ""
                    )[:1600],
                    "instruction": (
                        "This is a ChatGPT assignment-owner directional correction. "
                        "Continue this SAME fenced task from the current gate and "
                        "return fresh evidence/manifest bound to this handoff receipt."
                    ),
                }
            result = self.runtime.continue_task_handoff(
                worker_id,
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                connector_purposes=connectors,
                connector_mutation_authorized=mutation,
                handoff_id=hid,
                handoff_context=enriched_context,
            )
        except Exception as exc:
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_GATE_HANDOFF_FAILED:" + type(exc).__name__,
            )

        if result.get("status") != "CONTINUED":
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_GATE_HANDOFF_NOT_CONTINUED",
            )
        if result.get("provider_task_id") != worker_id:
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_GATE_HANDOFF_WORKER_MISMATCH",
            )
        if (
            result.get("requested_profile") != "lite"
            or result.get("observed_profile_verified") is not True
        ):
            return WorkerInvocation(
                accepted=False,
                worker_id=worker_id,
                route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                detail="MANUS_GATE_HANDOFF_LITE_IDENTITY_UNVERIFIED",
            )
        return WorkerInvocation(
            accepted=True,
            worker_id=worker_id,
            route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
            detail=(
                "MANUS_GATE_HANDOFF_REPLAY"
                if result.get("idempotent_replay") is True
                else "MANUS_GATE_HANDOFF_CONTINUED"
            ),
        )


class ManusLiteHealthProbe:
    """Translate Manus task state into JAYTEC fenced-worker health."""

    def __init__(
        self,
        runtime: ManusLiteRuntime,
        *,
        required_result_receipt: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.required_result_receipt = str(required_result_receipt or "").strip() or None

    def wait_for_healthy(
        self,
        *,
        task_id: str,
        fencing_token: int,
        worker_id: str,
        timeout_seconds: int,
    ) -> WorkerHealth:
        try:
            status = self.runtime.task_status(worker_id)
        except Exception as exc:
            return WorkerHealth(
                healthy=False,
                worker_id=worker_id,
                heartbeat_at=None,
                progress_marker=None,
                detail="MANUS_HEALTH_FAILED:" + type(exc).__name__,
            )

        state = str(status.get("status") or "UNKNOWN")
        if state == "PENDING":
            return WorkerHealth(
                healthy=True,
                worker_id=worker_id,
                heartbeat_at=_now(),
                progress_marker="MANUS_PENDING",
                detail="MANUS_LITE_TASK_PENDING",
            )
        if state == "VERIFIED_COMPLETE":
            result = status.get("result") if isinstance(status.get("result"), Mapping) else {}
            if (
                self.required_result_receipt is not None
                and not master_gate_result_has_receipt(
                    result,
                    self.required_result_receipt,
                )
            ):
                return WorkerHealth(
                    healthy=True,
                    worker_id=worker_id,
                    heartbeat_at=_now(),
                    progress_marker="MANUS_PENDING_GATE_RECEIPT",
                    detail="STALE_PRE_GATE_TERMINAL_IGNORED",
                )
            result_state = str(result.get("status") or "UNKNOWN")
            digest = hashlib.sha256(
                json.dumps(result, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
            return WorkerHealth(
                healthy=True,
                worker_id=worker_id,
                heartbeat_at=_now(),
                progress_marker=f"MANUS_TERMINAL:{result_state}:{digest}",
                detail="MANUS_LITE_TASK_VERIFIED_COMPLETE",
            )
        return WorkerHealth(
            healthy=False,
            worker_id=worker_id,
            heartbeat_at=None,
            progress_marker=None,
            detail="MANUS_TASK_UNHEALTHY:" + state,
        )


class JsonLogRecoveryNotifier:
    def notify(self, event: Mapping[str, Any]) -> None:
        safe = {
            str(k): v
            for k, v in dict(event).items()
            if "token" not in str(k).casefold()
            and "secret" not in str(k).casefold()
            and "credential" not in str(k).casefold()
        }
        print(
            "JAYTEC_AUTORECOVERY_EVENT="
            + json.dumps(safe, sort_keys=True, default=str),
            flush=True,
        )


__all__ = [
    "CALLABLE_ROUTE_ID",
    "EXPECTED_REPO",
    "HARD_RECOVERY_CONSTRAINTS",
    "JsonLogRecoveryNotifier",
    "ManusLiteHealthProbe",
    "ManusLiteRecoveryInvoker",
    "master_gate_handoff_id",
    "master_gate_result_has_receipt",
    "ObservedRefsCheckpointVerifier",
]
