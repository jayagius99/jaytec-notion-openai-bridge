from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

import psycopg2
import psycopg2.extras


SOURCE = "JAYTEC_DURABLE_RUNTIME"
RUNTIME_ID = "JAYTEC_RELIABILITY_RUNTIME_V1"
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

RECOVERY_EVENT_TYPES = frozenset(
    {
        "TASK_PACKET_REQUEUED",
        "GUARDIAN_STALE_EXECUTION_CONTAINED",
        "GUARDIAN_DUPLICATE_CONTAINED",
    }
)

_SECRET_TEXT = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|credential|authorization|bearer)\s*[:=]\s*[^\s,;]+"
)


def normalize_limit(limit: int = DEFAULT_LIMIT) -> int:
    """Return the bounded read-model page size; never allow an unbounded query."""
    if type(limit) is not int:
        raise ValueError("limit must be an integer")
    return max(1, min(limit, MAX_LIMIT))


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    return str(value)


def _safe_text(value: Any, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    text = _SECRET_TEXT.sub("[REDACTED]", text)
    return text[:160]


def _json_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _safe_specialist_plan(value: Any) -> Optional[list[str]]:
    scope = _json_object(value)
    raw = scope.get("specialists")
    if not isinstance(raw, (list, tuple)):
        return None
    result = []
    for item in raw:
        name = str(item).strip().lower()
        if name in {"codex", "gemini"} and name not in result:
            result.append(name)
    return result


def status_category(job_status: Any, packet_status: Any = None) -> tuple[str, bool]:
    """Map durable status to the explicit V1 workload categories."""
    status = str(job_status or packet_status or "").upper()
    if status == "RUNNING":
        return "Active/RUNNING", True
    if status == "QUEUED":
        return "Queued", True
    if status in {"BLOCKED", "PAUSED"}:
        return "Blocked-or-Paused", True
    if status == "FAILED_SAFE":
        return "Failed unresolved", True
    if status == "SUCCEEDED":
        return "Completed/SUCCEEDED", True
    return "Unknown", False


def _recovery_events(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("recovery_event_types") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(item) for item in raw if str(item) in RECOVERY_EVENT_TYPES})


def _retry_summary(row: Mapping[str, Any]) -> str:
    attempt = int(row.get("attempt_count") or 0)
    maximum = int(row.get("max_attempts") or 0)
    next_attempt = _iso(row.get("next_attempt_at"))
    if maximum and attempt >= maximum and str(row.get("job_status") or "").upper() == "FAILED_SAFE":
        return f"Retry budget exhausted ({attempt}/{maximum}); no automatic retry."
    if next_attempt:
        return f"Bounded retry scheduled for {next_attempt} ({attempt}/{maximum or '?'} attempts)."
    return f"No retry scheduled ({attempt}/{maximum or '?'} attempts)."


def _next_action(row: Mapping[str, Any], category: str, recovery_events: list[str]) -> str:
    status = str(row.get("job_status") or row.get("packet_status") or "").upper()
    if status == "RUNNING":
        if row.get("lease_owner") and row.get("lease_expires_at"):
            return "Monitor the active leased worker until terminal state."
        return "Verify worker and lease health before treating this as active."
    if status == "QUEUED":
        return "Wait for the durable worker to claim the assignment."
    if status == "PAUSED":
        return "Wait for the bounded retry window, then re-evaluate safely."
    if status == "BLOCKED":
        return "Resolve the authoritative blocker; do not blindly retry."
    if status == "FAILED_SAFE":
        if recovery_events:
            return "Review recovery evidence and confirm whether semantic follow-up is still required."
        return "Escalate the unresolved failed-safe assignment for semantic review."
    if status == "SUCCEEDED":
        return "No action; retain the completed assignment as evidence."
    if category == "Unknown":
        return "Reconcile the unsupported durable status before taking action."
    return "Inspect authoritative durable state before taking action."


