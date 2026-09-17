from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import psycopg2
import psycopg2.extras


ACTIVE_JOB_STATUSES = ("QUEUED", "RUNNING", "PAUSED", "BLOCKED")
TERMINAL_JOB_STATUSES = ("SUCCEEDED", "FAILED_SAFE", "CANCELED")
OPERATION_STATUSES = (
    "PREPARED",
    "IN_FLIGHT",
    "VERIFIED_COMPLETE",
    "VERIFIED_NOT_DONE",
    "UNCERTAIN_PARTIAL",
    "FAILED_SAFE",
)


class JobRuntimeError(RuntimeError):
    pass


class JobNotFoundError(JobRuntimeError):
    pass


class LeaseUnavailableError(JobRuntimeError):
    pass


class StaleFenceError(JobRuntimeError):
    pass


class VersionConflictError(JobRuntimeError):
    pass


class OperationConflictError(JobRuntimeError):
    pass


class UnsafeRetryError(JobRuntimeError):
    pass


@dataclass(frozen=True)
class LeaseToken:
    job_id: str
    owner: str
    ownership_epoch: int
    fence_token: int
    version: int
    lease_expires_at: datetime


def operation_transition_allowed(current: str, target: str) -> bool:
    """Return whether an operation status transition is mechanically safe.

    UNCERTAIN_PARTIAL is deliberately terminal for automatic progression. A
    caller must verify destination state and then explicitly classify it as
    VERIFIED_COMPLETE / VERIFIED_NOT_DONE / FAILED_SAFE rather than blindly
    putting it back IN_FLIGHT.
    """

    allowed = {
        "PREPARED": {"IN_FLIGHT", "VERIFIED_NOT_DONE", "FAILED_SAFE"},
        "IN_FLIGHT": {
            "VERIFIED_COMPLETE",
            "VERIFIED_NOT_DONE",
            "UNCERTAIN_PARTIAL",
            "FAILED_SAFE",
        },
        "UNCERTAIN_PARTIAL": {
            "VERIFIED_COMPLETE",
            "VERIFIED_NOT_DONE",
            "FAILED_SAFE",
        },
        "VERIFIED_COMPLETE": set(),
        "VERIFIED_NOT_DONE": set(),
        "FAILED_SAFE": set(),
    }
    return target in allowed.get(current, set())


def stale_fence(*, expected_epoch: int, expected_fence: int, actual_epoch: int, actual_fence: int) -> bool:
    return expected_epoch != actual_epoch or expected_fence != actual_fence


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


