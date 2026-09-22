from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from five_seat_signals import WORK_AVAILABLE_CHANNEL


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

    def pending_reviews(self, *, limit: int = 100) -> list[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT h.*,j.priority,j.fabric_state,j.checkpoint_ref
                    FROM jaytec_worker_handoffs h
                    JOIN jaytec_jobs j ON j.job_id=h.job_id
                    LEFT JOIN jaytec_watch_reviews r ON r.handoff_id=h.handoff_id
                    WHERE j.fabric_state='HANDOFF_PENDING_REVIEW'
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
                    SELECT *
                    FROM jaytec_jobs
                    WHERE job_id=%s
                    FOR UPDATE
                    """,
                    (handoff["job_id"],),
                )
                job = cur.fetchone()
                if (
                    job is None
                    or job["fabric_state"] != "HANDOFF_PENDING_REVIEW"
                    or job["seat_id"] is not None
                    or job["lease_owner"] is not None
                    or job["checkpoint_ref"] != handoff_id
                ):
                    raise WatchReviewConflict("job_not_reviewable:" + str(handoff["job_id"]))

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
                          WHEN %s::jsonb IS NULL THEN blockers
                          ELSE %s::jsonb
                        END,
                        next_attempt_at=CASE
                          WHEN %s='REWORK' THEN now()
                          ELSE next_attempt_at
                        END,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND fabric_state='HANDOFF_PENDING_REVIEW'
                      AND seat_id IS NULL
                      AND lease_owner IS NULL
                    RETURNING *
                    """,
                    (
                        target_status,
                        target_fabric_state,
                        blockers,
                        blockers,
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
