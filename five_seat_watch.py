from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from five_seat_signals import WORK_AVAILABLE_CHANNEL
from five_seat_authority import authority_requires_approval, normalize_cost_policy
from concurrency import UNRESOLVED_OPERATION_STATUSES


WATCH_CONTROLLER_ID = "WATCH"
WATCH_REVIEW_DECISIONS = ("ACCEPT", "REWORK", "BLOCK", "ESCALATE")
WATCH_LEADER_LOCK_KEY = "jaytec-five-seat-watch-leader-v1"


class WatchControllerError(RuntimeError):
    pass


class WatchLeaderUnavailable(WatchControllerError):
    pass


class WatchStaleLeader(WatchControllerError):
    pass


class WatchReviewConflict(WatchControllerError):
    pass


class WatchSelfApprovalForbidden(WatchControllerError):
    pass


@dataclass(frozen=True)
class WatchLeaderToken:
    owner: str
    leader_epoch: int
    fence_token: int
    lease_expires_at: datetime


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def review_target(decision: str) -> tuple[str, str]:
    mapping = {
        "ACCEPT": ("SUCCEEDED", "SUCCEEDED"),
        "REWORK": ("QUEUED", "REWORK_QUEUED"),
        "BLOCK": ("BLOCKED", "QUARANTINED"),
        "ESCALATE": ("BLOCKED", "ESCALATED"),
    }
    normalized = str(decision or "").upper()
    if normalized not in mapping:
        raise ValueError("invalid WATCH decision")
    return mapping[normalized]


