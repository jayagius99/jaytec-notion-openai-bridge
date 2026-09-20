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
)
from autorecovery_runtime import runtime_status, schema_probe
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
_BROKER_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|credential|token)",
    re.I,
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



def _broker_has_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _BROKER_SECRET_KEY.search(str(key)):
                return True
            if _broker_has_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_broker_has_secret_key(item) for item in value)
    return False


def _broker_context(value: Any, refs: Mapping[str, str]) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_INVALID")
    context = dict(value)
    required = {
        "schema_version",
        "kind",
        "repo",
        "refs",
        "issues",
        "pull_requests",
        "open_pull_requests",
        "authority",
        "sha256",
    }
    if set(context) != required:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_FIELDS_INVALID")
    if context.get("schema_version") != "JAYTEC_GITHUB_BROKER_CONTEXT_V1":
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_VERSION_INVALID")
    if context.get("kind") not in {
        "PRIVATE_REPO_BOOTSTRAP",
        "SPECIALIST_REQUEST_RESULTS",
    }:
        raise WatchIngressError("GITHUB_BROKER_CONTEXT_KIND_INVALID")
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


def _needs_jaytec_state(state: Any) -> bool:
    if state is None or not getattr(state, "worker_id", None):
        return False
    marker = str(getattr(state, "last_error", "") or "")
    return marker.startswith("MANUS_TERMINAL:NEEDS_JAYTEC:")



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
    if state is None:
        raw_checkpoint = payload.get("bootstrap_checkpoint")
        if not isinstance(raw_checkpoint, Mapping):
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "BOOTSTRAP_CHECKPOINT_REQUIRED",
            }
        checkpoint = AssignmentCheckpoint.from_mapping(raw_checkpoint)
        if checkpoint.task_id != task_id:
            raise WatchIngressError("BOOTSTRAP_TASK_ID_MISMATCH")
        verified, detail = verifier.verify(checkpoint)
        if not verified:
            return {
                "status": "BLOCKED_FAIL_CLOSED",
                "task_id": task_id,
                "reason": "BOOTSTRAP_CHECKPOINT_NOT_ATTESTED",
                "detail": detail,
            }
        store.upsert_checkpoint(
            checkpoint,
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

    invoker = ManusLiteRecoveryInvoker(
        manus_runtime,
        registry,
        broker_context=broker_context,
    )
    github_broker = "AVAILABLE" if broker_context else "NONE"

    # NEEDS_JAYTEC is an internal orchestration handoff, not an owner boundary.
    # Continue the SAME fenced worker with evidence supplied by GitHub Actions.
    if _needs_jaytec_state(state):
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
            if (
                readonly.get("status") != "VERIFIED_COMPLETE"
                or terminal.get("status") != "NEEDS_JAYTEC"
            ):
                return {
                    "status": "BLOCKED_FAIL_CLOSED",
                    "task_id": task_id,
                    "reason": "NEEDS_JAYTEC_TERMINAL_RESULT_NOT_VERIFIED",
                }

            handoff_digest = str(broker_context.get("sha256") or "")[:16]
            delivered_marker = "JAYTEC_BROKER_HANDOFF:" + handoff_digest
            if state.progress_marker == delivered_marker:
                github_broker = "WAITING_FOR_NEW_CONTEXT"
            else:
                claim_marker = "JAYTEC_BROKER_HANDOFF_CLAIM:" + handoff_digest
                store.heartbeat(
                    task_id,
                    fencing_token=state.fencing_token,
                    worker_id=state.worker_id,
                    progress_marker=claim_marker,
                )
                handoff = invoker.continue_existing(
                    checkpoint=state.checkpoint,
                    worker_id=state.worker_id,
                    fencing_token=state.fencing_token,
                    broker_context=broker_context,
                )
                if handoff.accepted:
                    store.heartbeat(
                        task_id,
                        fencing_token=state.fencing_token,
                        worker_id=state.worker_id,
                        progress_marker=delivered_marker,
                    )
                    github_broker = "HANDOFF_CONTINUED"
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

    supervisor = AutoRecoverySupervisor(
        store=store,
        verifier=verifier,
        invoker=invoker,
        health_probe=ManusLiteHealthProbe(manus_runtime),
        notifier=JsonLogRecoveryNotifier(),
        instance_id="github-watch-cycle",
    )

    refreshed = supervisor.refresh_worker_health(task_id)
    decision = supervisor.tick(task_id)
    final = store.get(task_id)
    return {
        "status": "PASS",
        "task_id": task_id,
        "bootstrapped": bootstrapped,
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
        },
    }


__all__ = [
    "FORGE_TASK_ID",
    "WatchIngressError",
    "execute_watch_cycle",
    "prepare_schema_if_authorized",
]
