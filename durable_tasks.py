from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from concurrency import SCHEDULER_LOCK_KEY, concurrency_decision, duplicate_assignment
from job_runtime import LeaseToken
from orchestration import (
    PacketValidationError,
    SECRET_KEY_PATTERN,
    SECRET_VALUE_PATTERN,
    packet_hash,
    parse_packet_json,
    validate_packet,
)


ASSIGNMENT_TYPE = "SPECIALIST_PACKET"
TRANSIENT_OVERALL_STATUSES = {"TIMEOUT", "RATE_LIMITED"}
SUCCESS_OVERALL_STATUSES = {"SUCCESS", "PARTIAL_SUCCESS", "NEEDS_VALIDATION"}
INCIDENT_TYPES = {
    "MCP_DEPENDENCY_TIMEOUT",
    "MESSAGE_DELIVERY_TIMEOUT",
    "UI_DELIVERY_TIMEOUT",
    "SESSION_FREEZE",
    "DURABLE_WORKER_EXCEPTION",
    "GUARDIAN_LOOP_EXCEPTION",
    "SPECIALIST_TIMEOUT",
    "SPECIALIST_PROVIDER_FAILURE",
}


class DurableTaskError(RuntimeError):
    pass


class DurableTaskNotFound(DurableTaskError):
    pass


class DurableTaskConflict(DurableTaskError):
    pass


