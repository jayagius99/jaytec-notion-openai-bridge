from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from concurrency import (
    SCHEDULER_LOCK_KEY,
    UNRESOLVED_OPERATION_STATUSES,
    concurrency_decision,
    duplicate_assignment,
)


WORKER_SEAT_IDS = tuple(f"WORKER-SEAT-{index}" for index in range(1, 6))
WORKER_SEAT_COUNT = len(WORKER_SEAT_IDS)


class FiveSeatRuntimeError(RuntimeError):
    pass


class FiveSeatSchemaNotReady(FiveSeatRuntimeError):
    pass


class FiveSeatStaleLease(FiveSeatRuntimeError):
    pass


class FiveSeatReleaseBlocked(FiveSeatRuntimeError):
    pass


@dataclass(frozen=True)
class FiveSeatLeaseToken:
    job_id: str
    seat_id: str
    owner: str
    ownership_epoch: int
    job_fence_token: int
    seat_epoch: int
    seat_fence_token: int
    lease_expires_at: datetime


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(dict(value)).encode("utf-8")).hexdigest()


HANDOFF_REQUIRED_FIELDS = (
    "starting_checkpoint",
    "operations",
    "artifacts",
    "tests",
    "evidence",
    "provider_identity",
    "unresolved_items",
    "partial_side_effect_status",
    "proposed_next_action",
    "worker_completion_classification",
)