class PostgresJobRuntime:
    """Durable operational state for JAYTEC jobs.

    Shared House remains the semantic/canonical project contract. This class
    owns only machine-operational state: leases, epochs/fences, steps,
    idempotent operations, events and health observations.

    All material mutations are guarded by monotonic ownership_epoch +
    fence_token and (where relevant) optimistic version checks. A stale room
    cannot mutate after another worker reclaims the job.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        schema_path = Path(__file__).with_name("job_runtime_schema.sql")
        sql = schema_path.read_text(encoding="utf-8")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def create_job(
        self,
        *,
        job_id: str,
        project_id: str,
        task_id: str,
        assignment_type: str,
        objective: str,
        source_shared_state_version: int,
        subtask_id: Optional[str] = None,
        stage: Optional[str] = None,
        priority: int = 100,
        execution_room_id: Optional[str] = None,
        concurrency_class: Optional[str] = None,
        mutation_scope: Optional[Iterable[str]] = None,
        read_scope: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        resource_scope: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if concurrency_class not in (None, "A", "B", "C", "D", "E"):
            raise ValueError("invalid concurrency_class")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_jobs (
                        job_id, project_id, task_id, subtask_id, assignment_type,
                        objective, status, stage, priority,
                        source_shared_state_version, execution_room_id,
                        concurrency_class, mutation_scope, read_scope,
                        dependencies, resource_scope, health
                    ) VALUES (%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s,%s,%s,%s,
                              %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,'HEALTHY')
                    ON CONFLICT (job_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        job_id,
                        project_id,
                        task_id,
                        subtask_id,
                        assignment_type,
                        objective,
                        stage,
                        priority,
                        source_shared_state_version,
                        execution_room_id,
                        concurrency_class,
                        _json(list(mutation_scope or [])),
                        _json(list(read_scope or [])),
                        _json(list(dependencies or [])),
                        _json(dict(resource_scope or {})),
                    ),
                )
                created = cur.fetchone()
                if created is not None:
                    return _row(created) or {}
                cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s", (job_id,))
                existing = cur.fetchone()
                if existing is None:
                    raise JobRuntimeError("job insert lost without existing row")
                return _row(existing) or {}

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s", (job_id,))
                row = cur.fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _row(row) or {}

    def list_active_jobs(self, *, limit: int = 100) -> list[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM jaytec_jobs
                    WHERE status = ANY(%s)
                    ORDER BY priority ASC, updated_at ASC
                    LIMIT %s
                    """,
                    (list(ACTIVE_JOB_STATUSES), bounded),
                )
                rows = cur.fetchall()
        return [_row(row) or {} for row in rows]

    def claim_job(
        self,
        job_id: str,
        *,
        owner: str,
        lease_seconds: int = 300,
        expected_version: Optional[int] = None,
        execution_room_id: Optional[str] = None,
    ) -> LeaseToken:
        if lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        version_clause = "" if expected_version is None else "AND version = %s"
        params: list[Any] = [owner, lease_seconds, execution_room_id, job_id, owner]
        if expected_version is not None:
            params.append(expected_version)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    UPDATE jaytec_jobs
                    SET status='RUNNING',
                        lease_owner=%s,
                        lease_expires_at=now() + (%s * interval '1 second'),
                        execution_room_id=COALESCE(%s, execution_room_id),
                        ownership_epoch=ownership_epoch+1,
                        fence_token=fence_token+1,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND status IN ('QUEUED','RUNNING','PAUSED')
                      AND (lease_expires_at IS NULL OR lease_expires_at < now() OR lease_owner=%s)
                      {version_clause}
                    RETURNING job_id, lease_owner, ownership_epoch, fence_token,
                              version, lease_expires_at
                    """,
                    params,
                )
                row = cur.fetchone()
        if row is None:
            try:
                self.get_job(job_id)
            except JobNotFoundError:
                raise
            raise LeaseUnavailableError(job_id)
        return LeaseToken(
            job_id=row["job_id"],
            owner=row["lease_owner"],
            ownership_epoch=int(row["ownership_epoch"]),
            fence_token=int(row["fence_token"]),
            version=int(row["version"]),
            lease_expires_at=row["lease_expires_at"],
        )

    def heartbeat(self, token: LeaseToken, *, lease_seconds: int = 300) -> LeaseToken:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET lease_expires_at=now() + (%s * interval '1 second'),
                        version=version+1, updated_at=now()
                    WHERE job_id=%s AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING job_id, lease_owner, ownership_epoch, fence_token,
                              version, lease_expires_at
                    """,
                    (
                        lease_seconds,
                        token.job_id,
                        token.owner,
                        token.ownership_epoch,
                        token.fence_token,
                    ),
                )
                row = cur.fetchone()
        if row is None:
            raise StaleFenceError(token.job_id)
        return LeaseToken(
            job_id=row["job_id"], owner=row["lease_owner"],
            ownership_epoch=int(row["ownership_epoch"]),
            fence_token=int(row["fence_token"]), version=int(row["version"]),
            lease_expires_at=row["lease_expires_at"],
        )

    def add_step(
        self,
        token: LeaseToken,
        *,
        step_id: str,
        ordinal: int,
        step_type: str,
        max_attempts: int = 1,
        idempotency_key: Optional[str] = None,
        input_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                self._assert_fence(cur, token)
                cur.execute(
                    """
                    INSERT INTO jaytec_job_steps (
                      job_id, step_id, ordinal, step_type, status, max_attempts,
                      idempotency_key, input_digest
                    ) VALUES (%s,%s,%s,%s,'READY',%s,%s,%s)
                    ON CONFLICT (job_id, step_id) DO UPDATE SET
                      updated_at=jaytec_job_steps.updated_at
                    RETURNING *
                    """,
                    (
                        token.job_id, step_id, ordinal, step_type, max_attempts,
                        idempotency_key, input_digest,
                    ),
                )
                row = cur.fetchone()
        return _row(row) or {}

    def checkpoint_job(
        self,
        token: LeaseToken,
        *,
        checkpoint_ref: str,
        latest_verified_result: Mapping[str, Any],
        health: str = "HEALTHY",
    ) -> LeaseToken:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET checkpoint_ref=%s,
                        latest_verified_result=%s::jsonb,
                        health=%s,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING job_id, lease_owner, ownership_epoch, fence_token,
                              version, lease_expires_at
                    """,
                    (
                        checkpoint_ref, _json(dict(latest_verified_result)), health,
                        token.job_id, token.owner, token.ownership_epoch, token.fence_token,
                    ),
                )
                row = cur.fetchone()
        if row is None:
            raise StaleFenceError(token.job_id)
        return LeaseToken(
            job_id=row["job_id"], owner=row["lease_owner"],
            ownership_epoch=int(row["ownership_epoch"]),
            fence_token=int(row["fence_token"]), version=int(row["version"]),
            lease_expires_at=row["lease_expires_at"],
        )

    def prepare_operation(
        self,
        token: LeaseToken,
        *,
        operation_id: str,
        operation_type: str,
        target: str,
        intended_effect: str,
        source_shared_state_version: int,
        idempotency_key: str,
        step_id: Optional[str] = None,
        preconditions: Optional[Mapping[str, Any]] = None,
        execution_room_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                self._assert_fence(cur, token)
                cur.execute(
                    "SELECT * FROM jaytec_operations WHERE idempotency_key=%s FOR UPDATE",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    same = (
                        existing["job_id"] == token.job_id
                        and existing["operation_type"] == operation_type
                        and existing["target"] == target
                        and existing["intended_effect"] == intended_effect
                    )
                    if not same:
                        raise OperationConflictError(idempotency_key)
                    return _row(existing) or {}
                cur.execute(
                    """
                    INSERT INTO jaytec_operations (
                      operation_id, job_id, step_id, operation_type, target,
                      intended_effect, source_shared_state_version,
                      execution_room_id, ownership_epoch, fence_token,
                      idempotency_key, status, preconditions
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PREPARED',%s::jsonb)
                    RETURNING *
                    """,
                    (
                        operation_id, token.job_id, step_id, operation_type, target,
                        intended_effect, source_shared_state_version,
                        execution_room_id, token.ownership_epoch, token.fence_token,
                        idempotency_key, _json(dict(preconditions or {})),
                    ),
                )
                row = cur.fetchone()
        return _row(row) or {}

    def transition_operation(
        self,
        token: LeaseToken,
        operation_id: str,
        *,
        target_status: str,
        evidence: Optional[Mapping[str, Any]] = None,
        retry_decision: Optional[str] = None,
    ) -> Dict[str, Any]:
        if target_status not in OPERATION_STATUSES:
            raise ValueError("invalid operation status")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                self._assert_fence(cur, token)
                cur.execute(
                    "SELECT * FROM jaytec_operations WHERE operation_id=%s FOR UPDATE",
                    (operation_id,),
                )
                operation = cur.fetchone()
                if operation is None:
                    raise JobRuntimeError(f"operation_not_found:{operation_id}")
                if operation["job_id"] != token.job_id:
                    raise StaleFenceError(token.job_id)
                if stale_fence(
                    expected_epoch=int(operation["ownership_epoch"]),
                    expected_fence=int(operation["fence_token"]),
                    actual_epoch=token.ownership_epoch,
                    actual_fence=token.fence_token,
                ):
                    raise StaleFenceError(token.job_id)
                current = operation["status"]
                if current == "UNCERTAIN_PARTIAL" and target_status == "IN_FLIGHT":
                    raise UnsafeRetryError(operation_id)
                if not operation_transition_allowed(current, target_status):
                    raise JobRuntimeError(f"invalid_operation_transition:{current}->{target_status}")
                cur.execute(
                    """
                    UPDATE jaytec_operations
                    SET status=%s,
                        evidence=COALESCE(%s::jsonb, evidence),
                        retry_decision=COALESCE(%s, retry_decision),
                        started_at=CASE WHEN %s='IN_FLIGHT' THEN COALESCE(started_at,now()) ELSE started_at END,
                        verified_at=CASE WHEN %s IN ('VERIFIED_COMPLETE','VERIFIED_NOT_DONE','FAILED_SAFE') THEN now() ELSE verified_at END,
                        updated_at=now()
                    WHERE operation_id=%s
                    RETURNING *
                    """,
                    (
                        target_status,
                        _json(dict(evidence)) if evidence is not None else None,
                        retry_decision,
                        target_status,
                        target_status,
                        operation_id,
                    ),
                )
                row = cur.fetchone()
        return _row(row) or {}

    def append_event(
        self,
        *,
        job_id: Optional[str],
        event_type: str,
        source: str,
        source_version: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    RETURNING *
                    """,
                    (job_id, event_type, source, source_version, _json(dict(payload or {}))),
                )
                row = cur.fetchone()
        return _row(row) or {}

    def complete_job(
        self,
        token: LeaseToken,
        *,
        status: str = "SUCCEEDED",
        result: Optional[Mapping[str, Any]] = None,
        health: Optional[str] = None,
    ) -> Dict[str, Any]:
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError("terminal status required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status=%s,
                        latest_verified_result=COALESCE(%s::jsonb, latest_verified_result),
                        health=COALESCE(%s, health),
                        lease_owner=NULL, lease_expires_at=NULL,
                        version=version+1, updated_at=now()
                    WHERE job_id=%s AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND lease_expires_at > now()
                    RETURNING *
                    """,
                    (
                        status,
                        _json(dict(result)) if result is not None else None,
                        health,
                        token.job_id, token.owner, token.ownership_epoch, token.fence_token,
                    ),
                )
                row = cur.fetchone()
        if row is None:
            raise StaleFenceError(token.job_id)
        return _row(row) or {}

    def guardian_audit(self, *, stale_after_minutes: int = 30) -> list[Dict[str, Any]]:
        findings: list[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id, lease_owner, lease_expires_at FROM jaytec_jobs
                    WHERE status='RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at < now()
                    """
                )
                for row in cur.fetchall():
                    findings.append({
                        "severity": "ERROR",
                        "finding_type": "EXPIRED_JOB_LEASE",
                        "job_id": row["job_id"],
                        "evidence": _row(row),
                    })
                cur.execute(
                    """
                    SELECT job_id, updated_at FROM jaytec_jobs
                    WHERE status='RUNNING'
                      AND updated_at < now() - (%s * interval '1 minute')
                    """,
                    (stale_after_minutes,),
                )
                for row in cur.fetchall():
                    findings.append({
                        "severity": "WARNING",
                        "finding_type": "STUCK_RUNNING_JOB",
                        "job_id": row["job_id"],
                        "evidence": _row(row),
                    })
                cur.execute(
                    "SELECT operation_id, job_id, updated_at FROM jaytec_operations WHERE status='UNCERTAIN_PARTIAL'"
                )
                for row in cur.fetchall():
                    findings.append({
                        "severity": "ERROR",
                        "finding_type": "UNCERTAIN_PARTIAL_OPERATION",
                        "job_id": row["job_id"],
                        "evidence": _row(row),
                    })
                cur.execute(
                    """
                    SELECT event_id, job_id, event_type, payload, created_at
                    FROM jaytec_job_events
                    WHERE event_type IN ('SPECIALIST_ROUTE_FAILED','BRIDGE_FAILED','CHECKPOINT_FAILED')
                      AND created_at > now() - interval '24 hours'
                    ORDER BY event_id DESC LIMIT 100
                    """
                )
                for row in cur.fetchall():
                    findings.append({
                        "severity": "WARNING",
                        "finding_type": row["event_type"],
                        "job_id": row["job_id"],
                        "evidence": _row(row),
                    })
        return findings

    @staticmethod
    def _assert_fence(cur, token: LeaseToken) -> None:
        cur.execute(
            """
            SELECT ownership_epoch, fence_token, lease_owner, lease_expires_at
            FROM jaytec_jobs WHERE job_id=%s FOR UPDATE
            """,
            (token.job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise JobNotFoundError(token.job_id)
        if (
            row["lease_owner"] != token.owner
            or int(row["ownership_epoch"]) != token.ownership_epoch
            or int(row["fence_token"]) != token.fence_token
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= datetime.now(timezone.utc)
        ):
            raise StaleFenceError(token.job_id)
