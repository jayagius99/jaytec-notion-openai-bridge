from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import psycopg2
import psycopg2.extras


CONCURRENCY_CLASSES = ("A", "B", "C", "D", "E")
SCHEDULER_LOCK_KEY = "jaytec-multi-assignment-concurrency-v1"
RUNNING_STATUSES = ("RUNNING",)
UNRESOLVED_OPERATION_STATUSES = ("IN_FLIGHT", "UNCERTAIN_PARTIAL")


class ConcurrencyError(RuntimeError):
    pass


class ConcurrencyConfigurationError(ConcurrencyError):
    pass


@dataclass(frozen=True)
class ConcurrencyDecision:
    allowed: bool
    reason: str
    conflicts: tuple[str, ...] = ()


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return set()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {text}
        return _as_string_set(decoded)
    if isinstance(value, Mapping):
        result: set[str] = set()
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                result.add(f"{key}={item}")
            elif isinstance(item, (list, tuple, set)):
                for child in item:
                    result.add(f"{key}={child}")
            else:
                result.add(f"{key}={json.dumps(item, sort_keys=True, default=str)}")
        return result
    if isinstance(value, Iterable):
        return {str(item).strip() for item in value if str(item).strip()}
    return {str(value).strip()} if str(value).strip() else set()


def _path_parts(scope: str) -> tuple[str, ...]:
    normalized = scope.strip().strip("/")
    if not normalized:
        return ()
    return tuple(part for part in normalized.replace("\\", "/").split("/") if part)


def scopes_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    """Return whether two resource scopes overlap.

    Exact equality conflicts. Hierarchical paths also conflict when one is a
    parent of the other (for example repo/src and repo/src/server.py). This is
    intentionally conservative and fail-closed for mutation safety.
    """

    left_set = {str(x).strip() for x in left if str(x).strip()}
    right_set = {str(x).strip() for x in right if str(x).strip()}
    if left_set & right_set:
        return True
    for a in left_set:
        a_parts = _path_parts(a)
        for b in right_set:
            b_parts = _path_parts(b)
            if not a_parts or not b_parts:
                continue
            shortest = min(len(a_parts), len(b_parts))
            if a_parts[:shortest] == b_parts[:shortest]:
                return True
    return False


def _job_class(job: Mapping[str, Any]) -> str:
    value = str(job.get("concurrency_class") or "").strip().upper()
    if value not in CONCURRENCY_CLASSES:
        raise ConcurrencyConfigurationError(
            f"job {job.get('job_id', '<unknown>')} has invalid concurrency_class {value!r}"
        )
    return value


def _mutation_scope(job: Mapping[str, Any]) -> set[str]:
    return _as_string_set(job.get("mutation_scope"))


def _read_scope(job: Mapping[str, Any]) -> set[str]:
    return _as_string_set(job.get("read_scope"))


def _resource_scope(job: Mapping[str, Any]) -> set[str]:
    return _as_string_set(job.get("resource_scope"))


def _dependencies(job: Mapping[str, Any]) -> set[str]:
    return _as_string_set(job.get("dependencies"))


def _scope_conflict(candidate: Mapping[str, Any], active: Mapping[str, Any]) -> bool:
    candidate_mut = _mutation_scope(candidate)
    candidate_read = _read_scope(candidate)
    active_mut = _mutation_scope(active)
    active_read = _read_scope(active)

    return (
        scopes_overlap(candidate_mut, active_mut)
        or scopes_overlap(candidate_mut, active_read)
        or scopes_overlap(candidate_read, active_mut)
    )


def _resource_conflict(candidate: Mapping[str, Any], active: Mapping[str, Any]) -> bool:
    return scopes_overlap(_resource_scope(candidate), _resource_scope(active))


def _validate_scopes(job: Mapping[str, Any]) -> Optional[str]:
    klass = _job_class(job)
    mutation = _mutation_scope(job)
    resources = _resource_scope(job)
    if klass == "A" and mutation:
        return "CLASS_A_MUST_BE_READ_ONLY"
    if klass in {"B", "C", "D", "E"} and not mutation and not resources:
        return "MUTATING_CLASS_REQUIRES_DECLARED_SCOPE"
    return None


