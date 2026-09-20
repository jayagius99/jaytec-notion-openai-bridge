"""JAYTEC WATCH + AUTORECOVERY supervisor.

The assignment is durable; workers are disposable.

This module intentionally separates:
- canonical assignment/checkpoint state;
- stop-reason classification;
- liveness/progress observation;
- exclusive recovery ownership via leases + fencing tokens;
- worker invocation.

Normal ChatGPT UI conversations are never "poked" or assumed callable. They
receive a manual continuation packet. Automatic recovery is available only to
explicitly registered callable JAYTEC worker endpoints.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping, Optional, Protocol

import psycopg2
import psycopg2.extras


class AutoRecoveryError(RuntimeError):
    pass


class StopReason(StrEnum):
    RUNNING = "RUNNING"

    STALLED_RECOVERABLE = "STALLED_RECOVERABLE"
    WORKER_LOST = "WORKER_LOST"
    TIMEOUT = "TIMEOUT"
    TRANSIENT_PROVIDER_FAILURE = "TRANSIENT_PROVIDER_FAILURE"

    PAUSED_BY_OWNER = "PAUSED_BY_OWNER"
    PAUSED_BY_OPERATOR = "PAUSED_BY_OPERATOR"
    WAITING_FOR_AUTHORITY = "WAITING_FOR_AUTHORITY"
    WAITING_FOR_REQUIRED_INPUT = "WAITING_FOR_REQUIRED_INPUT"
    WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"

    COMPLETED = "COMPLETED"
    FAILED_FATAL = "FAILED_FATAL"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"


RECOVERABLE_REASONS = frozenset(
    {
        StopReason.STALLED_RECOVERABLE,
        StopReason.WORKER_LOST,
        StopReason.TIMEOUT,
        StopReason.TRANSIENT_PROVIDER_FAILURE,
    }
)

HUMAN_OR_AUTHORITY_HOLDS = frozenset(
    {
        StopReason.PAUSED_BY_OWNER,
        StopReason.PAUSED_BY_OPERATOR,
        StopReason.WAITING_FOR_AUTHORITY,
        StopReason.WAITING_FOR_REQUIRED_INPUT,
        StopReason.WAITING_FOR_RESOURCE,
        StopReason.WAITING_FOR_DEPENDENCY,
        StopReason.RECOVERY_EXHAUSTED,
    }
)


class WorkerKind(StrEnum):
    CHATGPT_UI = "CHATGPT_UI"
    JAYTEC_CALLABLE = "JAYTEC_CALLABLE"


class RecoveryRoute(StrEnum):
    SAME_WORKER_PROVIDER = "SAME_WORKER_PROVIDER"
    FRESH_WORKER_SAME_CHECKPOINT = "FRESH_WORKER_SAME_CHECKPOINT"
    ALTERNATE_APPROVED_ROUTE = "ALTERNATE_APPROVED_ROUTE"


class SupervisorAction(StrEnum):
    NOOP_HEALTHY = "NOOP_HEALTHY"
    STOP_WATCH = "STOP_WATCH"
    HOLD = "HOLD"
    NOTIFY_JAY = "NOTIFY_JAY"
    MANUAL_RESUME_PACKET = "MANUAL_RESUME_PACKET"
    RECOVER = "RECOVER"
    LEASE_ALREADY_HELD = "LEASE_ALREADY_HELD"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    RECOVERY_FAILED = "RECOVERY_FAILED"


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        raise AutoRecoveryError("NAIVE_DATETIME_FORBIDDEN")
    return value.astimezone(timezone.utc)


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _lock_id(task_id: str) -> int:
    return int(zlib.crc32(("jaytec-recovery:" + task_id).encode("utf-8")) & 0x7FFFFFFF)


@dataclass(frozen=True)
class AssignmentCheckpoint:
    task_id: str
    objective: str
    current_phase: str
    completed_work: tuple[str, ...]
    remaining_work: tuple[str, ...]
    last_safe_checkpoint: str
    repo: str
    branch: str
    commit_head: str
    open_pr: Optional[int]
    current_files_state: Mapping[str, Any]
    tests_completed: tuple[str, ...]
    known_failures: tuple[str, ...]
    active_constraints: tuple[str, ...]
    authority_envelope: Mapping[str, Any]
    cost_envelope: Mapping[str, Any]
    dependencies: tuple[str, ...]
    next_intended_action: str
    worker_specialist_preference: tuple[str, ...]
    checkpoint_number: int = 1

    def validate(self) -> "AssignmentCheckpoint":
        if not TASK_ID_RE.fullmatch(self.task_id):
            raise AutoRecoveryError("CHECKPOINT_TASK_ID_INVALID")
        for name, value in (
            ("objective", self.objective),
            ("current_phase", self.current_phase),
            ("last_safe_checkpoint", self.last_safe_checkpoint),
            ("repo", self.repo),
            ("branch", self.branch),
            ("commit_head", self.commit_head),
            ("next_intended_action", self.next_intended_action),
        ):
            if not isinstance(value, str) or not value.strip():
                raise AutoRecoveryError("CHECKPOINT_FIELD_INVALID:" + name)
        if not SHA_RE.fullmatch(self.commit_head):
            raise AutoRecoveryError("CHECKPOINT_HEAD_INVALID")
        if self.open_pr is not None and (
            not isinstance(self.open_pr, int)
            or isinstance(self.open_pr, bool)
            or self.open_pr < 1
        ):
            raise AutoRecoveryError("CHECKPOINT_PR_INVALID")
        if (
            not isinstance(self.checkpoint_number, int)
            or isinstance(self.checkpoint_number, bool)
            or self.checkpoint_number < 1
        ):
            raise AutoRecoveryError("CHECKPOINT_NUMBER_INVALID")
        for name, values in (
            ("completed_work", self.completed_work),
            ("remaining_work", self.remaining_work),
            ("tests_completed", self.tests_completed),
            ("known_failures", self.known_failures),
            ("active_constraints", self.active_constraints),
            ("dependencies", self.dependencies),
            ("worker_specialist_preference", self.worker_specialist_preference),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) for item in values
            ):
                raise AutoRecoveryError("CHECKPOINT_LIST_INVALID:" + name)
        if not isinstance(self.current_files_state, Mapping):
            raise AutoRecoveryError("CHECKPOINT_FILES_STATE_INVALID")
        if not isinstance(self.authority_envelope, Mapping):
            raise AutoRecoveryError("CHECKPOINT_AUTHORITY_ENVELOPE_INVALID")
        if not isinstance(self.cost_envelope, Mapping):
            raise AutoRecoveryError("CHECKPOINT_COST_ENVELOPE_INVALID")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        data = asdict(self)
        for key in (
            "completed_work",
            "remaining_work",
            "tests_completed",
            "known_failures",
            "active_constraints",
            "dependencies",
            "worker_specialist_preference",
        ):
            data[key] = list(data[key])
        return _json_clone(data)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AssignmentCheckpoint":
        def tup(name: str) -> tuple[str, ...]:
            raw = value.get(name, [])
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise AutoRecoveryError("CHECKPOINT_LIST_INVALID:" + name)
            return tuple(raw)

        checkpoint = cls(
            task_id=str(value.get("task_id") or ""),
            objective=str(value.get("objective") or ""),
            current_phase=str(value.get("current_phase") or ""),
            completed_work=tup("completed_work"),
            remaining_work=tup("remaining_work"),
            last_safe_checkpoint=str(value.get("last_safe_checkpoint") or ""),
            repo=str(value.get("repo") or ""),
            branch=str(value.get("branch") or ""),
            commit_head=str(value.get("commit_head") or ""),
            open_pr=value.get("open_pr"),
            current_files_state=dict(value.get("current_files_state") or {}),
            tests_completed=tup("tests_completed"),
            known_failures=tup("known_failures"),
            active_constraints=tup("active_constraints"),
            authority_envelope=dict(value.get("authority_envelope") or {}),
            cost_envelope=dict(value.get("cost_envelope") or {}),
            dependencies=tup("dependencies"),
            next_intended_action=str(value.get("next_intended_action") or ""),
            worker_specialist_preference=tup("worker_specialist_preference"),
            checkpoint_number=int(value.get("checkpoint_number") or 0),
        )
        return checkpoint.validate()


@dataclass(frozen=True)
class AssignmentState:
    task_id: str
    checkpoint: AssignmentCheckpoint
    stop_reason: StopReason
    worker_kind: WorkerKind
    worker_id: Optional[str]
    worker_route: Optional[str]
    last_heartbeat_at: Optional[datetime]
    last_progress_at: Optional[datetime]
    progress_marker: Optional[str]
    recovery_attempts: int
    fencing_token: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[datetime]
    completed: bool
    last_error: Optional[str]
    updated_at: datetime


@dataclass(frozen=True)
class RecoveryLease:
    task_id: str
    lease_owner: str
    fencing_token: int
    attempt_number: int
    expires_at: datetime


@dataclass(frozen=True)
class RecoveryDecision:
    action: SupervisorAction
    effective_stop_reason: StopReason
    reason: str
    recovery_route: Optional[RecoveryRoute] = None
    continuation_packet: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class WorkerInvocation:
    accepted: bool
    worker_id: Optional[str]
    route: RecoveryRoute
    detail: str = ""


@dataclass(frozen=True)
class WorkerHealth:
    healthy: bool
    worker_id: Optional[str]
    heartbeat_at: Optional[datetime]
    progress_marker: Optional[str] = None
    detail: str = ""


class CheckpointVerifier(Protocol):
    def verify(self, checkpoint: AssignmentCheckpoint) -> tuple[bool, str]:
        ...


class WorkerInvoker(Protocol):
    def invoke(
        self,
        *,
        checkpoint: AssignmentCheckpoint,
        continuation_packet: Mapping[str, Any],
        route: RecoveryRoute,
        fencing_token: int,
    ) -> WorkerInvocation:
        ...


class HealthProbe(Protocol):
    def wait_for_healthy(
        self,
        *,
        task_id: str,
        fencing_token: int,
        worker_id: Optional[str],
        timeout_seconds: int,
    ) -> WorkerHealth:
        ...


class RecoveryNotifier(Protocol):
    def notify(self, event: Mapping[str, Any]) -> None:
        ...


def route_for_attempt(attempt_number: int) -> RecoveryRoute:
    if attempt_number == 1:
        return RecoveryRoute.SAME_WORKER_PROVIDER
    if attempt_number == 2:
        return RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT
    if attempt_number == 3:
        return RecoveryRoute.ALTERNATE_APPROVED_ROUTE
    raise AutoRecoveryError("RECOVERY_ATTEMPT_OUT_OF_RANGE")


def build_continuation_packet(
    checkpoint: AssignmentCheckpoint,
    *,
    fencing_token: Optional[int] = None,
    recovery_route: Optional[RecoveryRoute] = None,
) -> dict[str, Any]:
    checkpoint.validate()
    packet: dict[str, Any] = {
        "schema_version": "JAYTEC_CONTINUATION_PACKET_V1",
        "task_id": checkpoint.task_id,
        "checkpoint_number": checkpoint.checkpoint_number,
        "instruction": "Resume — do not recreate completed work",
        "objective": checkpoint.objective,
        "current_phase": checkpoint.current_phase,
        "completed_work": list(checkpoint.completed_work),
        "remaining_work": list(checkpoint.remaining_work),
        "last_safe_checkpoint": checkpoint.last_safe_checkpoint,
        "repo": checkpoint.repo,
        "branch": checkpoint.branch,
        "verified_head": checkpoint.commit_head,
        "open_pr": checkpoint.open_pr,
        "current_files_state": _json_clone(dict(checkpoint.current_files_state)),
        "tests_already_completed": list(checkpoint.tests_completed),
        "known_failures": list(checkpoint.known_failures),
        "active_constraints": list(checkpoint.active_constraints),
        "authority_envelope": _json_clone(dict(checkpoint.authority_envelope)),
        "cost_envelope": _json_clone(dict(checkpoint.cost_envelope)),
        "dependencies": list(checkpoint.dependencies),
        "next_intended_action": checkpoint.next_intended_action,
        "worker_specialist_preference": list(
            checkpoint.worker_specialist_preference
        ),
        "anti_duplication": (
            "Steps already listed as completed MUST NOT be recreated, repeated, "
            "or replaced unless checkpoint verification proves they are invalid."
        ),
    }
    if fencing_token is not None:
        packet["fencing_token"] = fencing_token
    if recovery_route is not None:
        packet["recovery_route"] = recovery_route.value
    return packet


def classify_assignment(
    state: AssignmentState,
    *,
    now: Optional[datetime] = None,
    heartbeat_timeout_seconds: int = 600,
    max_recovery_attempts: int = 3,
) -> RecoveryDecision:
    current = _aware(now or utcnow())
    assert current is not None

    if state.completed or state.stop_reason is StopReason.COMPLETED:
        return RecoveryDecision(
            SupervisorAction.STOP_WATCH,
            StopReason.COMPLETED,
            "ASSIGNMENT_COMPLETE",
        )

    if state.stop_reason is StopReason.FAILED_FATAL:
        return RecoveryDecision(
            SupervisorAction.HOLD,
            StopReason.FAILED_FATAL,
            "FATAL_FAILURE_REQUIRES_HUMAN_DECISION",
        )

    if (
        state.stop_reason is StopReason.WAITING_FOR_DEPENDENCY
        and str(state.last_error or "").startswith("MANUS_TERMINAL:NEEDS_JAYTEC:")
    ):
        return RecoveryDecision(
            SupervisorAction.HOLD,
            StopReason.WAITING_FOR_DEPENDENCY,
            "JAYTEC_INTERNAL_HANDOFF_REQUIRED",
        )

    if state.stop_reason in HUMAN_OR_AUTHORITY_HOLDS:
        action = (
            SupervisorAction.NOTIFY_JAY
            if state.stop_reason
            in {
                StopReason.WAITING_FOR_REQUIRED_INPUT,
                StopReason.RECOVERY_EXHAUSTED,
            }
            else SupervisorAction.HOLD
        )
        return RecoveryDecision(
            action,
            state.stop_reason,
            "INTENTIONAL_OR_EXTERNAL_BLOCKER",
        )

    effective = state.stop_reason
    if state.stop_reason is StopReason.RUNNING:
        heartbeat = _aware(state.last_heartbeat_at)
        if heartbeat is not None:
            age = (current - heartbeat).total_seconds()
            if age <= heartbeat_timeout_seconds:
                return RecoveryDecision(
                    SupervisorAction.NOOP_HEALTHY,
                    StopReason.RUNNING,
                    "HEALTHY_WORKER_HEARTBEAT",
                )
        effective = StopReason.WORKER_LOST

    if effective not in RECOVERABLE_REASONS:
        return RecoveryDecision(
            SupervisorAction.HOLD,
            effective,
            "UNCLASSIFIED_STOP_FAILS_CLOSED",
        )

    if state.recovery_attempts >= max_recovery_attempts:
        return RecoveryDecision(
            SupervisorAction.NOTIFY_JAY,
            StopReason.RECOVERY_EXHAUSTED,
            "RECOVERY_ATTEMPTS_EXHAUSTED",
        )

    if state.worker_kind is WorkerKind.CHATGPT_UI:
        return RecoveryDecision(
            SupervisorAction.MANUAL_RESUME_PACKET,
            effective,
            "UI_CHAT_IS_NOT_A_CALLABLE_WORKER",
            continuation_packet=build_continuation_packet(state.checkpoint),
        )

    attempt = state.recovery_attempts + 1
    return RecoveryDecision(
        SupervisorAction.RECOVER,
        effective,
        "RECOVERABLE_STOP_WITH_CALLABLE_WORKER",
        recovery_route=route_for_attempt(attempt),
    )


class PostgresAssignmentStore:
    """Durable canonical assignment state + atomic recovery leases.

    Fencing rule:
    every callable worker mutation/heartbeat must present the current token.
    Once recovery increments the token, an older worker is rejected even if it
    wakes up later.
    """

    def __init__(self, database_url: str):
        if not database_url or not str(database_url).strip():
            raise AutoRecoveryError("DATABASE_URL_REQUIRED")
        self.database_url = str(database_url)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jaytec_assignment_state (
                        task_id TEXT PRIMARY KEY,
                        checkpoint_json JSONB NOT NULL,
                        stop_reason TEXT NOT NULL,
                        worker_kind TEXT NOT NULL,
                        worker_id TEXT,
                        worker_route TEXT,
                        last_heartbeat_at TIMESTAMPTZ,
                        last_progress_at TIMESTAMPTZ,
                        progress_marker TEXT,
                        recovery_attempts INTEGER NOT NULL DEFAULT 0,
                        fencing_token BIGINT NOT NULL DEFAULT 0,
                        lease_owner TEXT,
                        lease_expires_at TIMESTAMPTZ,
                        completed BOOLEAN NOT NULL DEFAULT FALSE,
                        last_error TEXT,
                        updated_at TIMESTAMPTZ NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    jaytec_assignment_state_updated_idx
                    ON jaytec_assignment_state(updated_at);
                    """
                )

    def upsert_checkpoint(
        self,
        checkpoint: AssignmentCheckpoint,
        *,
        stop_reason: StopReason,
        worker_kind: WorkerKind,
        worker_id: Optional[str] = None,
        worker_route: Optional[str] = None,
        heartbeat_at: Optional[datetime] = None,
        progress_marker: Optional[str] = None,
        completed: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        checkpoint.validate()
        current = _aware(now or utcnow())
        heartbeat = _aware(heartbeat_at)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(checkpoint.task_id),))
                cur.execute(
                    """
                    INSERT INTO jaytec_assignment_state (
                        task_id, checkpoint_json, stop_reason, worker_kind,
                        worker_id, worker_route, last_heartbeat_at,
                        last_progress_at, progress_marker, recovery_attempts,
                        fencing_token, lease_owner, lease_expires_at, completed,
                        last_error, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, 0, 0, NULL, NULL, %s, NULL, %s
                    )
                    ON CONFLICT (task_id) DO UPDATE SET
                        checkpoint_json = EXCLUDED.checkpoint_json,
                        stop_reason = EXCLUDED.stop_reason,
                        worker_kind = EXCLUDED.worker_kind,
                        worker_id = EXCLUDED.worker_id,
                        worker_route = EXCLUDED.worker_route,
                        last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                        last_progress_at = EXCLUDED.last_progress_at,
                        progress_marker = EXCLUDED.progress_marker,
                        completed = EXCLUDED.completed,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        checkpoint.task_id,
                        psycopg2.extras.Json(checkpoint.to_dict()),
                        stop_reason.value,
                        worker_kind.value,
                        worker_id,
                        worker_route,
                        heartbeat,
                        current if progress_marker else None,
                        progress_marker,
                        bool(completed),
                        current,
                    ),
                )

    @staticmethod
    def _state_from_row(row: Mapping[str, Any]) -> AssignmentState:
        checkpoint_raw = row.get("checkpoint_json")
        if isinstance(checkpoint_raw, str):
            checkpoint_raw = json.loads(checkpoint_raw)
        if not isinstance(checkpoint_raw, Mapping):
            raise AutoRecoveryError("CHECKPOINT_JSON_INVALID")
        return AssignmentState(
            task_id=str(row["task_id"]),
            checkpoint=AssignmentCheckpoint.from_mapping(checkpoint_raw),
            stop_reason=StopReason(str(row["stop_reason"])),
            worker_kind=WorkerKind(str(row["worker_kind"])),
            worker_id=row.get("worker_id"),
            worker_route=row.get("worker_route"),
            last_heartbeat_at=_aware(row.get("last_heartbeat_at")),
            last_progress_at=_aware(row.get("last_progress_at")),
            progress_marker=row.get("progress_marker"),
            recovery_attempts=int(row.get("recovery_attempts") or 0),
            fencing_token=int(row.get("fencing_token") or 0),
            lease_owner=row.get("lease_owner"),
            lease_expires_at=_aware(row.get("lease_expires_at")),
            completed=bool(row.get("completed")),
            last_error=row.get("last_error"),
            updated_at=_aware(row.get("updated_at")) or utcnow(),
        )

    def get(self, task_id: str) -> Optional[AssignmentState]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM jaytec_assignment_state WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                return self._state_from_row(row) if row else None

    def acquire_recovery_lease(
        self,
        task_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = 180,
        max_recovery_attempts: int = 3,
        heartbeat_timeout_seconds: int = 600,
        now: Optional[datetime] = None,
    ) -> Optional[RecoveryLease]:
        if not lease_owner.strip():
            raise AutoRecoveryError("LEASE_OWNER_REQUIRED")
        if lease_seconds < 30 or lease_seconds > 900:
            raise AutoRecoveryError("LEASE_DURATION_INVALID")
        current = _aware(now or utcnow())
        assert current is not None

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(task_id),))
                cur.execute(
                    "SELECT * FROM jaytec_assignment_state WHERE task_id = %s FOR UPDATE",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise AutoRecoveryError("ASSIGNMENT_NOT_FOUND")
                state = self._state_from_row(row)
                if state.stop_reason not in RECOVERABLE_REASONS and not (
                    state.stop_reason is StopReason.RUNNING
                    and (
                        state.last_heartbeat_at is None
                        or (current - state.last_heartbeat_at).total_seconds()
                        > heartbeat_timeout_seconds
                    )
                ):
                    raise AutoRecoveryError("ASSIGNMENT_NOT_RECOVERABLE")
                if state.recovery_attempts >= max_recovery_attempts:
                    return None
                if (
                    state.lease_owner
                    and state.lease_expires_at is not None
                    and state.lease_expires_at > current
                ):
                    return None

                token = state.fencing_token + 1
                attempt = state.recovery_attempts + 1
                expires = current + timedelta(seconds=lease_seconds)
                cur.execute(
                    """
                    UPDATE jaytec_assignment_state
                    SET fencing_token = %s,
                        recovery_attempts = %s,
                        lease_owner = %s,
                        lease_expires_at = %s,
                        updated_at = %s
                    WHERE task_id = %s
                    """,
                    (token, attempt, lease_owner, expires, current, task_id),
                )
                return RecoveryLease(
                    task_id=task_id,
                    lease_owner=lease_owner,
                    fencing_token=token,
                    attempt_number=attempt,
                    expires_at=expires,
                )

    def _require_token(
        self,
        cur,
        task_id: str,
        fencing_token: int,
    ) -> Mapping[str, Any]:
        cur.execute(
            "SELECT * FROM jaytec_assignment_state WHERE task_id = %s FOR UPDATE",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            raise AutoRecoveryError("ASSIGNMENT_NOT_FOUND")
        current_token = int(row.get("fencing_token") or 0)
        if int(fencing_token) != current_token:
            raise AutoRecoveryError("STALE_FENCING_TOKEN")
        return row

    def heartbeat(
        self,
        task_id: str,
        *,
        fencing_token: int,
        worker_id: str,
        progress_marker: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        current = _aware(now or utcnow())
        assert current is not None
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(task_id),))
                self._require_token(cur, task_id, fencing_token)
                cur.execute(
                    """
                    UPDATE jaytec_assignment_state
                    SET stop_reason = %s,
                        worker_id = %s,
                        last_heartbeat_at = %s,
                        last_progress_at = CASE WHEN %s IS NULL
                            THEN last_progress_at ELSE %s END,
                        progress_marker = COALESCE(%s, progress_marker),
                        recovery_attempts = CASE WHEN %s IS NULL
                            THEN recovery_attempts ELSE 0 END,
                        last_error = NULL,
                        updated_at = %s
                    WHERE task_id = %s
                    """,
                    (
                        StopReason.RUNNING.value,
                        worker_id,
                        current,
                        progress_marker,
                        current,
                        progress_marker,
                        progress_marker,
                        current,
                        task_id,
                    ),
                )

    def mark_stop(
        self,
        task_id: str,
        *,
        fencing_token: int,
        stop_reason: StopReason,
        error: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        current = _aware(now or utcnow())
        assert current is not None
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(task_id),))
                self._require_token(cur, task_id, fencing_token)
                cur.execute(
                    """
                    UPDATE jaytec_assignment_state
                    SET stop_reason = %s,
                        completed = %s,
                        last_error = %s,
                        updated_at = %s
                    WHERE task_id = %s
                    """,
                    (
                        stop_reason.value,
                        stop_reason is StopReason.COMPLETED,
                        (error or "")[:2000] or None,
                        current,
                        task_id,
                    ),
                )

    def release_lease(
        self,
        lease: RecoveryLease,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        current = _aware(now or utcnow())
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(lease.task_id),))
                row = self._require_token(cur, lease.task_id, lease.fencing_token)
                if row.get("lease_owner") != lease.lease_owner:
                    raise AutoRecoveryError("LEASE_OWNER_MISMATCH")
                cur.execute(
                    """
                    UPDATE jaytec_assignment_state
                    SET lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = %s
                    WHERE task_id = %s
                    """,
                    (current, lease.task_id),
                )


class MemoryAssignmentStore:
    """Deterministic in-memory implementation used by unit tests."""

    def __init__(self, state: AssignmentState):
        self.state = state

    def get(self, task_id: str) -> Optional[AssignmentState]:
        return copy.deepcopy(self.state) if self.state.task_id == task_id else None

    def acquire_recovery_lease(
        self,
        task_id: str,
        *,
        lease_owner: str,
        lease_seconds: int = 180,
        max_recovery_attempts: int = 3,
        heartbeat_timeout_seconds: int = 600,
        now: Optional[datetime] = None,
    ) -> Optional[RecoveryLease]:
        current = _aware(now or utcnow())
        assert current is not None
        state = self.state
        if state.task_id != task_id:
            raise AutoRecoveryError("ASSIGNMENT_NOT_FOUND")
        if state.recovery_attempts >= max_recovery_attempts:
            return None
        if state.lease_owner and state.lease_expires_at and state.lease_expires_at > current:
            return None
        token = state.fencing_token + 1
        attempt = state.recovery_attempts + 1
        expires = current + timedelta(seconds=lease_seconds)
        self.state = AssignmentState(
            **{
                **asdict(state),
                "checkpoint": state.checkpoint,
                "stop_reason": state.stop_reason,
                "worker_kind": state.worker_kind,
                "recovery_attempts": attempt,
                "fencing_token": token,
                "lease_owner": lease_owner,
                "lease_expires_at": expires,
                "updated_at": current,
            }
        )
        return RecoveryLease(task_id, lease_owner, token, attempt, expires)

    def heartbeat(
        self,
        task_id: str,
        *,
        fencing_token: int,
        worker_id: str,
        progress_marker: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        current = _aware(now or utcnow())
        assert current is not None
        state = self.state
        if state.task_id != task_id:
            raise AutoRecoveryError("ASSIGNMENT_NOT_FOUND")
        if fencing_token != state.fencing_token:
            raise AutoRecoveryError("STALE_FENCING_TOKEN")
        self.state = AssignmentState(
            **{
                **asdict(state),
                "checkpoint": state.checkpoint,
                "stop_reason": StopReason.RUNNING,
                "worker_kind": state.worker_kind,
                "worker_id": worker_id,
                "last_heartbeat_at": current,
                "last_progress_at": current if progress_marker else state.last_progress_at,
                "progress_marker": progress_marker or state.progress_marker,
                "recovery_attempts": 0 if progress_marker else state.recovery_attempts,
                "last_error": None,
                "updated_at": current,
            }
        )

    def mark_stop(
        self,
        task_id: str,
        *,
        fencing_token: int,
        stop_reason: StopReason,
        error: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        current = _aware(now or utcnow())
        assert current is not None
        state = self.state
        if state.task_id != task_id:
            raise AutoRecoveryError("ASSIGNMENT_NOT_FOUND")
        if fencing_token != state.fencing_token:
            raise AutoRecoveryError("STALE_FENCING_TOKEN")
        self.state = AssignmentState(
            **{
                **asdict(state),
                "checkpoint": state.checkpoint,
                "stop_reason": stop_reason,
                "worker_kind": state.worker_kind,
                "completed": stop_reason is StopReason.COMPLETED,
                "last_error": error,
                "updated_at": current,
            }
        )

    def release_lease(self, lease: RecoveryLease, *, now: Optional[datetime] = None) -> None:
        current = _aware(now or utcnow())
        assert current is not None
        state = self.state
        if lease.fencing_token != state.fencing_token:
            raise AutoRecoveryError("STALE_FENCING_TOKEN")
        if lease.lease_owner != state.lease_owner:
            raise AutoRecoveryError("LEASE_OWNER_MISMATCH")
        self.state = AssignmentState(
            **{
                **asdict(state),
                "checkpoint": state.checkpoint,
                "stop_reason": state.stop_reason,
                "worker_kind": state.worker_kind,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": current,
            }
        )


class AutoRecoverySupervisor:
    def __init__(
        self,
        *,
        store: Any,
        verifier: CheckpointVerifier,
        invoker: WorkerInvoker,
        health_probe: HealthProbe,
        notifier: Optional[RecoveryNotifier] = None,
        instance_id: Optional[str] = None,
        heartbeat_timeout_seconds: int = 600,
        health_verify_timeout_seconds: int = 120,
        health_refresh_timeout_seconds: int = 15,
        max_recovery_attempts: int = 3,
    ):
        self.store = store
        self.verifier = verifier
        self.invoker = invoker
        self.health_probe = health_probe
        self.notifier = notifier
        self.instance_id = instance_id or ("watch-" + uuid.uuid4().hex[:16])
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.health_verify_timeout_seconds = health_verify_timeout_seconds
        self.health_refresh_timeout_seconds = health_refresh_timeout_seconds
        self.max_recovery_attempts = max_recovery_attempts

    def _notify(self, payload: Mapping[str, Any]) -> None:
        if self.notifier is not None:
            self.notifier.notify(_json_clone(dict(payload)))

    def _terminal_worker_result(
        self,
        task_id: str,
        *,
        fencing_token: int,
        health: WorkerHealth,
    ) -> RecoveryDecision | None:
        marker = str(health.progress_marker or "")
        if not marker.startswith("MANUS_TERMINAL:"):
            return None

        parts = marker.split(":", 3)
        result_state = parts[1] if len(parts) > 1 else "UNKNOWN"

        if result_state == "NEEDS_JAYTEC":
            stop_reason = StopReason.WAITING_FOR_DEPENDENCY
            action = SupervisorAction.HOLD
            reason = "CALLABLE_WORKER_NEEDS_JAYTEC"
        else:
            # Until a terminal status has a dedicated convergence rule, fail
            # closed exactly as before. Only NEEDS_JAYTEC is now known to be an
            # internal orchestration handoff rather than a Jay/owner boundary.
            stop_reason = StopReason.WAITING_FOR_REQUIRED_INPUT
            action = SupervisorAction.NOTIFY_JAY
            reason = "CALLABLE_WORKER_FINISHED_REVIEW_REQUIRED"

        self.store.mark_stop(
            task_id,
            fencing_token=fencing_token,
            stop_reason=stop_reason,
            error=marker[:2000],
            now=health.heartbeat_at or utcnow(),
        )
        self._notify(
            {
                "event": "JAYTEC_AUTORECOVERY_WORKER_TERMINAL",
                "task_id": task_id,
                "worker_id": health.worker_id,
                "progress_marker": marker,
                "result_state": result_state,
                "next_state": stop_reason.value,
                "owner_notification_required": action is SupervisorAction.NOTIFY_JAY,
            }
        )
        return RecoveryDecision(
            action,
            stop_reason,
            reason,
        )

    def refresh_worker_health(
        self,
        task_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """Refresh a RUNNING callable worker before stale-heartbeat classification.

        A callable worker need not write directly to JAYTEC Postgres. The
        supervisor may poll its registered health adapter once per canonical
        cycle and translate that proof into a fenced heartbeat. A stale fencing
        token or any probe failure fails closed and never revives an old worker.
        """

        state = self.store.get(task_id)
        if state is None:
            return False
        if (
            state.completed
            or state.stop_reason is not StopReason.RUNNING
            or state.worker_kind is not WorkerKind.JAYTEC_CALLABLE
            or not state.worker_id
        ):
            return False

        try:
            health = self.health_probe.wait_for_healthy(
                task_id=task_id,
                fencing_token=state.fencing_token,
                worker_id=state.worker_id,
                timeout_seconds=self.health_refresh_timeout_seconds,
            )
        except Exception:
            return False

        if not health.healthy or health.heartbeat_at is None:
            return False

        try:
            terminal = self._terminal_worker_result(
                task_id,
                fencing_token=state.fencing_token,
                health=health,
            )
            if terminal is not None:
                return True
            self.store.heartbeat(
                task_id,
                fencing_token=state.fencing_token,
                worker_id=health.worker_id or state.worker_id,
                progress_marker=health.progress_marker,
                now=health.heartbeat_at,
            )
        except Exception:
            return False
        return True

    def tick(
        self,
        task_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> RecoveryDecision:
        current = _aware(now or utcnow())
        assert current is not None
        state = self.store.get(task_id)
        if state is None:
            return RecoveryDecision(
                SupervisorAction.NOTIFY_JAY,
                StopReason.WAITING_FOR_REQUIRED_INPUT,
                "CANONICAL_ASSIGNMENT_STATE_NOT_FOUND",
            )

        decision = classify_assignment(
            state,
            now=current,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            max_recovery_attempts=self.max_recovery_attempts,
        )

        if decision.action is SupervisorAction.MANUAL_RESUME_PACKET:
            self._notify(
                {
                    "event": "JAYTEC_AUTORECOVERY_MANUAL_RESUME_REQUIRED",
                    "task_id": task_id,
                    "reason": decision.reason,
                    "continuation_packet": decision.continuation_packet,
                }
            )
            return decision

        if decision.action is not SupervisorAction.RECOVER:
            if decision.action in {
                SupervisorAction.NOTIFY_JAY,
                SupervisorAction.STOP_WATCH,
            }:
                self._notify(
                    {
                        "event": "JAYTEC_AUTORECOVERY_STATE",
                        "task_id": task_id,
                        "action": decision.action.value,
                        "stop_reason": decision.effective_stop_reason.value,
                        "reason": decision.reason,
                    }
                )
            return decision

        lease = self.store.acquire_recovery_lease(
            task_id,
            lease_owner=self.instance_id,
            max_recovery_attempts=self.max_recovery_attempts,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            now=current,
        )
        if lease is None:
            refreshed = self.store.get(task_id)
            if refreshed and refreshed.recovery_attempts >= self.max_recovery_attempts:
                self._notify(
                    {
                        "event": "JAYTEC_AUTORECOVERY_EXHAUSTED",
                        "task_id": task_id,
                        "recovery_attempts": refreshed.recovery_attempts,
                    }
                )
                return RecoveryDecision(
                    SupervisorAction.NOTIFY_JAY,
                    StopReason.RECOVERY_EXHAUSTED,
                    "RECOVERY_ATTEMPTS_EXHAUSTED",
                )
            return RecoveryDecision(
                SupervisorAction.LEASE_ALREADY_HELD,
                decision.effective_stop_reason,
                "ANOTHER_SUPERVISOR_OWNS_RECOVERY_LEASE",
            )

        route = route_for_attempt(lease.attempt_number)
        checkpoint = state.checkpoint
        try:
            try:
                verified, verify_detail = self.verifier.verify(checkpoint)
            except Exception as exc:
                verified = False
                verify_detail = "VERIFIER_EXCEPTION:" + type(exc).__name__
            if not verified:
                self.store.mark_stop(
                    task_id,
                    fencing_token=lease.fencing_token,
                    stop_reason=StopReason.WAITING_FOR_DEPENDENCY,
                    error="CHECKPOINT_VERIFICATION_FAILED:" + str(verify_detail),
                    now=current,
                )
                self._notify(
                    {
                        "event": "JAYTEC_AUTORECOVERY_CHECKPOINT_BLOCKED",
                        "task_id": task_id,
                        "detail": str(verify_detail),
                    }
                )
                return RecoveryDecision(
                    SupervisorAction.NOTIFY_JAY,
                    StopReason.WAITING_FOR_DEPENDENCY,
                    "CHECKPOINT_VERIFICATION_FAILED",
                )

            packet = build_continuation_packet(
                checkpoint,
                fencing_token=lease.fencing_token,
                recovery_route=route,
            )
            try:
                invocation = self.invoker.invoke(
                    checkpoint=checkpoint,
                    continuation_packet=packet,
                    route=route,
                    fencing_token=lease.fencing_token,
                )
            except Exception as exc:
                invocation = WorkerInvocation(
                    accepted=False,
                    worker_id=None,
                    route=route,
                    detail="INVOKER_EXCEPTION:" + type(exc).__name__,
                )
            if not invocation.accepted:
                stop_reason = (
                    StopReason.RECOVERY_EXHAUSTED
                    if lease.attempt_number >= self.max_recovery_attempts
                    else StopReason.TRANSIENT_PROVIDER_FAILURE
                )
                self.store.mark_stop(
                    task_id,
                    fencing_token=lease.fencing_token,
                    stop_reason=stop_reason,
                    error="RECOVERY_INVOCATION_REJECTED:" + invocation.detail,
                    now=current,
                )
                return RecoveryDecision(
                    SupervisorAction.RECOVERY_FAILED,
                    stop_reason,
                    "RECOVERY_INVOCATION_REJECTED",
                    recovery_route=route,
                )

            try:
                health = self.health_probe.wait_for_healthy(
                    task_id=task_id,
                    fencing_token=lease.fencing_token,
                    worker_id=invocation.worker_id,
                    timeout_seconds=self.health_verify_timeout_seconds,
                )
            except Exception as exc:
                health = WorkerHealth(
                    healthy=False,
                    worker_id=invocation.worker_id,
                    heartbeat_at=None,
                    progress_marker=None,
                    detail="HEALTH_PROBE_EXCEPTION:" + type(exc).__name__,
                )
            if not health.healthy or health.heartbeat_at is None:
                stop_reason = (
                    StopReason.RECOVERY_EXHAUSTED
                    if lease.attempt_number >= self.max_recovery_attempts
                    else StopReason.STALLED_RECOVERABLE
                )
                self.store.mark_stop(
                    task_id,
                    fencing_token=lease.fencing_token,
                    stop_reason=stop_reason,
                    error="RECOVERY_HEALTH_VERIFY_FAILED:" + health.detail,
                )
                return RecoveryDecision(
                    SupervisorAction.RECOVERY_FAILED,
                    stop_reason,
                    "RECOVERY_HEALTH_VERIFY_FAILED",
                    recovery_route=route,
                )

            terminal = self._terminal_worker_result(
                task_id,
                fencing_token=lease.fencing_token,
                health=health,
            )
            if terminal is not None:
                return terminal

            self.store.heartbeat(
                task_id,
                fencing_token=lease.fencing_token,
                worker_id=health.worker_id or invocation.worker_id or "unknown",
                progress_marker=health.progress_marker,
                now=health.heartbeat_at,
            )
            self._notify(
                {
                    "event": "JAYTEC_AUTORECOVERY_RESUMED",
                    "task_id": task_id,
                    "attempt": lease.attempt_number,
                    "recovery_route": route.value,
                    "fencing_token": lease.fencing_token,
                    "worker_id": health.worker_id or invocation.worker_id,
                    "instruction": "Resume — do not recreate completed work",
                }
            )
            return RecoveryDecision(
                SupervisorAction.RECOVERY_STARTED,
                StopReason.RUNNING,
                "RECOVERY_WORKER_HEALTHY",
                recovery_route=route,
                continuation_packet=packet,
            )
        finally:
            try:
                self.store.release_lease(lease)
            except AutoRecoveryError:
                # Never mask the actual recovery result. A stale lease/token
                # means another valid fencing transition already superseded it.
                pass


__all__ = [
    "AssignmentCheckpoint",
    "AssignmentState",
    "AutoRecoveryError",
    "AutoRecoverySupervisor",
    "HUMAN_OR_AUTHORITY_HOLDS",
    "MemoryAssignmentStore",
    "PostgresAssignmentStore",
    "RECOVERABLE_REASONS",
    "RecoveryDecision",
    "RecoveryLease",
    "RecoveryRoute",
    "StopReason",
    "SupervisorAction",
    "WorkerHealth",
    "WorkerInvocation",
    "WorkerKind",
    "build_continuation_packet",
    "classify_assignment",
    "route_for_attempt",
]
