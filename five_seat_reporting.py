from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import psycopg2
import psycopg2.extras


REPORT_SCHEMA = "JAYTEC_WATCH_LAST_60_MINUTES_V1"
SEAT_IDS = tuple(f"WORKER-SEAT-{i}" for i in range(1, 6))
OWNER_EVENT_TYPES = frozenset({"OWNER_NOTIFICATION_REQUIRED"})
RECOVERY_EVENT_TYPES = frozenset(
    {
        "FABRIC_SAFE_RETRY_QUEUED",
        "FABRIC_JOB_QUARANTINED",
        "WORKER_HANDOFF_QUARANTINED_AND_SEAT_RELEASED",
        "GUARDIAN_STALE_EXECUTION_CONTAINED",
        "GUARDIAN_DUPLICATE_CONTAINED",
    }
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    return str(value)


def _text(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _obj(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _next_action(status: str, fabric_state: str, blocker_present: bool) -> str:
    state = str(fabric_state or "").upper()
    raw = str(status or "").upper()
    if state == "SUCCEEDED" or raw == "SUCCEEDED":
        return "No action; retain WATCH-attested evidence."
    if state in {"QUARANTINED", "ESCALATED", "BLOCKED_OWNER"}:
        return "Resolve the durable blocker before any retry or reassignment."
    if state in {"HANDOFF_PENDING_REVIEW", "REVIEWING"}:
        return "WATCH must review the immutable handoff."
    if state in {"QUEUED", "REWORK_QUEUED"} or raw == "QUEUED":
        return "Wait for the next compatible fenced seat claim."
    if state in {"RUNNING", "CLAIMED"} or raw == "RUNNING":
        return "Continue under the current lease/fence; do not duplicate."
    if blocker_present:
        return "Reconcile the authoritative blocker before continuing."
    return "Reconcile durable state before taking another action."


class FiveSeatReporter:
    """Read-only delta report over WATCH + exactly five durable worker seats."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def last_60_minutes(self, *, window_minutes: int = 60) -> dict[str, Any]:
        minutes = int(window_minutes)
        if minutes < 1 or minutes > 240:
            raise ValueError("window_minutes must be between 1 and 240")

        with self._connect() as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT now() AS observed_at")
                observed_at = cur.fetchone()["observed_at"]

                cur.execute(
                    """
                    SELECT controller_id,lease_owner,lease_expires_at,
                           leader_epoch,fence_token,version,updated_at
                    FROM jaytec_watch_leader
                    WHERE controller_id='WATCH'
                    """
                )
                leader = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT s.seat_id,s.state,s.worker_id,s.lease_owner,
                           s.lease_expires_at,s.seat_epoch,s.fence_token,
                           s.last_handoff_ref,s.updated_at,
                           j.job_id,j.task_id,j.objective,j.status,j.fabric_state,
                           j.created_at AS job_created_at,j.updated_at AS job_updated_at,
                           j.blockers,e.worker_kind,e.cost_policy
                    FROM jaytec_worker_seats s
                    LEFT JOIN jaytec_jobs j ON j.job_id=s.current_job_id
                    LEFT JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    ORDER BY s.seat_id
                    """
                )
                seat_rows = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT e.event_id,e.job_id,e.event_type,e.source,e.payload,e.created_at,
                           j.task_id,j.objective,j.status,j.fabric_state,j.blockers
                    FROM jaytec_job_events e
                    LEFT JOIN jaytec_jobs j ON j.job_id=e.job_id
                    WHERE e.created_at >= now() - (%s * interval '1 minute')
                    ORDER BY e.event_id ASC
                    LIMIT 4000
                    """,
                    (minutes,),
                )
                events = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT r.review_id,r.job_id,r.decision,r.reason,r.evidence,r.created_at,
                           j.task_id,j.objective,j.status,j.fabric_state
                    FROM jaytec_watch_reviews r
                    JOIN jaytec_jobs j ON j.job_id=r.job_id
                    WHERE r.created_at >= now() - (%s * interval '1 minute')
                    ORDER BY r.created_at ASC,r.review_id ASC
                    LIMIT 1000
                    """,
                    (minutes,),
                )
                reviews = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT j.job_id,j.task_id,j.objective,j.status,j.fabric_state,
                           j.priority,j.blockers,j.updated_at,
                           e.worker_kind,e.cost_policy
                    FROM jaytec_jobs j
                    JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    WHERE j.fabric_state IN (
                      'QUEUED','REWORK_QUEUED','BLOCKED_OWNER',
                      'BLOCKED_DEPENDENCY','QUARANTINED','ESCALATED'
                    )
                    ORDER BY j.priority ASC,j.updated_at ASC,j.job_id ASC
                    LIMIT 200
                    """
                )
                queued = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT worker_kind,state,consecutive_failures,failure_threshold,
                           open_until,probe_job_id,last_failure,updated_at
                    FROM jaytec_fabric_circuits
                    WHERE state <> 'CLOSED' OR consecutive_failures > 0
                    ORDER BY worker_kind
                    """
                )
                circuits = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT h.job_id,h.seat_id,h.worker_id,h.payload,h.created_at,
                           e.cost_policy
                    FROM jaytec_worker_handoffs h
                    JOIN jaytec_fabric_envelopes e ON e.job_id=h.job_id
                    WHERE h.created_at >= now() - (%s * interval '1 minute')
                    ORDER BY h.created_at ASC
                    LIMIT 1000
                    """,
                    (minutes,),
                )
                handoffs = [dict(row) for row in cur.fetchall()]

        by_job: dict[str, dict[str, Any]] = {}
        seat_activity = {
            seat_id: {"claims": 0, "releases": 0, "jobs": []}
            for seat_id in SEAT_IDS
        }
        recovery_events: list[dict[str, Any]] = []
        owner_events: list[dict[str, Any]] = []

        for event in events:
            job_id = str(event.get("job_id") or "")
            payload = _obj(event.get("payload"))
            if job_id:
                item = by_job.setdefault(
                    job_id,
                    {
                        "job_id": job_id,
                        "task_id": _text(event.get("task_id")),
                        "description": _text(event.get("objective")),
                        "event_types": [],
                        "first_event_at": None,
                        "last_event_at": None,
                    },
                )
                item["event_types"].append(str(event.get("event_type") or ""))
                if item["first_event_at"] is None:
                    item["first_event_at"] = _iso(event.get("created_at"))
                item["last_event_at"] = _iso(event.get("created_at"))

            seat_id = str(payload.get("seat_id") or "")
            event_type = str(event.get("event_type") or "")
            if seat_id in seat_activity:
                if event_type == "WORKER_SEAT_CLAIMED":
                    seat_activity[seat_id]["claims"] += 1
                if "SEAT_RELEASED" in event_type:
                    seat_activity[seat_id]["releases"] += 1
                if job_id and job_id not in seat_activity[seat_id]["jobs"]:
                    seat_activity[seat_id]["jobs"].append(job_id)

            if event_type in RECOVERY_EVENT_TYPES or "STALE" in event_type or "RETRY" in event_type or "QUARANT" in event_type:
                recovery_events.append(
                    {
                        "event_type": event_type,
                        "job_id": job_id or None,
                        "at": _iso(event.get("created_at")),
                    }
                )
            if event_type in OWNER_EVENT_TYPES:
                owner_events.append(
                    {
                        "job_id": job_id or None,
                        "at": _iso(event.get("created_at")),
                        "payload": dict(payload),
                    }
                )

        completed = []
        for review in reviews:
            if str(review.get("decision") or "") != "ACCEPT":
                continue
            completed.append(
                {
                    "job_id": str(review.get("job_id") or ""),
                    "task_id": _text(review.get("task_id")),
                    "description": _text(review.get("objective")),
                    "watch_attestation": "ACCEPT",
                    "review_id": str(review.get("review_id") or ""),
                    "completed_at": _iso(review.get("created_at")),
                }
            )

        accepted_ids = {row["job_id"] for row in completed}
        attempted_not_complete = []
        for job_id, item in by_job.items():
            if job_id in accepted_ids:
                continue
            attempted_not_complete.append(
                {
                    **item,
                    "event_types": sorted(set(item["event_types"])),
                }
            )

        seats = []
        seat_map = {str(row.get("seat_id")): row for row in seat_rows}
        for seat_id in SEAT_IDS:
            row = seat_map.get(seat_id, {})
            status = str(row.get("status") or "")
            state = str(row.get("fabric_state") or "")
            blockers = row.get("blockers") or []
            current_job = str(row.get("job_id") or "")
            activity = seat_activity[seat_id]
            seats.append(
                {
                    "seat_id": seat_id,
                    "state": str(row.get("state") or "MISSING"),
                    "worker_identity": _text(row.get("worker_id")) or None,
                    "lease_owner": _text(row.get("lease_owner")) or None,
                    "lease_expires_at": _iso(row.get("lease_expires_at")),
                    "seat_epoch": int(row.get("seat_epoch") or 0),
                    "fence_token": int(row.get("fence_token") or 0),
                    "current_job": (
                        {
                            "job_id": current_job,
                            "task_id": _text(row.get("task_id")),
                            "description": _text(row.get("objective")),
                            "status": status,
                            "fabric_state": state,
                            "started_at": _iso(row.get("job_created_at")),
                            "updated_at": _iso(row.get("job_updated_at")),
                            "worker_kind": _text(row.get("worker_kind")) or None,
                            "blockers": blockers,
                            "next_action": _next_action(status, state, bool(blockers)),
                        }
                        if current_job
                        else None
                    ),
                    "last_60_minutes": {
                        "claims": activity["claims"],
                        "releases": activity["releases"],
                        "jobs_touched": activity["jobs"],
                    },
                }
            )

        provider_usage: dict[str, int] = {}
        zero_spend_only = True
        for handoff in handoffs:
            payload = _obj(handoff.get("payload"))
            provider = _text(payload.get("provider_identity") or "UNSPECIFIED", 160)
            provider_usage[provider] = provider_usage.get(provider, 0) + 1
            policy = _obj(handoff.get("cost_policy"))
            if str(policy.get("mode") or "ZERO_SPEND").upper() != "ZERO_SPEND":
                zero_spend_only = False

        active_count = sum(
            1 for row in seats
            if row["current_job"] is not None
            and row["current_job"]["fabric_state"] in {"CLAIMED", "RUNNING"}
        )
        blocked_count = sum(
            1 for row in queued
            if str(row.get("fabric_state") or "") in {
                "BLOCKED_OWNER","BLOCKED_DEPENDENCY","QUARANTINED","ESCALATED"
            }
        )
        queue_rows = []
        for row in queued:
            queue_rows.append(
                {
                    "job_id": str(row.get("job_id") or ""),
                    "task_id": _text(row.get("task_id")),
                    "description": _text(row.get("objective")),
                    "status": str(row.get("status") or ""),
                    "fabric_state": str(row.get("fabric_state") or ""),
                    "priority": int(row.get("priority") or 0),
                    "worker_kind": _text(row.get("worker_kind")) or None,
                    "blockers": row.get("blockers") or [],
                    "next_action": _next_action(
                        str(row.get("status") or ""),
                        str(row.get("fabric_state") or ""),
                        bool(row.get("blockers")),
                    ),
                }
            )

        watch_lease_active = bool(
            leader.get("lease_owner")
            and leader.get("lease_expires_at")
            and leader["lease_expires_at"] > observed_at
        )
        jay_action_required = any(
            str(row.get("fabric_state") or "") in {"BLOCKED_OWNER", "ESCALATED"}
            for row in queued
        )

        return {
            "schema_version": REPORT_SCHEMA,
            "window_minutes": minutes,
            "window_end_utc": _iso(observed_at),
            "watch": {
                "healthy": watch_lease_active,
                "controller": str(leader.get("controller_id") or "WATCH"),
                "leader": _text(leader.get("lease_owner")) or None,
                "leader_epoch": int(leader.get("leader_epoch") or 0),
                "fence_token": int(leader.get("fence_token") or 0),
                "lease_expires_at": _iso(leader.get("lease_expires_at")),
                "recovery_occurred": bool(recovery_events),
            },
            "seats": seats,
            "completed_jobs": completed,
            "attempted_not_completed": attempted_not_complete,
            "seat_activity": seat_activity,
            "queued_jobs": queue_rows,
            "recovery_and_safety_events": recovery_events,
            "circuits": [
                {
                    "worker_kind": _text(row.get("worker_kind")),
                    "state": str(row.get("state") or ""),
                    "consecutive_failures": int(row.get("consecutive_failures") or 0),
                    "failure_threshold": int(row.get("failure_threshold") or 0),
                    "open_until": _iso(row.get("open_until")),
                    "probe_job_id": row.get("probe_job_id"),
                }
                for row in circuits
            ],
            "owner_notifications": owner_events,
            "provider_and_spend": {
                "provider_usage": provider_usage,
                "zero_spend_policy_only": zero_spend_only,
                "declared_max_spend_usd": 0.0 if zero_spend_only else None,
                "paid_fallback_observed": False if zero_spend_only else None,
                "actual_spend_usd": None,
                "metering_note": (
                    "No provider billing meter is read by this report; policy and "
                    "observed provider route are reported separately."
                ),
            },
            "summary": {
                "completed": len(completed),
                "active": active_count,
                "blocked": blocked_count,
                "queued": sum(
                    1 for row in queue_rows
                    if row["fabric_state"] in {"QUEUED", "REWORK_QUEUED"}
                ),
                "seats_free": sum(1 for row in seats if row["state"] == "FREE"),
                "seats_total": len(seats),
            },
            "jay_action_required": (
                "Review owner-gated/escalated jobs."
                if jay_action_required
                else "None"
            ),
        }