def _row(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result: Dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat()
        else:
            result[key] = value
    return result


class PostgresFiveSeatScheduler:
    """Five explicit durable worker seats over the existing JAYTEC job runtime.

    The scheduler intentionally uses the SAME advisory lock and the SAME
    jaytec_jobs / jaytec_operations tables as PostgresConcurrencyScheduler.
    During migration this prevents a second admission authority from racing the
    legacy scheduler. No DDL is executed by this class.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def verify_schema_ready(self) -> Dict[str, bool]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                      to_regclass(current_schema() || '.jaytec_worker_seats') IS NOT NULL
                        AS worker_seats,
                      EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name='jaytec_jobs'
                          AND column_name='seat_id'
                      ) AS jobs_seat_id,
                      EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name='jaytec_jobs'
                          AND column_name='fabric_state'
                      ) AS jobs_fabric_state
                    """
                )
                row = dict(cur.fetchone() or {})
                if bool(row.get("worker_seats")):
                    cur.execute("SELECT seat_id FROM jaytec_worker_seats ORDER BY seat_id")
                    observed = tuple(str(item["seat_id"]) for item in cur.fetchall())
                else:
                    observed = ()
        checks = {
            "worker_seats": bool(row.get("worker_seats")),
            "jobs_seat_id": bool(row.get("jobs_seat_id")),
            "jobs_fabric_state": bool(row.get("jobs_fabric_state")),
            "exact_seat_set": observed == WORKER_SEAT_IDS,
        }
        missing = sorted(name for name, ready in checks.items() if not ready)
        if missing:
            raise FiveSeatSchemaNotReady(",".join(missing))
        return checks

    def list_seats(self) -> list[Dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT seat_id,state,current_job_id,worker_id,lease_owner,
                           lease_expires_at,seat_epoch,fence_token,
                           last_handoff_ref,version,updated_at
                    FROM jaytec_worker_seats
                    ORDER BY seat_id
                    """
                )
                return [_row(row) or {} for row in cur.fetchall()]

    def claim_next(
        self,
        *,
        owner: str,
        execution_room_id: str,
        lease_seconds: int = 300,
    ) -> Optional[Dict[str, Any]]:
        if not owner:
            raise ValueError("owner is required")
        if not execution_room_id:
            raise ValueError("execution_room_id is required")
        if lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Shared with the legacy scheduler so migration cannot produce
                # two simultaneous admission decisions.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )

                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_worker_seats
                    WHERE state='FREE'
                      AND current_job_id IS NULL
                      AND lease_owner IS NULL
                      AND lease_expires_at IS NULL
                    ORDER BY seat_id
                    LIMIT 1
                    FOR UPDATE
                    """
                )
                seat = cur.fetchone()
                if seat is None:
                    return None
                seat_id = str(seat["seat_id"])
                if seat_id not in WORKER_SEAT_IDS:
                    raise FiveSeatSchemaNotReady("unexpected_seat_id:" + seat_id)

                cur.execute(
                    """
                    SELECT * FROM jaytec_jobs
                    WHERE status='RUNNING'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > now()
                    ORDER BY priority ASC, updated_at ASC
                    """
                )
                active = [dict(row) for row in cur.fetchall()]
                if len(active) >= WORKER_SEAT_COUNT:
                    return None

                cur.execute(
                    """
                    SELECT DISTINCT job_id
                    FROM jaytec_operations
                    WHERE status = ANY(%s)
                    """,
                    (list(UNRESOLVED_OPERATION_STATUSES),),
                )
                unresolved = {str(row["job_id"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT job_id FROM jaytec_jobs
                    WHERE status='SUCCEEDED'
                       OR fabric_state='SUCCEEDED'
                    """
                )
                completed = {str(row["job_id"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT * FROM jaytec_jobs
                    WHERE status IN ('QUEUED','PAUSED')
                      AND COALESCE(fabric_state,'QUEUED') IN ('QUEUED','REWORK_QUEUED')
                      AND seat_id IS NULL
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                    ORDER BY priority ASC, created_at ASC, job_id ASC
                    FOR UPDATE SKIP LOCKED
                    """
                )
                queued = [dict(row) for row in cur.fetchall()]

                for candidate in queued:
                    duplicate = duplicate_assignment(candidate, active)
                    if duplicate:
                        continue
                    decision = concurrency_decision(
                        candidate,
                        active,
                        completed_job_ids=completed,
                        unresolved_side_effect_job_ids=unresolved,
                    )
                    if not decision.allowed:
                        continue

                    cur.execute(
                        """
                        UPDATE jaytec_jobs
                        SET status='RUNNING',
                            fabric_state='RUNNING',
                            seat_id=%s,
                            lease_owner=%s,
                            lease_expires_at=now() + (%s * interval '1 second'),
                            execution_room_id=%s,
                            ownership_epoch=ownership_epoch+1,
                            fence_token=fence_token+1,
                            version=version+1,
                            updated_at=now()
                        WHERE job_id=%s
                          AND status IN ('QUEUED','PAUSED')
                          AND COALESCE(fabric_state,'QUEUED') IN ('QUEUED','REWORK_QUEUED')
                          AND seat_id IS NULL
                          AND (lease_expires_at IS NULL OR lease_expires_at < now())
                        RETURNING *
                        """,
                        (
                            seat_id,
                            owner,
                            lease_seconds,
                            execution_room_id,
                            candidate["job_id"],
                        ),
                    )
                    claimed = cur.fetchone()
                    if claimed is None:
                        continue

                    cur.execute(
                        """
                        UPDATE jaytec_worker_seats
                        SET state='RUNNING',
                            current_job_id=%s,
                            worker_id=%s,
                            lease_owner=%s,
                            lease_expires_at=now() + (%s * interval '1 second'),
                            seat_epoch=seat_epoch+1,
                            fence_token=fence_token+1,
                            version=version+1,
                            updated_at=now()
                        WHERE seat_id=%s
                          AND state='FREE'
                          AND current_job_id IS NULL
                          AND lease_owner IS NULL
                          AND lease_expires_at IS NULL
                        RETURNING seat_epoch,fence_token,lease_expires_at
                        """,
                        (
                            claimed["job_id"],
                            owner,
                            owner,
                            lease_seconds,
                            seat_id,
                        ),
                    )
                    seat_claim = cur.fetchone()
                    if seat_claim is None:
                        raise FiveSeatRuntimeError("seat_claim_lost:" + seat_id)

                    payload = {
                        "execution_room_id": execution_room_id,
                        "concurrency_class": claimed.get("concurrency_class"),
                        "scheduler": "five-seat-v1",
                        "seat_id": seat_id,
                        "seat_epoch": int(seat_claim["seat_epoch"]),
                        "seat_fence_token": int(seat_claim["fence_token"]),
                    }
                    cur.execute(
                        """
                        INSERT INTO jaytec_job_events(
                          job_id,event_type,source,source_version,payload
                        )
                        VALUES (
                          %s,'WORKER_SEAT_CLAIMED','FIVE_SEAT_SCHEDULER',%s,%s::jsonb
                        )
                        """,
                        (
                            claimed["job_id"],
                            claimed.get("source_shared_state_version"),
                            _json(payload),
                        ),
                    )

                    result = dict(claimed)
                    result["seat_id"] = seat_id
                    result["seat_epoch"] = int(seat_claim["seat_epoch"])
                    result["seat_fence_token"] = int(seat_claim["fence_token"])
                    result["seat_lease_expires_at"] = seat_claim["lease_expires_at"]
                    return result

                return None

    @staticmethod
    def token_from_claim(claim: Mapping[str, Any]) -> FiveSeatLeaseToken:
        required = (
            "job_id",
            "seat_id",
            "lease_owner",
            "ownership_epoch",
            "fence_token",
            "seat_epoch",
            "seat_fence_token",
            "lease_expires_at",
        )
        missing = [key for key in required if claim.get(key) in (None, "")]
        if missing:
            raise FiveSeatRuntimeError("claim_missing:" + ",".join(sorted(missing)))
        seat_id = str(claim["seat_id"])
        if seat_id not in WORKER_SEAT_IDS:
            raise FiveSeatRuntimeError("invalid_seat_id:" + seat_id)
        return FiveSeatLeaseToken(
            job_id=str(claim["job_id"]),
            seat_id=seat_id,
            owner=str(claim["lease_owner"]),
            ownership_epoch=int(claim["ownership_epoch"]),
            job_fence_token=int(claim["fence_token"]),
            seat_epoch=int(claim["seat_epoch"]),
            seat_fence_token=int(claim["seat_fence_token"]),
            lease_expires_at=claim["lease_expires_at"],
        )

    def heartbeat(
        self,
        token: FiveSeatLeaseToken,
        *,
        lease_seconds: int = 300,
    ) -> FiveSeatLeaseToken:
        if lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_worker_seats
                    SET lease_expires_at=now() + (%s * interval '1 second'),
                        version=version+1,
                        updated_at=now()
                    WHERE seat_id=%s
                      AND state='RUNNING'
                      AND current_job_id=%s
                      AND worker_id=%s
                      AND lease_owner=%s
                      AND seat_epoch=%s
                      AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING lease_expires_at
                    """,
                    (
                        lease_seconds,
                        token.seat_id,
                        token.job_id,
                        token.owner,
                        token.owner,
                        token.seat_epoch,
                        token.seat_fence_token,
                    ),
                )
                seat = cur.fetchone()
                if seat is None:
                    raise FiveSeatStaleLease(token.job_id)

                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET lease_expires_at=now() + (%s * interval '1 second'),
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND status='RUNNING'
                      AND fabric_state='RUNNING'
                      AND seat_id=%s
                      AND lease_owner=%s
                      AND ownership_epoch=%s
                      AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING lease_expires_at
                    """,
                    (
                        lease_seconds,
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                    ),
                )
                job = cur.fetchone()
                if job is None:
                    raise FiveSeatStaleLease(token.job_id)

                expiry = min(seat["lease_expires_at"], job["lease_expires_at"])
                return FiveSeatLeaseToken(
                    job_id=token.job_id,
                    seat_id=token.seat_id,
                    owner=token.owner,
                    ownership_epoch=token.ownership_epoch,
                    job_fence_token=token.job_fence_token,
                    seat_epoch=token.seat_epoch,
                    seat_fence_token=token.seat_fence_token,
                    lease_expires_at=expiry,
                )

    def release_for_review(
        self,
        token: FiveSeatLeaseToken,
        *,
        handoff_ref: str,
        handoff_payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        handoff_ref = str(handoff_ref or "").strip()
        if not handoff_ref:
            raise ValueError("handoff_ref is required")
        payload = dict(handoff_payload or {})
        missing = [
            key
            for key in HANDOFF_REQUIRED_FIELDS
            if key not in payload
        ]
        if missing:
            raise ValueError("handoff_payload_missing:" + ",".join(sorted(missing)))

        lineage = {
            "handoff_id": handoff_ref,
            "job_id": token.job_id,
            "seat_id": token.seat_id,
            "worker_id": token.owner,
            "ownership_epoch": token.ownership_epoch,
            "job_fence_token": token.job_fence_token,
            "seat_epoch": token.seat_epoch,
            "seat_fence_token": token.seat_fence_token,
            "task_packet_hash": payload.get("task_packet_hash"),
            "payload": payload,
        }
        handoff_digest = _digest(lineage)

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )

                # Lost acknowledgement must be safely replayable. If the exact
                # immutable handoff is already present and the seat is already
                # free for that handoff, return the durable state unchanged.
                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_worker_handoffs
                    WHERE handoff_id=%s
                    FOR UPDATE
                    """,
                    (handoff_ref,),
                )
                existing_handoff = cur.fetchone()
                if existing_handoff is not None:
                    exact = (
                        existing_handoff["job_id"] == token.job_id
                        and existing_handoff["seat_id"] == token.seat_id
                        and existing_handoff["worker_id"] == token.owner
                        and int(existing_handoff["ownership_epoch"]) == token.ownership_epoch
                        and int(existing_handoff["job_fence_token"]) == token.job_fence_token
                        and int(existing_handoff["seat_epoch"]) == token.seat_epoch
                        and int(existing_handoff["seat_fence_token"]) == token.seat_fence_token
                        and existing_handoff["handoff_digest"] == handoff_digest
                    )
                    if not exact:
                        raise FiveSeatRuntimeError("conflicting_handoff_replay:" + handoff_ref)
                    cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s", (token.job_id,))
                    replay_job = cur.fetchone()
                    cur.execute(
                        "SELECT * FROM jaytec_worker_seats WHERE seat_id=%s",
                        (token.seat_id,),
                    )
                    replay_seat = cur.fetchone()
                    if (
                        replay_job is not None
                        and replay_job["fabric_state"] == "HANDOFF_PENDING_REVIEW"
                        and replay_job["seat_id"] is None
                        and replay_job["lease_owner"] is None
                        and replay_seat is not None
                        and replay_seat["state"] == "FREE"
                        and replay_seat["current_job_id"] is None
                        and replay_seat["last_handoff_ref"] == handoff_ref
                    ):
                        return {
                            "job": _row(replay_job) or {},
                            "seat": _row(replay_seat) or {},
                            "handoff": _row(existing_handoff) or {},
                            "idempotent_replay": True,
                        }
                    raise FiveSeatRuntimeError("handoff_replay_state_mismatch:" + handoff_ref)

                cur.execute(
                    """
                    SELECT seat_id,state,current_job_id,worker_id,lease_owner,
                           lease_expires_at,seat_epoch,fence_token
                    FROM jaytec_worker_seats
                    WHERE seat_id=%s
                    FOR UPDATE
                    """,
                    (token.seat_id,),
                )
                seat = cur.fetchone()
                if (
                    seat is None
                    or seat["state"] != "RUNNING"
                    or seat["current_job_id"] != token.job_id
                    or seat["worker_id"] != token.owner
                    or seat["lease_owner"] != token.owner
                    or int(seat["seat_epoch"]) != token.seat_epoch
                    or int(seat["fence_token"]) != token.seat_fence_token
                    or seat["lease_expires_at"] is None
                    or seat["lease_expires_at"] <= datetime.now(timezone.utc)
                ):
                    raise FiveSeatStaleLease(token.job_id)

                cur.execute(
                    """
                    SELECT job_id,status,fabric_state,seat_id,lease_owner,
                           lease_expires_at,ownership_epoch,fence_token,
                           task_packet_hash
                    FROM jaytec_jobs
                    WHERE job_id=%s
                    FOR UPDATE
                    """,
                    (token.job_id,),
                )
                job = cur.fetchone()
                if (
                    job is None
                    or job["status"] != "RUNNING"
                    or job["fabric_state"] != "RUNNING"
                    or job["seat_id"] != token.seat_id
                    or job["lease_owner"] != token.owner
                    or int(job["ownership_epoch"]) != token.ownership_epoch
                    or int(job["fence_token"]) != token.job_fence_token
                    or job["lease_expires_at"] is None
                    or job["lease_expires_at"] <= datetime.now(timezone.utc)
                ):
                    raise FiveSeatStaleLease(token.job_id)

                cur.execute(
                    """
                    SELECT operation_id,status
                    FROM jaytec_operations
                    WHERE job_id=%s
                      AND status = ANY(%s)
                    ORDER BY operation_id
                    """,
                    (
                        token.job_id,
                        list(UNRESOLVED_OPERATION_STATUSES),
                    ),
                )
                unresolved = [dict(row) for row in cur.fetchall()]
                if unresolved:
                    raise FiveSeatReleaseBlocked(
                        "unresolved_operations:"
                        + ",".join(str(row["operation_id"]) for row in unresolved)
                    )

                cur.execute(
                    """
                    INSERT INTO jaytec_worker_handoffs(
                      handoff_id,job_id,seat_id,worker_id,
                      ownership_epoch,job_fence_token,seat_epoch,seat_fence_token,
                      task_packet_hash,handoff_digest,payload
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    )
                    RETURNING *
                    """,
                    (
                        handoff_ref,
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                        token.seat_epoch,
                        token.seat_fence_token,
                        payload.get("task_packet_hash") or job.get("task_packet_hash"),
                        handoff_digest,
                        _json(payload),
                    ),
                )
                durable_handoff = cur.fetchone()
                if durable_handoff is None:
                    raise FiveSeatRuntimeError("handoff_insert_failed:" + handoff_ref)

                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status='PAUSED',
                        fabric_state='HANDOFF_PENDING_REVIEW',
                        seat_id=NULL,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        checkpoint_ref=%s,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND seat_id=%s
                      AND lease_owner=%s
                      AND ownership_epoch=%s
                      AND fence_token=%s
                    RETURNING *
                    """,
                    (
                        handoff_ref,
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                    ),
                )
                released_job = cur.fetchone()
                if released_job is None:
                    raise FiveSeatStaleLease(token.job_id)

                cur.execute(
                    """
                    UPDATE jaytec_worker_seats
                    SET state='FREE',
                        current_job_id=NULL,
                        worker_id=NULL,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        last_handoff_ref=%s,
                        version=version+1,
                        updated_at=now()
                    WHERE seat_id=%s
                      AND current_job_id=%s
                      AND worker_id=%s
                      AND seat_epoch=%s
                      AND fence_token=%s
                    RETURNING *
                    """,
                    (
                        handoff_ref,
                        token.seat_id,
                        token.job_id,
                        token.owner,
                        token.seat_epoch,
                        token.seat_fence_token,
                    ),
                )
                released_seat = cur.fetchone()
                if released_seat is None:
                    raise FiveSeatStaleLease(token.job_id)

                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(
                      job_id,event_type,source,source_version,payload
                    )
                    SELECT job_id,
                           'WORKER_HANDOFF_COMMITTED_AND_SEAT_RELEASED',
                           'FIVE_SEAT_SCHEDULER',
                           source_shared_state_version,
                           %s::jsonb
                    FROM jaytec_jobs
                    WHERE job_id=%s
                    """,
                    (
                        _json(
                            {
                                "seat_id": token.seat_id,
                                "seat_epoch": token.seat_epoch,
                                "seat_fence_token": token.seat_fence_token,
                                "handoff_ref": handoff_ref,
                                "handoff_digest": handoff_digest,
                            }
                        ),
                        token.job_id,
                    ),
                )

                return {
                    "job": _row(released_job) or {},
                    "seat": _row(released_seat) or {},
                    "handoff": _row(durable_handoff) or {},
                    "idempotent_replay": False,
                }