def concurrency_decision(
    candidate: Mapping[str, Any],
    active_jobs: Sequence[Mapping[str, Any]],
    *,
    completed_job_ids: Optional[Iterable[str]] = None,
    unresolved_side_effect_job_ids: Optional[Iterable[str]] = None,
) -> ConcurrencyDecision:
    """Decide whether candidate may start alongside active jobs.

    Operational class semantics:
    A = read-only work. May run concurrently unless it reads something an
        active mutator is changing.
    B = scoped mutation. May run concurrently only with disjoint read/write
        and resource scopes.
    C = shared/canonical-state mutation. Serialized against other C/D/E work
        and against any overlapping read/write scope.
    D = external side effect / deployment / production mutation. Serialized
        against C/D/E and any overlapping declared resource or read/write scope.
    E = global-exclusive/high-risk work. Runs alone.

    Unknown or under-specified mutating jobs fail closed.
    """

    candidate_id = str(candidate.get("job_id") or "")
    config_error = _validate_scopes(candidate)
    if config_error:
        return ConcurrencyDecision(False, config_error)

    completed = {str(x) for x in (completed_job_ids or [])}
    missing_dependencies = sorted(_dependencies(candidate) - completed)
    if missing_dependencies:
        return ConcurrencyDecision(
            False,
            "DEPENDENCIES_INCOMPLETE",
            tuple(missing_dependencies),
        )

    unresolved = {str(x) for x in (unresolved_side_effect_job_ids or [])}
    candidate_class = _job_class(candidate)
    if candidate_class == "E" and active_jobs:
        return ConcurrencyDecision(
            False,
            "GLOBAL_EXCLUSIVE_REQUIRES_EMPTY_RUNTIME",
            tuple(str(job.get("job_id") or "") for job in active_jobs),
        )

    conflicts: list[str] = []
    reasons: list[str] = []
    for active in active_jobs:
        active_id = str(active.get("job_id") or "")
        if not active_id or active_id == candidate_id:
            continue
        active_error = _validate_scopes(active)
        if active_error:
            conflicts.append(active_id)
            reasons.append("ACTIVE_JOB_CONFIGURATION_UNSAFE")
            continue
        active_class = _job_class(active)

        if active_class == "E":
            conflicts.append(active_id)
            reasons.append("ACTIVE_GLOBAL_EXCLUSIVE")
            continue

        if active_id in unresolved:
            conflicts.append(active_id)
            reasons.append("ACTIVE_UNRESOLVED_SIDE_EFFECT")
            continue

        if candidate_class == "A":
            if scopes_overlap(_read_scope(candidate), _mutation_scope(active)):
                conflicts.append(active_id)
                reasons.append("READ_WRITE_SCOPE_CONFLICT")
            continue

        if candidate_class == "B":
            if _scope_conflict(candidate, active) or _resource_conflict(candidate, active):
                conflicts.append(active_id)
                reasons.append("SCOPED_MUTATION_CONFLICT")
            continue

        if candidate_class == "C":
            if active_class in {"C", "D", "E"}:
                conflicts.append(active_id)
                reasons.append("SHARED_STATE_SERIALIZATION")
            elif _scope_conflict(candidate, active) or _resource_conflict(candidate, active):
                conflicts.append(active_id)
                reasons.append("SHARED_STATE_SCOPE_CONFLICT")
            continue

        if candidate_class == "D":
            if active_class in {"C", "D", "E"}:
                conflicts.append(active_id)
                reasons.append("EXTERNAL_SIDE_EFFECT_SERIALIZATION")
            elif _scope_conflict(candidate, active) or _resource_conflict(candidate, active):
                conflicts.append(active_id)
                reasons.append("EXTERNAL_SIDE_EFFECT_SCOPE_CONFLICT")
            continue

        if candidate_class == "E":
            conflicts.append(active_id)
            reasons.append("GLOBAL_EXCLUSIVE")

    if conflicts:
        reason = sorted(set(reasons))[0] if reasons else "CONCURRENCY_CONFLICT"
        return ConcurrencyDecision(False, reason, tuple(sorted(set(conflicts))))
    return ConcurrencyDecision(True, "SAFE_TO_RUN")


