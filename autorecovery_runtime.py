"""Fail-closed runtime gate for JAYTEC WATCH + AUTORECOVERY.

This module makes installation/activation state observable without silently
creating schema or enabling recovery. It deliberately has no mutation endpoint.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

import psycopg2
import psycopg2.extras

from autorecovery_supervisor import (
    AssignmentCheckpoint,
    AutoRecoveryError,
    PostgresAssignmentStore,
    build_continuation_packet,
)

AUTORECOVERY_SCHEMA = "JAYTEC_AUTORECOVERY_RUNTIME_V1"
MAX_RECOVERY_ATTEMPTS = 3


class AutoRecoveryRuntimeError(RuntimeError):
    pass


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    value = str(env.get(name, "0")).strip()
    if value == "1":
        return True
    if value in {"", "0"}:
        return False
    raise AutoRecoveryRuntimeError(name + "_INVALID_BOOLEAN")


def _parse_route_ids(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = str(env.get("JAYTEC_AUTORECOVERY_CALLABLE_ROUTES", "")).strip()
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AutoRecoveryRuntimeError("CALLABLE_ROUTES_JSON_INVALID") from exc
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AutoRecoveryRuntimeError("CALLABLE_ROUTES_INVALID")
    normalized = tuple(dict.fromkeys(item.strip() for item in value))
    if len(normalized) > 16:
        raise AutoRecoveryRuntimeError("CALLABLE_ROUTES_TOO_MANY")
    return normalized


def _safe_mode(value: str, *, allowed: frozenset[str], name: str) -> str:
    normalized = str(value or "").strip()
    if normalized and normalized not in allowed:
        raise AutoRecoveryRuntimeError(name + "_UNSUPPORTED")
    return normalized


@dataclass(frozen=True)
class RuntimeStatus:
    schema_version: str
    installed: bool
    requested_enabled: bool
    active: bool
    mode: str
    database_configured: bool
    schema_ready_declared: bool
    callable_worker_routes: tuple[str, ...]
    checkpoint_verifier: str
    heartbeat_mode: str
    notification_mode: str
    max_recovery_attempts: int
    ui_chat_autoresume_supported: bool
    observer_role: str
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["callable_worker_routes"] = list(self.callable_worker_routes)
        value["blockers"] = list(self.blockers)
        return value


def runtime_status(
    *,
    env: Optional[Mapping[str, str]] = None,
    database_url: str = "",
) -> RuntimeStatus:
    source = dict(os.environ if env is None else env)
    blockers: list[str] = []

    try:
        requested = _env_bool(source, "JAYTEC_AUTORECOVERY_ENABLED")
    except AutoRecoveryRuntimeError as exc:
        requested = False
        blockers.append(str(exc))

    try:
        schema_ready = _env_bool(source, "JAYTEC_AUTORECOVERY_SCHEMA_READY")
    except AutoRecoveryRuntimeError as exc:
        schema_ready = False
        blockers.append(str(exc))

    try:
        routes = _parse_route_ids(source)
    except AutoRecoveryRuntimeError as exc:
        routes = ()
        blockers.append(str(exc))

    try:
        verifier = _safe_mode(
            source.get("JAYTEC_AUTORECOVERY_CHECKPOINT_VERIFIER", ""),
            allowed=frozenset({"github_exact_head_v1"}),
            name="CHECKPOINT_VERIFIER",
        )
    except AutoRecoveryRuntimeError as exc:
        verifier = ""
        blockers.append(str(exc))

    try:
        heartbeat = _safe_mode(
            source.get("JAYTEC_AUTORECOVERY_HEARTBEAT_MODE", ""),
            allowed=frozenset({"fenced_postgres_v1"}),
            name="HEARTBEAT_MODE",
        )
    except AutoRecoveryRuntimeError as exc:
        heartbeat = ""
        blockers.append(str(exc))

    try:
        notification = _safe_mode(
            source.get("JAYTEC_AUTORECOVERY_NOTIFICATION_MODE", ""),
            allowed=frozenset({"event_log_v1", "jay_notification_v1"}),
            name="NOTIFICATION_MODE",
        )
    except AutoRecoveryRuntimeError as exc:
        notification = ""
        blockers.append(str(exc))

    db_configured = bool(str(database_url or "").strip())

    if requested:
        if not db_configured:
            blockers.append("DATABASE_URL_NOT_CONFIGURED")
        if not schema_ready:
            blockers.append("AUTORECOVERY_SCHEMA_NOT_READY")
        if not routes:
            blockers.append("NO_JAYTEC_CALLABLE_WORKER_ROUTE")
        if not verifier:
            blockers.append("CHECKPOINT_VERIFIER_NOT_CONFIGURED")
        if not heartbeat:
            blockers.append("HEARTBEAT_MODE_NOT_CONFIGURED")
        if not notification:
            blockers.append("NOTIFICATION_MODE_NOT_CONFIGURED")

    active = bool(requested and not blockers)
    mode = (
        "ACTIVE_CALLABLE_WORKERS_ONLY"
        if active
        else ("BLOCKED_FAIL_CLOSED" if requested else "DISABLED")
    )

    return RuntimeStatus(
        schema_version=AUTORECOVERY_SCHEMA,
        installed=True,
        requested_enabled=requested,
        active=active,
        mode=mode,
        database_configured=db_configured,
        schema_ready_declared=schema_ready,
        callable_worker_routes=routes,
        checkpoint_verifier=verifier,
        heartbeat_mode=heartbeat,
        notification_mode=notification,
        max_recovery_attempts=MAX_RECOVERY_ATTEMPTS,
        ui_chat_autoresume_supported=False,
        observer_role="READ_ONLY_GITHUB_EVIDENCE",
        blockers=tuple(blockers),
    )


def validate_checkpoint_payload(payload: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AutoRecoveryRuntimeError("CHECKPOINT_JSON_INVALID") from exc
    else:
        raw = dict(payload)

    if not isinstance(raw, Mapping):
        raise AutoRecoveryRuntimeError("CHECKPOINT_ROOT_INVALID")
    try:
        checkpoint = AssignmentCheckpoint.from_mapping(raw)
    except (AutoRecoveryError, ValueError, TypeError) as exc:
        raise AutoRecoveryRuntimeError(
            "CHECKPOINT_VALIDATION_FAILED:" + str(exc)
        ) from exc

    packet = build_continuation_packet(checkpoint)
    return {
        "status": "VALID",
        "task_id": checkpoint.task_id,
        "checkpoint_number": checkpoint.checkpoint_number,
        "repo": checkpoint.repo,
        "branch": checkpoint.branch,
        "verified_head": checkpoint.commit_head,
        "worker_specialist_preference": list(
            checkpoint.worker_specialist_preference
        ),
        "continuation_instruction": packet["instruction"],
        "automatic_recovery_authorized": False,
        "note": (
            "Validation proves checkpoint structure only. It does not register "
            "the assignment or grant recovery authority."
        ),
    }


def schema_probe(database_url: str) -> dict[str, Any]:
    """Read-only probe. Never creates or migrates schema."""

    if not str(database_url or "").strip():
        return {
            "status": "BLOCKED",
            "schema_present": False,
            "reason": "DATABASE_URL_NOT_CONFIGURED",
        }
    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                      to_regclass('public.jaytec_assignment_state')::text
                        AS assignment_state_table
                    """
                )
                row = cur.fetchone() or {}
                present = bool(row.get("assignment_state_table"))
                return {
                    "status": "PASS" if present else "NOT_INITIALIZED",
                    "schema_present": present,
                    "table": row.get("assignment_state_table"),
                }
    except Exception as exc:
        return {
            "status": "FAILED_CLOSED",
            "schema_present": False,
            "reason": "SCHEMA_PROBE_ERROR:" + type(exc).__name__,
        }


