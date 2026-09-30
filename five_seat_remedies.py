from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from concurrency import SCHEDULER_LOCK_KEY, UNRESOLVED_OPERATION_STATUSES
from five_seat_runtime import FiveSeatLeaseToken, FiveSeatStaleLease
from five_seat_signals import PostgresFabricSignal, WORK_AVAILABLE_CHANNEL


class FabricRemedyError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def retry_delay_seconds(attempt_count: int) -> int:
    attempt = max(1, int(attempt_count))
    return min(300, 5 * (2 ** (attempt - 1)))


class PostgresFabricRemedies:
    """Failure/cancellation/reconciliation authority for worker ownership.

    This class may invalidate stale worker authority and free seats. It may not
    accept worker results or perform WATCH review.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.signal = PostgresFabricSignal(database_url)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    @staticmethod
    def _assert_worker(cur, token: FiveSeatLeaseToken) -> Mapping[str, Any]:
        cur.execute(
            """
            SELECT j.*,s.state AS seat_state,s.current_job_id,s.worker_id,
                   s.lease_owner AS seat_lease_owner,
                   s.lease_expires_at AS seat_lease_expires_at,
                   s.seat_epoch,s.fence_token AS seat_fence_token
            FROM jaytec_jobs j
            JOIN jaytec_worker_seats s ON s.seat_id=j.seat_id
            WHERE j.job_id=%s
            FOR UPDATE OF j,s
            """,
            (token.job_id,),
        )
        row = cur.fetchone()
        now = datetime.now(timezone.utc)
        if (
            row is None
            or row["status"] != "RUNNING"
            or row["seat_id"] != token.seat_id
            or row["lease_owner"] != token.owner
            or int(row["ownership_epoch"]) != token.ownership_epoch
            or int(row["fence_token"]) != token.job_fence_token
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
            or row["seat_state"] != "RUNNING"
            or row["current_job_id"] != token.job_id
            or row["worker_id"] != token.owner
            or row["seat_lease_owner"] != token.owner
            or int(row["seat_epoch"]) != token.seat_epoch
            or int(row["seat_fence_token"]) != token.seat_fence_token
            or row["seat_lease_expires_at"] is None
            or row["seat_lease_expires_at"] <= now
        ):
            raise FiveSeatStaleLease(token.job_id)
        return row

    @staticmethod
    def _unresolved_operations(cur, job_id: str) -> list[str]:
        cur.execute(
            """
            SELECT operation_id
            FROM jaytec_operations
            WHERE job_id=%s AND status = ANY(%s)
            ORDER BY operation_id
            """,
            (job_id, list(UNRESOLVED_OPERATION_STATUSES)),
        )
        return [str(row["operation_id"]) for row in cur.fetchall()]

    @staticmethod
    def _free_seat(cur, token: FiveSeatLeaseToken, *, last_ref: Optional[str]) -> None:
        cur.execute(
            """
            UPDATE jaytec_worker_seats
            SET state='FREE',
                current_job_id=NULL,
                worker_id=NULL,
                lease_owner=NULL,
                lease_expires_at=NULL,
                last_handoff_ref=COALESCE(%s,last_handoff_ref),
                seat_epoch=seat_epoch+1,
                fence_token=fence_token+1,
                version=version+1,
                updated_at=now()
            WHERE seat_id=%s
              AND current_job_id=%s
              AND worker_id=%s
              AND seat_epoch=%s
              AND fence_token=%s
            RETURNING seat_id
            """,
            (
                last_ref,
                token.seat_id,
                token.job_id,
                token.owner,
                token.seat_epoch,
                token.seat_fence_token,
            ),
        )
        if cur.fetchone() is None:
            raise FiveSeatStaleLease(token.job_id)

    def request_cancel(self, job_id: str, *, reason: str) -> Dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("reason is required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )
                cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
                job = cur.fetchone()
                if job is None:
                    raise FabricRemedyError("job_not_found:" + job_id)
                if job["fabric_state"] in {
                    "SUCCEEDED","FAILED_SAFE","CANCELLED","SUPERSEDED","QUARANTINED"
                }:
                    return dict(job)

                if job["status"] == "RUNNING" and job["seat_id"] is not None:
                    cur.execute(
                        """
                        UPDATE jaytec_jobs
                        SET fabric_state='CANCEL_REQUESTED',
                            cancel_requested_at=COALESCE(cancel_requested_at,now()),
                            blockers=%s::jsonb,
                            version=version+1,
                            updated_at=now()
                        WHERE job_id=%s
                        RETURNING *
                        """,
                        (
                            _json([{"source":"CANCEL_REQUEST","reason":reason}]),
                            job_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE jaytec_jobs
                        SET status='CANCELED',
                            fabric_state='CANCELLED',
                            cancel_requested_at=COALESCE(cancel_requested_at,now()),
                            blockers=%s::jsonb,
                            lease_owner=NULL,
                            lease_expires_at=NULL,
                            seat_id=NULL,
                            ownership_epoch=ownership_epoch+1,
                            fence_token=fence_token+1,
                            version=version+1,
                            updated_at=now()
                        WHERE job_id=%s
                        RETURNING *
                        """,
                        (
                            _json([{"source":"CANCEL_REQUEST","reason":reason}]),
                            job_id,
                        ),
                    )
                updated = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,'FABRIC_CANCEL_REQUESTED','FIVE_SEAT_REMEDIES',%s::jsonb)
                    """,
                    (job_id, _json({"reason": reason})),
                )
                return dict(updated)

    def cancel_requested(self, token: FiveSeatLeaseToken) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM jaytec_jobs
                    WHERE job_id=%s AND seat_id=%s AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND fabric_state='CANCEL_REQUESTED'
                    """,
                    (
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                    ),
                )
                return cur.fetchone() is not None

    def acknowledge_cancel(self, token: FiveSeatLeaseToken) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )
                job = self._assert_worker(cur, token)
                if not (
                    job.get("fabric_state") == "CANCEL_REQUESTED"
                    or job.get("cancel_requested_at") is not None
                ):
                    raise FabricRemedyError("cancel_not_requested:" + token.job_id)
                unresolved = self._unresolved_operations(cur, token.job_id)
                target_status = "BLOCKED" if unresolved else "CANCELED"
                target_fabric = "QUARANTINED" if unresolved else "CANCELLED"
                blockers = _json(
                    [
                        {
                            "source": "CANCEL_ACK",
                            "reason": (
                                "CANCEL_WITH_UNRESOLVED_SIDE_EFFECTS"
                                if unresolved
                                else "CANCELLED_BY_REQUEST"
                            ),
                            "unresolved_operations": unresolved,
                        }
                    ]
                )
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status=%s,
                        fabric_state=%s,
                        blockers=%s::jsonb,
                        seat_id=NULL,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        execution_room_id=NULL,
                        ownership_epoch=ownership_epoch+1,
                        fence_token=fence_token+1,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND seat_id=%s
                      AND lease_owner=%s
                      AND ownership_epoch=%s
                      AND fence_token=%s
                      AND fabric_state='CANCEL_REQUESTED'
                      AND cancel_requested_at IS NOT NULL
                    RETURNING *
                    """,
                    (
                        target_status,
                        target_fabric,
                        blockers,
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                    ),
                )
                updated = cur.fetchone()
                if updated is None:
                    cur.execute(
                        """
                        SELECT fabric_state,cancel_requested_at
                        FROM jaytec_jobs
                        WHERE job_id=%s
                        """,
                        (token.job_id,),
                    )
                    current = cur.fetchone()
                    if (
                        current is not None
                        and (
                            current["fabric_state"] == "CANCEL_REQUESTED"
                            or current["cancel_requested_at"] is not None
                        )
                    ):
                        return {
                            "requeued": False,
                            "reason": "CANCEL_REQUESTED",
                        }
                    raise FiveSeatStaleLease(token.job_id)
                self._free_seat(cur, token, last_ref=None)
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,%s,'FIVE_SEAT_REMEDIES',%s::jsonb)
                    """,
                    (
                        token.job_id,
                        (
                            "FABRIC_CANCEL_QUARANTINED"
                            if unresolved
                            else "FABRIC_CANCELLED"
                        ),
                        _json({"unresolved_operations": unresolved}),
                    ),
                )
                cur.execute(
                    "SELECT pg_notify(%s,%s)",
                    (
                        WORK_AVAILABLE_CHANNEL,
                        _json({"reason":"seat_released_after_cancel","job_id":token.job_id}),
                    ),
                )
                return dict(updated)

    def safe_retry(
        self,
        token: FiveSeatLeaseToken,
        *,
        worker_kind: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> Dict[str, Any]:
        """Requeue only a verified no-side-effect transient failure."""
        delay = max(1, min(int(delay_seconds), 3600))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )
                job = self._assert_worker(cur, token)
                if (
                    job.get("fabric_state") == "CANCEL_REQUESTED"
                    or job.get("cancel_requested_at") is not None
                ):
                    return {"requeued": False, "reason": "CANCEL_REQUESTED"}
                unresolved = self._unresolved_operations(cur, token.job_id)
                if unresolved:
                    return {
                        "requeued": False,
                        "reason": "UNRESOLVED_SIDE_EFFECT",
                        "unresolved_operations": unresolved,
                    }
                attempt_count = int(job.get("fabric_attempt_count") or 0)
                max_attempts = int(job.get("fabric_max_attempts") or 1)
                if attempt_count >= max_attempts:
                    return {
                        "requeued": False,
                        "reason": "RETRY_BUDGET_EXHAUSTED",
                        "attempt_count": attempt_count,
                        "max_attempts": max_attempts,
                    }

                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status='PAUSED',
                        fabric_state='QUEUED',
                        health='DEGRADED',
                        blockers=%s::jsonb,
                        seat_id=NULL,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        execution_room_id=NULL,
                        next_attempt_at=now()+(%s*interval '1 second'),
                        ownership_epoch=ownership_epoch+1,
                        fence_token=fence_token+1,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND seat_id=%s
                      AND lease_owner=%s
                      AND ownership_epoch=%s
                      AND fence_token=%s
                      AND fabric_state='RUNNING'
                      AND cancel_requested_at IS NULL
                    RETURNING *
                    """,
                    (
                        _json([{"source":"SAFE_RETRY","error":dict(error)}]),
                        delay,
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                    ),
                )
                updated = cur.fetchone()
                if updated is None:
                    raise FiveSeatStaleLease(token.job_id)
                self._free_seat(cur, token, last_ref=None)
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,'FABRIC_SAFE_RETRY_QUEUED','FIVE_SEAT_REMEDIES',%s::jsonb)
                    """,
                    (
                        token.job_id,
                        _json(
                            {
                                "worker_kind": str(worker_kind).upper(),
                                "delay_seconds": delay,
                                "attempt_count": attempt_count,
                                "max_attempts": max_attempts,
                            }
                        ),
                    ),
                )
                cur.execute(
                    "SELECT pg_notify(%s,%s)",
                    (
                        WORK_AVAILABLE_CHANNEL,
                        _json({"reason":"safe_retry_queued","job_id":token.job_id}),
                    ),
                )
                return {"requeued": True, "job": dict(updated)}

    def record_adapter_failure(
        self,
        worker_kind: str,
        *,
        error: Mapping[str, Any],
        cooldown_seconds: int = 60,
    ) -> Dict[str, Any]:
        kind = str(worker_kind or "").strip().upper()
        if not kind:
            raise ValueError("worker_kind is required")
        cooldown = max(5, min(int(cooldown_seconds), 3600))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_fabric_circuits(
                      worker_kind,state,consecutive_failures,failure_threshold,
                      open_until,probe_job_id,probe_started_at,last_failure
                    ) VALUES (%s,'CLOSED',1,3,NULL,NULL,NULL,%s::jsonb)
                    ON CONFLICT(worker_kind) DO UPDATE SET
                      consecutive_failures=jaytec_fabric_circuits.consecutive_failures+1,
                      last_failure=EXCLUDED.last_failure,
                      state=CASE
                        WHEN jaytec_fabric_circuits.state='HALF_OPEN'
                          OR jaytec_fabric_circuits.consecutive_failures+1
                             >= jaytec_fabric_circuits.failure_threshold
                        THEN 'OPEN'
                        ELSE 'CLOSED'
                      END,
                      open_until=CASE
                        WHEN jaytec_fabric_circuits.state='HALF_OPEN'
                          OR jaytec_fabric_circuits.consecutive_failures+1
                             >= jaytec_fabric_circuits.failure_threshold
                        THEN now()+(%s*interval '1 second')
                        ELSE NULL
                      END,
                      probe_job_id=NULL,
                      probe_started_at=NULL,
                      version=jaytec_fabric_circuits.version+1,
                      updated_at=now()
                    RETURNING *
                    """,
                    (kind, _json(dict(error)), cooldown),
                )
                return dict(cur.fetchone())

    def record_adapter_success(self, worker_kind: str) -> Dict[str, Any]:
        kind = str(worker_kind or "").strip().upper()
        if not kind:
            raise ValueError("worker_kind is required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_fabric_circuits(
                      worker_kind,state,consecutive_failures,failure_threshold,
                      open_until,probe_job_id,probe_started_at
                    ) VALUES (%s,'CLOSED',0,3,NULL,NULL,NULL)
                    ON CONFLICT(worker_kind) DO UPDATE SET
                      state='CLOSED',
                      consecutive_failures=0,
                      open_until=NULL,
                      probe_job_id=NULL,
                      probe_started_at=NULL,
                      last_failure=NULL,
                      version=jaytec_fabric_circuits.version+1,
                      updated_at=now()
                    RETURNING *
                    """,
                    (kind,),
                )
                return dict(cur.fetchone())

    def quarantine_current(
        self,
        token: FiveSeatLeaseToken,
        *,
        reason: str,
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Fail closed, invalidate worker, free seat, block only this job/resource."""
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )
                self._assert_worker(cur, token)
                unresolved = self._unresolved_operations(cur, token.job_id)
                blocker = {
                    "source": "FABRIC_QUARANTINE",
                    "reason": str(reason or "UNKNOWN"),
                    "evidence": dict(evidence or {}),
                    "unresolved_operations": unresolved,
                }
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status='BLOCKED',
                        fabric_state='QUARANTINED',
                        health='FAILED_SAFE',
                        blockers=%s::jsonb,
                        seat_id=NULL,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        execution_room_id=NULL,
                        ownership_epoch=ownership_epoch+1,
                        fence_token=fence_token+1,
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
                        _json([blocker]),
                        token.job_id,
                        token.seat_id,
                        token.owner,
                        token.ownership_epoch,
                        token.job_fence_token,
                    ),
                )
                updated = cur.fetchone()
                if updated is None:
                    raise FiveSeatStaleLease(token.job_id)
                self._free_seat(cur, token, last_ref=None)
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,'FABRIC_JOB_QUARANTINED','FIVE_SEAT_REMEDIES',%s::jsonb)
                    """,
                    (token.job_id, _json(blocker)),
                )
                cur.execute(
                    "SELECT pg_notify(%s,%s)",
                    (
                        WORK_AVAILABLE_CHANNEL,
                        _json({"reason":"quarantine_freed_seat","job_id":token.job_id}),
                    ),
                )
                return dict(updated)

    def reconcile_expired_leases(self, *, limit: int = 100) -> list[Dict[str, Any]]:
        """Fence dead workers and recover only when external state is unambiguous."""
        bounded = max(1, min(int(limit), 500))
        results: list[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (SCHEDULER_LOCK_KEY,),
                )
                cur.execute(
                    """
                    SELECT s.*,j.status AS job_status,j.fabric_state,
                           j.fabric_attempt_count,j.fabric_max_attempts,
                           j.cancel_requested_at,j.source_shared_state_version,
                           e.worker_kind,e.payload AS envelope_payload
                    FROM jaytec_worker_seats s
                    LEFT JOIN jaytec_jobs j ON j.job_id=s.current_job_id
                    LEFT JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    WHERE s.state='RUNNING'
                      AND s.lease_expires_at IS NOT NULL
                      AND s.lease_expires_at <= now()
                    ORDER BY s.seat_id
                    LIMIT %s
                    FOR UPDATE OF s
                    """,
                    (bounded,),
                )
                seats = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT current_shared_state_version
                    FROM jaytec_fabric_authority_state
                    WHERE authority_id='FABRIC'
                    """
                )
                authority = cur.fetchone()
                current_shared_state_version = int(
                    authority["current_shared_state_version"]
                ) if authority is not None else 0

                for seat in seats:
                    job_id = str(seat.get("current_job_id") or "")
                    if not job_id:
                        cur.execute(
                            """
                            UPDATE jaytec_worker_seats
                            SET state='FREE',worker_id=NULL,lease_owner=NULL,
                                lease_expires_at=NULL,seat_epoch=seat_epoch+1,
                                fence_token=fence_token+1,version=version+1,updated_at=now()
                            WHERE seat_id=%s
                            """,
                            (seat["seat_id"],),
                        )
                        results.append({"seat_id":seat["seat_id"],"action":"ORPHAN_SEAT_FREED"})
                        continue

                    cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
                    job = cur.fetchone()
                    if job is None:
                        cur.execute(
                            """
                            UPDATE jaytec_worker_seats
                            SET state='QUARANTINED',worker_id=NULL,lease_owner=NULL,
                                lease_expires_at=NULL,seat_epoch=seat_epoch+1,
                                fence_token=fence_token+1,version=version+1,updated_at=now()
                            WHERE seat_id=%s
                            """,
                            (seat["seat_id"],),
                        )
                        results.append({"seat_id":seat["seat_id"],"action":"MISSING_JOB_SEAT_QUARANTINED"})
                        continue

                    unresolved = self._unresolved_operations(cur, job_id)
                    cancelled = (
                        job["fabric_state"] == "CANCEL_REQUESTED"
                        or job.get("cancel_requested_at") is not None
                    )
                    stale_source = (
                        current_shared_state_version <= 0
                        or int(job.get("source_shared_state_version") or -1)
                           != current_shared_state_version
                    )
                    attempts = int(job.get("fabric_attempt_count") or 0)
                    max_attempts = int(job.get("fabric_max_attempts") or 1)
                    worker_kind = str(seat.get("worker_kind") or "").upper()
                    # Fabric/lease retries are independent from a TaskPacket's
                    # specialist-internal max_retries. A packet may deliberately
                    # set max_retries=0 while the durable worker fabric still has
                    # multiple safe transport/lease recovery attempts available.

                    if stale_source:
                        new_status = "BLOCKED"
                        new_fabric = "STALE"
                        next_attempt = None
                        action = "EXPIRED_WORKER_STALE_SOURCE"
                    elif unresolved:
                        new_status = "BLOCKED"
                        new_fabric = "QUARANTINED"
                        next_attempt = None
                        action = "EXPIRED_WORKER_QUARANTINED"
                    elif cancelled:
                        new_status = "CANCELED"
                        new_fabric = "CANCELLED"
                        next_attempt = None
                        action = "EXPIRED_WORKER_CANCELLED"
                    elif attempts < max_attempts:
                        new_status = "PAUSED"
                        new_fabric = "QUEUED"
                        next_attempt = retry_delay_seconds(attempts)
                        action = "EXPIRED_WORKER_REQUEUED"
                    else:
                        new_status = "FAILED_SAFE"
                        new_fabric = "FAILED_SAFE"
                        next_attempt = None
                        action = "EXPIRED_WORKER_RETRY_BUDGET_EXHAUSTED"

                    blockers = _json(
                        [
                            {
                                "source": "EXPIRED_WORKER_RECONCILIATION",
                                "action": action,
                                "unresolved_operations": unresolved,
                            }
                        ]
                    )
                    cur.execute(
                        """
                        UPDATE jaytec_jobs
                        SET status=%s,fabric_state=%s,blockers=%s::jsonb,
                            seat_id=NULL,lease_owner=NULL,lease_expires_at=NULL,
                            execution_room_id=NULL,
                            next_attempt_at=CASE
                              WHEN %s IS NULL THEN NULL
                              ELSE now()+(%s*interval '1 second')
                            END,
                            ownership_epoch=ownership_epoch+1,
                            fence_token=fence_token+1,
                            version=version+1,updated_at=now()
                        WHERE job_id=%s
                        RETURNING job_id
                        """,
                        (
                            new_status,
                            new_fabric,
                            blockers,
                            next_attempt,
                            next_attempt or 0,
                            job_id,
                        ),
                    )
                    cur.execute(
                        """
                        UPDATE jaytec_worker_seats
                        SET state='FREE',current_job_id=NULL,worker_id=NULL,
                            lease_owner=NULL,lease_expires_at=NULL,
                            seat_epoch=seat_epoch+1,fence_token=fence_token+1,
                            version=version+1,updated_at=now()
                        WHERE seat_id=%s
                        """,
                        (seat["seat_id"],),
                    )
                    cur.execute(
                        """
                        INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                        VALUES (%s,%s,'FIVE_SEAT_REMEDIES',%s::jsonb)
                        """,
                        (
                            job_id,
                            action,
                            _json(
                                {
                                    "seat_id": seat["seat_id"],
                                    "unresolved_operations": unresolved,
                                    "attempt_count": attempts,
                                    "max_attempts": max_attempts,
                                }
                            ),
                        ),
                    )
                    if worker_kind:
                        cur.execute(
                            """
                            UPDATE jaytec_fabric_circuits
                            SET state='OPEN',
                                consecutive_failures=consecutive_failures+1,
                                open_until=now()+(60*interval '1 second'),
                                probe_job_id=NULL,
                                probe_started_at=NULL,
                                last_failure=%s::jsonb,
                                version=version+1,
                                updated_at=now()
                            WHERE worker_kind=%s
                              AND state='HALF_OPEN'
                              AND probe_job_id=%s
                            """,
                            (
                                _json(
                                    {
                                        "reason":"HALF_OPEN_PROBE_WORKER_EXPIRED",
                                        "job_id":job_id,
                                    }
                                ),
                                worker_kind,
                                job_id,
                            ),
                        )
                    results.append(
                        {
                            "job_id": job_id,
                            "seat_id": seat["seat_id"],
                            "action": action,
                        }
                    )

                cur.execute(
                    """
                    UPDATE jaytec_fabric_circuits c
                    SET state='OPEN',
                        consecutive_failures=consecutive_failures+1,
                        open_until=now()+(60*interval '1 second'),
                        probe_job_id=NULL,
                        probe_started_at=NULL,
                        last_failure=%s::jsonb,
                        version=version+1,
                        updated_at=now()
                    WHERE c.state='HALF_OPEN'
                      AND (
                        c.probe_job_id IS NULL
                        OR NOT EXISTS (
                          SELECT 1
                          FROM jaytec_jobs j
                          WHERE j.job_id=c.probe_job_id
                            AND j.status='RUNNING'
                            AND j.lease_expires_at IS NOT NULL
                            AND j.lease_expires_at > now()
                        )
                      )
                    RETURNING worker_kind
                    """,
                    (_json({"reason":"ORPHAN_HALF_OPEN_RECOVERED"}),),
                )
                recovered_circuits = [
                    str(row["worker_kind"]) for row in cur.fetchall()
                ]
                for worker_kind in recovered_circuits:
                    results.append(
                        {
                            "worker_kind":worker_kind,
                            "action":"ORPHAN_HALF_OPEN_RECOVERED",
                        }
                    )

                if results:
                    cur.execute(
                        "SELECT pg_notify(%s,%s)",
                        (
                            WORK_AVAILABLE_CHANNEL,
                            _json({"reason":"expired_lease_reconciliation","count":len(results)}),
                        ),
                    )
        return results
