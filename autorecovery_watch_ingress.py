"""WATCH-only runtime ingress for one canonical JAYTEC autorecovery cycle."""
from __future__ import annotations

import json
import os
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

    supervisor = AutoRecoverySupervisor(
        store=store,
        verifier=verifier,
        invoker=ManusLiteRecoveryInvoker(manus_runtime, registry),
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