def assignment_status(
    database_url: str,
    task_id: str,
    *,
    schema_ready_declared: bool,
) -> dict[str, Any]:
    """Read-only canonical state inspection. No schema creation or recovery."""

    if not schema_ready_declared:
        return {
            "status": "BLOCKED",
            "task_id": task_id,
            "reason": "AUTORECOVERY_SCHEMA_NOT_READY",
        }
    if not str(database_url or "").strip():
        return {
            "status": "BLOCKED",
            "task_id": task_id,
            "reason": "DATABASE_URL_NOT_CONFIGURED",
        }
    try:
        state = PostgresAssignmentStore(database_url).get(task_id)
    except Exception as exc:
        return {
            "status": "FAILED_CLOSED",
            "task_id": task_id,
            "reason": "ASSIGNMENT_READ_ERROR:" + type(exc).__name__,
        }
    if state is None:
        return {
            "status": "NOT_FOUND",
            "task_id": task_id,
        }
    return {
        "status": "FOUND",
        "task_id": state.task_id,
        "objective": state.checkpoint.objective,
        "current_phase": state.checkpoint.current_phase,
        "stop_reason": state.stop_reason.value,
        "worker_kind": state.worker_kind.value,
        "worker_id": state.worker_id,
        "worker_route": state.worker_route,
        "checkpoint_number": state.checkpoint.checkpoint_number,
        "repo": state.checkpoint.repo,
        "branch": state.checkpoint.branch,
        "verified_head": state.checkpoint.commit_head,
        "recovery_attempts": state.recovery_attempts,
        "fencing_token": state.fencing_token,
        "lease_active": bool(
            state.lease_owner
            and state.lease_expires_at
        ),
        "last_heartbeat_at": (
            state.last_heartbeat_at.isoformat()
            if state.last_heartbeat_at
            else None
        ),
        "last_progress_at": (
            state.last_progress_at.isoformat()
            if state.last_progress_at
            else None
        ),
        "progress_marker": state.progress_marker,
        "completed": state.completed,
        "last_error": state.last_error,
        "next_intended_action": state.checkpoint.next_intended_action,
    }


__all__ = [
    "AUTORECOVERY_SCHEMA",
    "MAX_RECOVERY_ATTEMPTS",
    "AutoRecoveryRuntimeError",
    "RuntimeStatus",
    "assignment_status",
    "runtime_status",
    "schema_probe",
    "validate_checkpoint_payload",
]