class PostgresWatchController:
    """Singleton out-of-band WATCH dispatcher/reviewer authority."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def claim_leader(
        self,
        *,
        owner: str,
        lease_seconds: int = 300,
    ) -> WatchLeaderToken:
        if not owner:
            raise ValueError("owner is required")
        if lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (WATCH_LEADER_LOCK_KEY,),
                )
                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_watch_leader
                    WHERE controller_id='WATCH'
                    FOR UPDATE
                    """
                )
                row = cur.fetchone()
                if row is None:
                    raise WatchControllerError("watch_leader_row_missing")
                if (
                    row["lease_expires_at"] is not None
                    and row["lease_expires_at"] > datetime.now(timezone.utc)
                    and row["lease_owner"] not in (None, owner)
                ):
                    raise WatchLeaderUnavailable(str(row["lease_owner"]))

                cur.execute(
                    """
                    UPDATE jaytec_watch_leader
                    SET lease_owner=%s,
                        lease_expires_at=now() + (%s * interval '1 second'),
                        leader_epoch=leader_epoch+1,
                        fence_token=fence_token+1,
                        version=version+1,
                        updated_at=now()
                    WHERE controller_id='WATCH'
                    RETURNING lease_owner,leader_epoch,fence_token,lease_expires_at
                    """,
                    (owner, lease_seconds),
                )
                claimed = cur.fetchone()
                if claimed is None:
                    raise WatchControllerError("watch_leader_claim_failed")
                return WatchLeaderToken(
                    owner=str(claimed["lease_owner"]),
                    leader_epoch=int(claimed["leader_epoch"]),
                    fence_token=int(claimed["fence_token"]),
                    lease_expires_at=claimed["lease_expires_at"],
                )

    def heartbeat(
        self,
        token: WatchLeaderToken,
        *,
        lease_seconds: int = 300,
    ) -> WatchLeaderToken:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_watch_leader
                    SET lease_expires_at=now() + (%s * interval '1 second'),
                        version=version+1,
                        updated_at=now()
                    WHERE controller_id='WATCH'
                      AND lease_owner=%s
                      AND leader_epoch=%s
                      AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING lease_owner,leader_epoch,fence_token,lease_expires_at
                    """,
                    (
                        lease_seconds,
                        token.owner,
                        token.leader_epoch,
                        token.fence_token,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise WatchStaleLeader(token.owner)
                return WatchLeaderToken(
                    owner=str(row["lease_owner"]),
                    leader_epoch=int(row["leader_epoch"]),
                    fence_token=int(row["fence_token"]),
                    lease_expires_at=row["lease_expires_at"],
                )

    def release_leader(self, token: WatchLeaderToken) -> None:
        """Release only the exact WATCH generation currently owned by token."""
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_watch_leader
                    SET lease_owner=NULL,
                        lease_expires_at=NULL,
                        version=version+1,
                        updated_at=now()
                    WHERE controller_id='WATCH'
                      AND lease_owner=%s
                      AND leader_epoch=%s
                      AND fence_token=%s
                    RETURNING controller_id
                    """,
                    (
                        token.owner,
                        token.leader_epoch,
                        token.fence_token,
                    ),
                )
                if cur.fetchone() is None:
                    raise WatchStaleLeader(token.owner)

    def pending_reviews(self, *, limit: int = 100) -> list[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT h.*,j.task_id,j.subtask_id,j.objective,j.priority,
                           j.fabric_state,j.checkpoint_ref,
                           j.source_shared_state_version,j.fabric_rework_count,
                           j.fabric_max_reworks,j.fence_token AS current_job_fence_token,
                           e.worker_kind,e.evidence_standard,e.result_destination
                    FROM jaytec_worker_handoffs h
                    JOIN jaytec_jobs j ON j.job_id=h.job_id
                    JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    LEFT JOIN jaytec_watch_reviews r ON r.handoff_id=h.handoff_id
                    WHERE j.fabric_state IN ('HANDOFF_PENDING_REVIEW','QUARANTINED')
                      AND j.seat_id IS NULL
                      AND j.lease_owner IS NULL
                      AND r.handoff_id IS NULL
                    ORDER BY j.priority ASC,h.created_at ASC,h.handoff_id ASC
                    LIMIT %s
                    """,
                    (bounded,),
                )
                return [dict(row) for row in cur.fetchall()]

    def review(
        self,
        token: WatchLeaderToken,
        *,
        review_id: str,
        handoff_id: str,
        decision: str,
        reason: str,
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        review_id = str(review_id or "").strip()
        handoff_id = str(handoff_id or "").strip()
        reason = str(reason or "").strip()
        decision = str(decision or "").upper().strip()
        if not review_id or not handoff_id or not reason:
            raise ValueError("review_id, handoff_id and reason are required")
        if decision not in WATCH_REVIEW_DECISIONS:
            raise ValueError("invalid WATCH decision")
        if decision == "ACCEPT" and not dict(evidence or {}):
            raise ValueError("ACCEPT requires non-empty independent evidence")

        target_status, target_fabric_state = review_target(decision)

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                self._assert_leader(cur, token)

                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_watch_reviews
                    WHERE handoff_id=%s
                    FOR UPDATE
                    """,
                    (handoff_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if (
                        existing["review_id"] == review_id
                        and existing["decision"] == decision
                    ):
                        return {
                            "review": dict(existing),
                            "idempotent_replay": True,
                        }
                    raise WatchReviewConflict("handoff_already_reviewed:" + handoff_id)

                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_worker_handoffs
                    WHERE handoff_id=%s
                    FOR UPDATE
                    """,
                    (handoff_id,),
                )
                handoff = cur.fetchone()
                if handoff is None:
                    raise WatchControllerError("handoff_not_found:" + handoff_id)
                if str(handoff["worker_id"]) == token.owner:
                    raise WatchSelfApprovalForbidden(handoff_id)

                cur.execute(
                    """
                    SELECT
                      j.*,
                      e.envelope_hash,
                      e.approval_required,
                      e.authority_class,
                      e.cost_policy,
                      a.current_shared_state_version,
                      p.approval_id,
                      p.envelope_hash AS approval_envelope_hash,
                      p.source_shared_state_version AS approval_source_version,
                      p.authority_class AS approval_authority_class,
                      p.max_cost_usd AS approval_max_cost_usd
                    FROM jaytec_jobs j
                    JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    JOIN jaytec_fabric_authority_state a ON a.authority_id='FABRIC'
                    LEFT JOIN jaytec_fabric_approvals p
                      ON p.job_id=j.job_id
                     AND p.state='APPROVED'
                     AND (p.expires_at IS NULL OR p.expires_at > now())
                    WHERE j.job_id=%s
                    FOR UPDATE OF j,e,a
                    """,
                    (handoff["job_id"],),
                )
                job = cur.fetchone()
                if (
                    job is None
                    or job["fabric_state"] not in {"HANDOFF_PENDING_REVIEW", "QUARANTINED"}
                    or job["seat_id"] is not None
                    or job["lease_owner"] is not None
                    or job["checkpoint_ref"] != handoff_id
                ):
                    raise WatchReviewConflict("job_not_reviewable:" + str(handoff["job_id"]))
                if decision in {"ACCEPT", "REWORK"}:
                    current_shared_state_version = int(
                        job.get("current_shared_state_version") or 0
                    )
                    if (
                        current_shared_state_version <= 0
                        or int(job.get("source_shared_state_version") or -1)
                           != current_shared_state_version
                    ):
                        raise WatchReviewConflict("stale_source_shared_state_version")
                    if job.get("cancel_requested_at") is not None:
                        raise WatchReviewConflict("cancel_requested_cannot_accept_or_rework")
                    policy = normalize_cost_policy(job.get("cost_policy") or {})
                    approval_required = bool(
                        job.get("approval_required")
                    ) or authority_requires_approval(
                        job.get("authority_class"),
                        policy,
                    )
                    if approval_required:
                        if not job.get("approval_id"):
                            raise WatchReviewConflict("approval_required_or_expired")
                        if job.get("approval_envelope_hash") != job.get("envelope_hash"):
                            raise WatchReviewConflict("approval_envelope_mismatch")
                        if int(job.get("approval_source_version") or -1) != current_shared_state_version:
                            raise WatchReviewConflict("approval_source_version_stale")
                        if job.get("approval_authority_class") != job.get("authority_class"):
                            raise WatchReviewConflict("approval_authority_class_mismatch")
                        if float(job.get("approval_max_cost_usd") or 0) < float(
                            policy["max_cost_usd"]
                        ):
                            raise WatchReviewConflict("approval_cost_cap_too_low")

                if (
                    job["fabric_state"] == "QUARANTINED"
                    and decision in {"ACCEPT", "REWORK"}
                ):
                    if dict(evidence or {}).get("quarantine_reconciled") is not True:
                        raise WatchReviewConflict(
                            "quarantine_requires_reconciliation_evidence"
                        )
                    cur.execute(
                        """
                        SELECT operation_id,status
                        FROM jaytec_operations
                        WHERE job_id=%s
                          AND status = ANY(%s)
                        ORDER BY operation_id
                        FOR UPDATE
                        """,
                        (
                            handoff["job_id"],
                            list(UNRESOLVED_OPERATION_STATUSES),
                        ),
                    )
                    unresolved_operations = [
                        dict(row) for row in cur.fetchall()
                    ]
                    if unresolved_operations:
                        raise WatchReviewConflict(
                            "quarantine_has_unresolved_operations:"
                            + ",".join(
                                str(row["operation_id"])
                                for row in unresolved_operations
                            )
                        )
                if decision == "REWORK":
                    rework_count = int(job.get("fabric_rework_count") or 0)
                    max_reworks = int(job.get("fabric_max_reworks") or 0)
                    if rework_count >= max_reworks:
                        raise WatchReviewConflict("rework_budget_exhausted")

                cur.execute(
                    """
                    INSERT INTO jaytec_watch_reviews(
                      review_id,job_id,handoff_id,controller_id,controller_owner,
                      leader_epoch,fence_token,decision,reason,evidence
                    ) VALUES (
                      %s,%s,%s,'WATCH',%s,%s,%s,%s,%s,%s::jsonb
                    )
                    RETURNING *
                    """,
                    (
                        review_id,
                        handoff["job_id"],
                        handoff_id,
                        token.owner,
                        token.leader_epoch,
                        token.fence_token,
                        decision,
                        reason,
                        _json(dict(evidence or {})),
                    ),
                )
                review = cur.fetchone()
                if review is None:
                    raise WatchControllerError("review_insert_failed:" + review_id)

                blockers = None
                if decision in {"BLOCK", "ESCALATE"}:
                    blockers = _json(
                        [
                            {
                                "source": "WATCH_REVIEW",
                                "decision": decision,
                                "review_id": review_id,
                                "reason": reason,
                            }
                        ]
                    )

                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status=%s,
                        fabric_state=%s,
                        blockers=CASE
                          WHEN %s='REWORK' THEN '[]'::jsonb
                          WHEN %s::jsonb IS NULL THEN blockers
                          ELSE %s::jsonb
                        END,
                        next_attempt_at=CASE
                          WHEN %s='REWORK' THEN now()
                          ELSE next_attempt_at
                        END,
                        fabric_attempt_count=CASE
                          WHEN %s='REWORK' THEN 0
                          ELSE fabric_attempt_count
                        END,
                        fabric_rework_count=CASE
                          WHEN %s='REWORK' THEN fabric_rework_count+1
                          ELSE fabric_rework_count
                        END,
                        cancel_requested_at=CASE
                          WHEN %s='REWORK' THEN NULL
                          ELSE cancel_requested_at
                        END,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND fabric_state IN ('HANDOFF_PENDING_REVIEW','QUARANTINED')
                      AND seat_id IS NULL
                      AND lease_owner IS NULL
                    RETURNING *
                    """,
                    (
                        target_status,
                        target_fabric_state,
                        decision,
                        blockers,
                        blockers,
                        decision,
                        decision,
                        decision,
                        decision,
                        handoff["job_id"],
                    ),
                )
                updated_job = cur.fetchone()
                if updated_job is None:
                    raise WatchReviewConflict("review_state_transition_lost:" + handoff_id)

                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(
                      job_id,event_type,source,source_version,payload
                    )
                    SELECT job_id,
                           'WATCH_REVIEW_DECISION',
                           'FIVE_SEAT_WATCH',
                           source_shared_state_version,
                           %s::jsonb
                    FROM jaytec_jobs
                    WHERE job_id=%s
                    """,
                    (
                        _json(
                            {
                                "review_id": review_id,
                                "handoff_id": handoff_id,
                                "decision": decision,
                                "leader_epoch": token.leader_epoch,
                                "fence_token": token.fence_token,
                            }
                        ),
                        handoff["job_id"],
                    ),
                )

                if decision in {"ACCEPT", "REWORK"}:
                    cur.execute(
                        "SELECT pg_notify(%s,%s)",
                        (
                            WORK_AVAILABLE_CHANNEL,
                            _json(
                                {
                                    "reason": "watch_review_" + decision.lower(),
                                    "job_id": str(handoff["job_id"]),
                                }
                            ),
                        ),
                    )

                if decision in {"ACCEPT", "ESCALATE"}:
                    owner_event_kind = (
                        "WHOLE_JOB_COMPLETE"
                        if decision == "ACCEPT"
                        else "NEEDS_OWNER"
                    )
                    cur.execute(
                        """
                        INSERT INTO jaytec_job_events(
                          job_id,event_type,source,source_version,payload
                        )
                        SELECT job_id,
                               'OWNER_NOTIFICATION_REQUIRED',
                               'FIVE_SEAT_WATCH',
                               source_shared_state_version,
                               %s::jsonb
                        FROM jaytec_jobs
                        WHERE job_id=%s
                        """,
                        (
                            _json(
                                {
                                    "kind": owner_event_kind,
                                    "review_id": review_id,
                                    "handoff_id": handoff_id,
                                    "decision": decision,
                                    "reason": reason,
                                    "leader_epoch": token.leader_epoch,
                                    "fence_token": token.fence_token,
                                }
                            ),
                            handoff["job_id"],
                        ),
                    )

                return {
                    "review": dict(review),
                    "job": dict(updated_job),
                    "idempotent_replay": False,
                }

    @staticmethod
    def _assert_leader(cur, token: WatchLeaderToken) -> None:
        cur.execute(
            """
            SELECT lease_owner,lease_expires_at,leader_epoch,fence_token
            FROM jaytec_watch_leader
            WHERE controller_id='WATCH'
            FOR UPDATE
            """
        )
        row = cur.fetchone()
        if (
            row is None
            or row["lease_owner"] != token.owner
            or int(row["leader_epoch"]) != token.leader_epoch
            or int(row["fence_token"]) != token.fence_token
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= datetime.now(timezone.utc)
        ):
            raise WatchStaleLeader(token.owner)
