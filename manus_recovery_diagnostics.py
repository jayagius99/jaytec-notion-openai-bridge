"""Read-only diagnostics for already-persisted Manus recovery attempts.

This module never invokes Manus, acquires recovery leases, updates assignment
state, or mutates the idempotency registry. It only projects a strict safe
allowlist from an existing execution_registry row.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

MAX_ROWS = 3
MAX_TASK_ID = 200
MAX_ATTEMPT = 1000

_SAFE_RESULT_FIELDS = (
    "schema_version",
    "status",
    "error",
    "provider_task_id",
    "requested_profile",
    "observed_profile_verified",
)


class ManusRecoveryDiagnosticError(RuntimeError):
    pass


def recovery_key_prefix(task_id: str, attempt: int) -> str:
    task = str(task_id or "").strip()
    if not task or len(task) > MAX_TASK_ID:
        raise ManusRecoveryDiagnosticError("DIAGNOSTIC_TASK_ID_INVALID")
    if type(attempt) is not int or attempt < 1 or attempt > MAX_ATTEMPT:
        raise ManusRecoveryDiagnosticError("DIAGNOSTIC_ATTEMPT_INVALID")
    return f"manus:{task}:recovery:{attempt}:"


def safe_result_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise ManusRecoveryDiagnosticError("DIAGNOSTIC_RESULT_INVALID")
    safe = {key: result.get(key) for key in _SAFE_RESULT_FIELDS if key in result}
    # Never echo arbitrary nested/provider payloads. Only known scalar fields
    # from runtime_error_payload()/STARTED metadata are retained.
    for key, value in list(safe.items()):
        if value is not None and not isinstance(value, (str, bool, int, float)):
            safe[key] = str(value)[:256]
        elif isinstance(value, str):
            safe[key] = value[:512]
    return safe


def read_saved_manus_recovery_result(
    database_url: str,
    *,
    task_id: str,
    attempt: int,
) -> dict[str, Any]:
    if not str(database_url or "").strip():
        return {
            "status": "BLOCKED",
            "reason": "DATABASE_URL_NOT_CONFIGURED",
        }

    prefix = recovery_key_prefix(task_id, attempt)
    try:
        with psycopg2.connect(database_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT idempotency_key, result_json, created_at
                    FROM execution_registry
                    WHERE idempotency_key LIKE %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (prefix + "%", MAX_ROWS),
                )
                rows = list(cur.fetchall() or [])
    except Exception as exc:
        return {
            "status": "FAILED_CLOSED",
            "reason": "MANUS_DIAGNOSTIC_READ_ERROR:" + type(exc).__name__,
        }

    if not rows:
        return {
            "status": "NOT_FOUND",
            "task_id": task_id,
            "attempt": attempt,
        }
    if len(rows) != 1:
        return {
            "status": "FAILED_CLOSED",
            "task_id": task_id,
            "attempt": attempt,
            "reason": "MULTIPLE_MANUS_RECOVERY_RECORDS",
            "record_count": len(rows),
        }

    row = rows[0]
    raw = row.get("result_json")
    try:
        result = json.loads(str(raw or ""))
    except Exception:
        return {
            "status": "FAILED_CLOSED",
            "task_id": task_id,
            "attempt": attempt,
            "reason": "MANUS_DIAGNOSTIC_RESULT_JSON_INVALID",
        }
    if not isinstance(result, Mapping):
        return {
            "status": "FAILED_CLOSED",
            "task_id": task_id,
            "attempt": attempt,
            "reason": "MANUS_DIAGNOSTIC_RESULT_INVALID",
        }

    created_at = row.get("created_at")
    if isinstance(created_at, datetime):
        created_text = created_at.isoformat()
    else:
        created_text = str(created_at) if created_at is not None else None

    return {
        "status": "FOUND",
        "task_id": task_id,
        "attempt": attempt,
        "created_at": created_text,
        "result": safe_result_projection(result),
        "read_only": True,
    }


__all__ = [
    "ManusRecoveryDiagnosticError",
    "read_saved_manus_recovery_result",
    "recovery_key_prefix",
    "safe_result_projection",
]