def duplicate_assignment(
    candidate: Mapping[str, Any], active_jobs: Sequence[Mapping[str, Any]]
) -> Optional[str]:
    """Return conflicting job id for an obvious duplicate assignment."""

    project_id = str(candidate.get("project_id") or "")
    task_id = str(candidate.get("task_id") or "")
    assignment_type = str(candidate.get("assignment_type") or "")
    for active in active_jobs:
        if (
            str(active.get("project_id") or "") == project_id
            and str(active.get("task_id") or "") == task_id
            and str(active.get("assignment_type") or "") == assignment_type
        ):
            if _scope_conflict(candidate, active) or _resource_conflict(candidate, active):
                return str(active.get("job_id") or "")
    return None


def select_runnable_jobs(
    queued_jobs: Sequence[Mapping[str, Any]],
    active_jobs: Sequence[Mapping[str, Any]],
    *,
    completed_job_ids: Optional[Iterable[str]] = None,
    unresolved_side_effect_job_ids: Optional[Iterable[str]] = None,
    max_parallel: int = 4,
) -> list[Mapping[str, Any]]:
    """Deterministically choose a safe concurrent batch.

    Jobs are ordered by priority then job id. Each selected job becomes active
    for the next decision so two queued jobs that conflict cannot both be
    admitted in the same batch.
    """

    if max_parallel < 1:
        return []
    active = list(active_jobs)
    available_slots = max(0, max_parallel - len(active))
    if available_slots == 0:
        return []

    selected: list[Mapping[str, Any]] = []
    ordered = sorted(
        queued_jobs,
        key=lambda job: (int(job.get("priority") or 100), str(job.get("job_id") or "")),
    )
    for candidate in ordered:
        if len(selected) >= available_slots:
            break
        duplicate = duplicate_assignment(candidate, active)
        if duplicate:
            continue
        decision = concurrency_decision(
            candidate,
            active,
            completed_job_ids=completed_job_ids,
            unresolved_side_effect_job_ids=unresolved_side_effect_job_ids,
        )
        if decision.allowed:
            selected.append(candidate)
            active.append(candidate)
    return selected


class PostgresConcurrencyScheduler:
    """Atomic admission control for independent durable JAYTEC jobs.

    Only the scheduling decision is globally serialized. Once claimed, each
    job is independently protected by its own ownership epoch, fence token and
    lease in the v1.3 durable runtime.
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
                # Serialize only admission decisions across workers/tabs.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEDULER_LOCK_KEY,))

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
                if len(active) >= self.max_parallel:
                    return None

                cur.execute(
                    """
                    SELECT DISTINCT job_id FROM jaytec_operations
                    WHERE status = ANY(%s)
                    """,
                    (list(UNRESOLVED_OPERATION_STATUSES),),
                )
                unresolved = {str(row["job_id"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT job_id FROM jaytec_jobs
                    WHERE status='SUCCEEDED'
                    """
                )
                completed = {str(row["job_id"]) for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT * FROM jaytec_jobs
                    WHERE status IN ('QUEUED','PAUSED')
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
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
                            lease_owner=%s,
                            lease_expires_at=now() + (%s * interval '1 second'),
                            execution_room_id=%s,
                            ownership_epoch=ownership_epoch+1,
                            fence_token=fence_token+1,
                            version=version+1,
                            updated_at=now()
                        WHERE job_id=%s
                          AND status IN ('QUEUED','PAUSED')
                          AND (lease_expires_at IS NULL OR lease_expires_at < now())
                        RETURNING *
                        """,
                        (owner, lease_seconds, execution_room_id, candidate["job_id"]),
                    )
                    claimed = cur.fetchone()
                    if claimed is None:
                        continue

                    cur.execute(
                        """
                        INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                        VALUES (%s,'JOB_CONCURRENCY_ADMITTED','CONCURRENCY_SCHEDULER',%s,%s::jsonb)
                        """,
                        (
                            claimed["job_id"],
                            claimed.get("source_shared_state_version"),
                            json.dumps(
                                {
                                    "execution_room_id": execution_room_id,
                                    "concurrency_class": claimed.get("concurrency_class"),
                                    "scheduler": "v1",
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
                    return dict(claimed)

                return None