def _row_projection(row: Mapping[str, Any]) -> Dict[str, Any]:
    category, supported = status_category(row.get("job_status"), row.get("packet_status"))
    recovery_events = _recovery_events(row)
    status = str(row.get("job_status") or row.get("packet_status") or "UNKNOWN").upper()
    blockers = row.get("blockers")
    blocker_present = bool(blockers) or status in {"BLOCKED", "PAUSED", "FAILED_SAFE"} or str(row.get("health") or "").upper() in {"BLOCKED", "FAILED_SAFE"}
    lease_expires = _iso(row.get("lease_expires_at"))
    return {
        "job_id": _safe_text(row.get("job_id")),
        "task_id": _safe_text(row.get("task_id")),
        "subtask_id": _safe_text(row.get("subtask_id")),
        "friendly_title": _safe_text(row.get("objective"), fallback="Untitled assignment"),
        "objective": _safe_text(row.get("objective"), fallback="Untitled assignment"),
        "job_status": status,
        "packet_status": _safe_text(row.get("packet_status"), fallback="UNKNOWN"),
        "lifecycle_category": category,
        "lifecycle_category_supported": supported,
        "health": _safe_text(row.get("health"), fallback="UNKNOWN").upper(),
        "priority": int(row.get("priority") or 0),
        "updated_at": _iso(row.get("updated_at")),
        "next_attempt_at": _iso(row.get("next_attempt_at")),
        "lease": {
            "owner": _safe_text(row.get("lease_owner")) or None,
            "expires_at": lease_expires,
            "active": bool(row.get("lease_owner") and lease_expires),
            "ownership_epoch": int(row.get("ownership_epoch") or 0),
            "fence_token": int(row.get("fence_token") or 0),
        },
        "attempt_count": int(row.get("attempt_count") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "specialist_plan": _safe_specialist_plan(row.get("resource_scope")),
        "next_action": _next_action(row, category, recovery_events),
        "retry_summary": _retry_summary(row),
        "blocker_summary": (
            "Authoritative blocker metadata is present; semantic review is required."
            if blocker_present
            else "No active blocker indicated by the durable job state."
        ),
        "recovery": {
            "supported": True,
            "recovered_or_fixed": bool(recovery_events),
            "event_types": recovery_events,
        },
        "source_shared_state_version": int(row.get("source_shared_state_version") or 0),
    }


def build_workload_snapshot(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: int = DEFAULT_LIMIT,
    observed_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    bounded = normalize_limit(limit)
    selected = list(rows)[:bounded]
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    projected = [_row_projection(row) for row in selected]
    counts: Dict[str, int] = {
        "Active/RUNNING": 0,
        "Queued": 0,
        "Blocked-or-Paused": 0,
        "Failed unresolved": 0,
        "Recovered/Fixed": 0,
        "Completed/SUCCEEDED": 0,
        "Unknown": 0,
    }
    for item in projected:
        counts[item["lifecycle_category"]] = counts.get(item["lifecycle_category"], 0) + 1
        if item["recovery"]["recovered_or_fixed"]:
            counts["Recovered/Fixed"] += 1

    versions = sorted({item["source_shared_state_version"] for item in projected if item["source_shared_state_version"] > 0})
    categories = {
        name: {
            "count": counts.get(name, 0),
            "supported": name != "Unknown",
        }
        for name in (
            "Active/RUNNING",
            "Queued",
            "Blocked-or-Paused",
            "Failed unresolved",
            "Recovered/Fixed",
            "Completed/SUCCEEDED",
        )
    }
    categories["Unknown"] = {"count": counts["Unknown"], "supported": False}
    return {
        "source": SOURCE,
        "runtime_id": RUNTIME_ID,
        "observed_at_utc": observed.astimezone(timezone.utc).isoformat(),
        "freshness": {
            "observed_at_utc": observed.astimezone(timezone.utc).isoformat(),
            "scope": "current_and_recent",
            "bounded_limit": bounded,
        },
        "provenance": {
            "source": SOURCE,
            "runtime_id": RUNTIME_ID,
            "shared_state_version_supported": True,
            "shared_state_versions": versions,
        },
        "summary": {
            "counts": categories,
            "recovered_fixed_is_an_evidence_overlay": True,
        },
        "assignments": projected,
        "returned_count": len(projected),
        "limit": bounded,
    }


class WorkloadReadModel:
    """Read-only bounded projection over the existing durable runtime schema."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def snapshot(self, *, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
        bounded = normalize_limit(limit)
        sql = """
            SELECT
              j.job_id,j.task_id,j.subtask_id,j.objective,
              j.status AS job_status,j.health,j.priority,
              j.updated_at,j.next_attempt_at,j.lease_owner,j.lease_expires_at,
              j.ownership_epoch,j.fence_token,j.source_shared_state_version,
              j.resource_scope,j.blockers,
              p.status AS packet_status,p.attempt_count,p.max_attempts,
              COALESCE((
                SELECT array_agg(DISTINCT e.event_type ORDER BY e.event_type)
                FROM jaytec_job_events e
                WHERE e.job_id=j.job_id
                  AND e.event_type = ANY(%s)
              ), ARRAY[]::TEXT[]) AS recovery_event_types
            FROM jaytec_jobs j
            LEFT JOIN jaytec_task_packets p ON p.job_id=j.job_id
            ORDER BY j.updated_at DESC,j.priority ASC,j.job_id DESC
            LIMIT %s
        """
        with self._connect() as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (list(RECOVERY_EVENT_TYPES), bounded))
                rows = cur.fetchall()
        return build_workload_snapshot(rows, limit=bounded)