class DurableTaskFenceError(DurableTaskError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _safe_row(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result: Dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat()
        else:
            result[key] = value
    return result


def contains_secret_material(value: Any) -> bool:
    """Conservatively reject secrets before durable packet persistence."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if SECRET_KEY_PATTERN.search(str(key)):
                return True
            if contains_secret_material(child):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_secret_material(child) for child in value)
    if isinstance(value, str):
        return bool(SECRET_VALUE_PATTERN.search(value))
    return False


def should_cache_orchestration_result(result: Mapping[str, Any]) -> bool:
    """Transient whole-packet failures must not poison idempotent replay."""

    return str(result.get("overall_status") or "") not in TRANSIENT_OVERALL_STATUSES


def retry_delay_seconds(attempt_count: int) -> int:
    """Short bounded exponential retry delay for transient safe specialist work."""

    attempt = max(1, int(attempt_count))
    return min(60, 5 * (2 ** (attempt - 1)))


class DurableTaskQueue:
    """Durable packet submission/result layer on the canonical v1.3 job runtime.

    The jaytec_jobs row remains scheduler/lease/fence authority. This class only
    persists validated specialist packets/results and performs atomic state
    transitions guarded by the same ownership epoch + fence token.
    """

    def __init__(self, database_url: str, *, max_parallel: int = 4):
        if not database_url:
            raise ValueError("database_url is required")
        if max_parallel < 1 or max_parallel > 32:
            raise ValueError("max_parallel must be between 1 and 32")
        self.database_url = database_url
        self.max_parallel = max_parallel

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        sql = Path(__file__).with_name("job_runtime_schema.sql").read_text(encoding="utf-8")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def submit(
        self,
        packet_json: str,
        *,
        source_shared_state_version: int,
        priority: int = 100,
        source_execution_room_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        packet, parse_errors = parse_packet_json(packet_json)
        if packet is None:
            raise PacketValidationError(";".join(parse_errors))
        validation = validate_packet(packet)
        if not validation.ok:
            raise PacketValidationError(";".join(validation.errors))
        if contains_secret_material(packet):
            raise PacketValidationError("packet_contains_secret_material")
        if source_shared_state_version < 0:
            raise ValueError("source_shared_state_version must be >= 0")
        if priority < 0 or priority > 1_000_000:
            raise ValueError("priority out of range")

        digest = packet_hash(packet)
        idem = str(packet["idempotency_key"])
        job_id = "packet-" + hashlib.sha256(f"{idem}:{digest}".encode("utf-8")).hexdigest()[:24]
        objective = str(packet.get("intent") or packet.get("request") or "specialist packet")[:4000]
        max_attempts = min(5, max(1, int(packet.get("max_retries", 0)) + 1))
        specialist_plan = [str(item) for item in packet.get("specialist_plan", [])]

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM jaytec_task_packets WHERE idempotency_key=%s FOR UPDATE",
                    (idem,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if existing["packet_hash"] != digest:
                        raise DurableTaskConflict("CONFLICTING_DUPLICATE")
                    return self._snapshot_with_cursor(cur, str(existing["job_id"]))

                cur.execute(
                    """
                    INSERT INTO jaytec_jobs(
                      job_id,project_id,task_id,subtask_id,assignment_type,objective,
                      status,priority,source_shared_state_version,concurrency_class,
                      mutation_scope,read_scope,dependencies,resource_scope,health
                    ) VALUES (%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s,'A',
                              '[]'::jsonb,%s::jsonb,'[]'::jsonb,%s::jsonb,'HEALTHY')
                    """,
                    (
                        job_id,
                        str(packet.get("parent_task_id") or packet["task_id"]),
                        str(packet["task_id"]),
                        str(packet["subtask_id"]),
                        ASSIGNMENT_TYPE,
                        objective,
                        priority,
                        int(source_shared_state_version),
                        _json([f"specialist:{name}" for name in specialist_plan]),
                        _json({"specialists": specialist_plan}),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_task_packets(
                      job_id,packet_hash,idempotency_key,packet,status,max_attempts
                    ) VALUES (%s,%s,%s,%s::jsonb,'QUEUED',%s)
                    """,
                    (job_id, digest, idem, _json(packet), max_attempts),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                    VALUES (%s,'TASK_PACKET_SUBMITTED','DURABLE_TASK_QUEUE',%s,%s::jsonb)
                    """,
                    (
                        job_id,
                        int(source_shared_state_version),
                        _json({
                            "source_execution_room_id": source_execution_room_id,
                            "packet_hash": digest,
                            "specialist_plan": specialist_plan,
                        }),
                    ),
                )
                return self._snapshot_with_cursor(cur, job_id)

    def _snapshot_with_cursor(self, cur, job_id: str) -> Dict[str, Any]:
        cur.execute(
            """
            SELECT j.job_id,j.project_id,j.task_id,j.subtask_id,j.assignment_type,
                   j.status AS job_status,j.health,j.priority,j.source_shared_state_version,
                   j.ownership_epoch,j.fence_token,j.execution_room_id,j.lease_owner,
                   j.lease_expires_at,j.next_attempt_at,j.updated_at AS job_updated_at,
                   p.packet_hash,p.idempotency_key,p.status AS packet_status,
                   p.attempt_count,p.max_attempts,p.result,p.error,p.last_started_at,
                   p.completed_at,p.updated_at AS packet_updated_at
            FROM jaytec_jobs j
            JOIN jaytec_task_packets p ON p.job_id=j.job_id
            WHERE j.job_id=%s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise DurableTaskNotFound(job_id)
        return _safe_row(row) or {}

    def status(
        self,
        *,
        job_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not job_id and not idempotency_key:
            raise ValueError("job_id or idempotency_key is required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if job_id:
                    return self._snapshot_with_cursor(cur, job_id)
                cur.execute(
                    "SELECT job_id FROM jaytec_task_packets WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
                row = cur.fetchone()
                if row is None:
                    raise DurableTaskNotFound(str(idempotency_key))
                return self._snapshot_with_cursor(cur, str(row["job_id"]))

    def _active_runtime_state(self, cur):
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
        cur.execute(
            "SELECT DISTINCT job_id FROM jaytec_operations WHERE status IN ('IN_FLIGHT','UNCERTAIN_PARTIAL')"
        )
        unresolved = {str(row["job_id"]) for row in cur.fetchall()}
        cur.execute("SELECT job_id FROM jaytec_jobs WHERE status='SUCCEEDED'")
        completed = {str(row["job_id"]) for row in cur.fetchall()}
        return active, unresolved, completed

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
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEDULER_LOCK_KEY,))
                active, unresolved, completed = self._active_runtime_state(cur)
                if len(active) >= self.max_parallel:
                    return None

                cur.execute(
                    """
                    SELECT j.*,p.packet,p.packet_hash,p.idempotency_key,
                           p.status AS packet_status,p.attempt_count,p.max_attempts
                    FROM jaytec_jobs j
                    JOIN jaytec_task_packets p ON p.job_id=j.job_id
                    WHERE j.assignment_type=%s
                      AND j.status IN ('QUEUED','PAUSED')
                      AND p.status IN ('QUEUED','RUNNING')
                      AND (j.lease_expires_at IS NULL OR j.lease_expires_at < now())
                      AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= now())
                    ORDER BY j.priority ASC,j.created_at ASC,j.job_id ASC
                    FOR UPDATE OF j SKIP LOCKED
                    """,
                    (ASSIGNMENT_TYPE,),
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
                        SET status='RUNNING',lease_owner=%s,
                            lease_expires_at=now() + (%s * interval '1 second'),
                            execution_room_id=%s,next_attempt_at=NULL,
                            ownership_epoch=ownership_epoch+1,
                            fence_token=fence_token+1,version=version+1,updated_at=now()
                        WHERE job_id=%s
                          AND status IN ('QUEUED','PAUSED')
                          AND (lease_expires_at IS NULL OR lease_expires_at < now())
                          AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                        RETURNING *
                        """,
                        (owner, lease_seconds, execution_room_id, candidate["job_id"]),
                    )
                    claimed = cur.fetchone()
                    if claimed is None:
                        continue
                    cur.execute(
                        """
                        UPDATE jaytec_task_packets
                        SET status='RUNNING',attempt_count=attempt_count+1,
                            last_started_at=now(),updated_at=now()
                        WHERE job_id=%s
                        RETURNING packet,packet_hash,idempotency_key,status AS packet_status,
                                  attempt_count,max_attempts
                        """,
                        (candidate["job_id"],),
                    )
                    packet_row = cur.fetchone()
                    if packet_row is None:
                        raise DurableTaskNotFound(str(candidate["job_id"]))
                    cur.execute(
                        """
                        INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                        VALUES (%s,'TASK_PACKET_CLAIMED','DURABLE_TASK_WORKER',%s,%s::jsonb)
                        """,
                        (
                            candidate["job_id"],
                            claimed.get("source_shared_state_version"),
                            _json({
                                "owner": owner,
                                "execution_room_id": execution_room_id,
                                "attempt_count": packet_row["attempt_count"],
                            }),
                        ),
                    )
                    combined = dict(claimed)
                    combined.update(dict(packet_row))
                    return combined
                return None

    @staticmethod
    def token_from_claim(claimed: Mapping[str, Any]) -> LeaseToken:
        return LeaseToken(
            job_id=str(claimed["job_id"]),
            owner=str(claimed["lease_owner"]),
            ownership_epoch=int(claimed["ownership_epoch"]),
            fence_token=int(claimed["fence_token"]),
            version=int(claimed["version"]),
            lease_expires_at=claimed["lease_expires_at"],
        )

    def finish(
        self,
        token: LeaseToken,
        *,
        result: Mapping[str, Any],
        succeeded: bool,
    ) -> Dict[str, Any]:
        packet_status = "SUCCEEDED" if succeeded else "FAILED_SAFE"
        job_status = "SUCCEEDED" if succeeded else "FAILED_SAFE"
        health = "HEALTHY" if succeeded else "FAILED_SAFE"
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status=%s,health=%s,latest_verified_result=%s::jsonb,
                        lease_owner=NULL,lease_expires_at=NULL,execution_room_id=NULL,
                        next_attempt_at=NULL,version=version+1,updated_at=now()
                    WHERE job_id=%s AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING job_id
                    """,
                    (
                        job_status,
                        health,
                        _json(dict(result)),
                        token.job_id,
                        token.owner,
                        token.ownership_epoch,
                        token.fence_token,
                    ),
                )
                if cur.fetchone() is None:
                    raise DurableTaskFenceError(token.job_id)
                cur.execute(
                    """
                    UPDATE jaytec_task_packets
                    SET status=%s,result=%s::jsonb,error=NULL,completed_at=now(),updated_at=now()
                    WHERE job_id=%s
                    """,
                    (packet_status, _json(dict(result)), token.job_id),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,%s,'DURABLE_TASK_WORKER',%s::jsonb)
                    """,
                    (
                        token.job_id,
                        "TASK_PACKET_SUCCEEDED" if succeeded else "TASK_PACKET_FAILED_SAFE",
                        _json({"overall_status": result.get("overall_status")}),
                    ),
                )
                return self._snapshot_with_cursor(cur, token.job_id)

    def requeue(
        self,
        token: LeaseToken,
        *,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> Dict[str, Any]:
        delay = max(1, min(int(delay_seconds), 3600))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status='PAUSED',health='DEGRADED',lease_owner=NULL,
                        lease_expires_at=NULL,execution_room_id=NULL,
                        next_attempt_at=now() + (%s * interval '1 second'),
                        ownership_epoch=ownership_epoch+1,fence_token=fence_token+1,
                        version=version+1,updated_at=now()
                    WHERE job_id=%s AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING job_id
                    """,
                    (
                        delay,
                        token.job_id,
                        token.owner,
                        token.ownership_epoch,
                        token.fence_token,
                    ),
                )
                if cur.fetchone() is None:
                    raise DurableTaskFenceError(token.job_id)
                cur.execute(
                    """
                    UPDATE jaytec_task_packets
                    SET status='QUEUED',error=%s::jsonb,updated_at=now()
                    WHERE job_id=%s
                    """,
                    (_json(dict(error)), token.job_id),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,'TASK_PACKET_REQUEUED','DURABLE_TASK_WORKER',%s::jsonb)
                    """,
                    (token.job_id, _json({"delay_seconds": delay, "error": dict(error)})),
                )
                return self._snapshot_with_cursor(cur, token.job_id)

    def record_incident(
        self,
        event_type: str,
        *,
        detail: Mapping[str, Any],
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if event_type not in INCIDENT_TYPES:
            raise ValueError("unsupported incident type")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
                    VALUES (%s,%s,'RELIABILITY_RUNTIME',%s::jsonb)
                    RETURNING *
                    """,
                    (job_id, event_type, _json(dict(detail))),
                )
                row = cur.fetchone()
        return _safe_row(row) or {}

    def stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT status,count(*) AS n FROM jaytec_jobs GROUP BY status")
                job_counts = {str(r["status"]): int(r["n"]) for r in cur.fetchall()}
                cur.execute("SELECT status,count(*) AS n FROM jaytec_task_packets GROUP BY status")
                packet_counts = {str(r["status"]): int(r["n"]) for r in cur.fetchall()}
                cur.execute(
                    "SELECT status,count(*) AS n FROM jaytec_guardian_findings GROUP BY status"
                )
                guardian_counts = {str(r["status"]): int(r["n"]) for r in cur.fetchall()}
                cur.execute(
                    """
                    SELECT event_type,count(*) AS n FROM jaytec_job_events
                    WHERE event_type IN ('MCP_DEPENDENCY_TIMEOUT','MESSAGE_DELIVERY_TIMEOUT',
                                         'UI_DELIVERY_TIMEOUT','SESSION_FREEZE',
                                         'SPECIALIST_TIMEOUT','SPECIALIST_PROVIDER_FAILURE',
                                         'DURABLE_WORKER_EXCEPTION','GUARDIAN_LOOP_EXCEPTION')
                      AND created_at > now() - interval '24 hours'
                    GROUP BY event_type
                    """
                )
                incident_counts = {str(r["event_type"]): int(r["n"]) for r in cur.fetchall()}
        return {
            "jobs": job_counts,
            "task_packets": packet_counts,
            "guardian_findings": guardian_counts,
            "recent_incidents_24h": incident_counts,
        }


class DurableTaskWorker:
    """Background worker that turns long specialist calls into pollable jobs."""

    def __init__(
        self,
        queue: DurableTaskQueue,
        execute_packet: Callable[[str], Mapping[str, Any]],
        *,
        owner: str,
        execution_room_id: str,
        poll_seconds: float = 1.0,
        lease_seconds: int = 300,
    ):
        self.queue = queue
        self.execute_packet = execute_packet
        self.owner = owner
        self.execution_room_id = execution_room_id
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.lease_seconds = int(lease_seconds)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> bool:
        claimed = self.queue.claim_next(
            owner=self.owner,
            execution_room_id=self.execution_room_id,
            lease_seconds=self.lease_seconds,
        )
        if claimed is None:
            return False
        token = self.queue.token_from_claim(claimed)
        attempt_count = int(claimed.get("attempt_count") or 1)
        max_attempts = int(claimed.get("max_attempts") or 1)
        packet = claimed.get("packet")
        try:
            raw = self.execute_packet(_json(packet))
            result = dict(raw)
        except Exception as exc:
            error = {"error_class": type(exc).__name__, "message": str(exc)[:1000]}
            if attempt_count < max_attempts:
                self.queue.requeue(
                    token,
                    error=error,
                    delay_seconds=retry_delay_seconds(attempt_count),
                )
            else:
                self.queue.finish(
                    token,
                    result={
                        "overall_status": "FAILED_CLOSED",
                        "unresolved_items": [f"durable_worker_exception:{type(exc).__name__}"],
                    },
                    succeeded=False,
                )
            self.queue.record_incident(
                "DURABLE_WORKER_EXCEPTION",
                job_id=token.job_id,
                detail=error,
            )
            return True

        overall = str(result.get("overall_status") or "FAILED_CLOSED")
        if overall in TRANSIENT_OVERALL_STATUSES and attempt_count < max_attempts:
            self.queue.requeue(
                token,
                error={"overall_status": overall, "result": result},
                delay_seconds=retry_delay_seconds(attempt_count),
            )
            incident = "SPECIALIST_TIMEOUT" if overall == "TIMEOUT" else "SPECIALIST_PROVIDER_FAILURE"
            self.queue.record_incident(
                incident,
                job_id=token.job_id,
                detail={"overall_status": overall, "attempt_count": attempt_count},
            )
            return True

        self.queue.finish(
            token,
            result=result,
            succeeded=overall in SUCCESS_OVERALL_STATUSES,
        )
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:
                try:
                    self.queue.record_incident(
                        "DURABLE_WORKER_EXCEPTION",
                        detail={"error_class": type(exc).__name__, "message": str(exc)[:1000]},
                    )
                except Exception:
                    pass
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="jaytec-durable-task-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
