from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional

import psycopg2
import psycopg2.extras

from concurrency import concurrency_decision
from durable_tasks import contains_secret_material
from five_seat_authority import authority_requires_approval, normalize_cost_policy
from five_seat_signals import PostgresFabricSignal, WORK_AVAILABLE_CHANNEL


FABRIC_ASSIGNMENT_TYPE = "FIVE_SEAT_FABRIC"
FABRIC_SUBMIT_LOCK_PREFIX = "jaytec-five-seat-submit:"
WORKER_KIND_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
AUTHORITY_CLASSES = frozenset(
    {
        "READ_ONLY",
        "SCOPED_MUTATION",
        "CANONICAL_SHARED",
        "EXTERNAL_SIDE_EFFECT",
        "GLOBAL_EXCLUSIVE",
        "OWNER_GATED",
    }
)


class FabricQueueError(RuntimeError):
    pass


class FabricEnvelopeConflict(FabricQueueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(dict(value)).encode("utf-8")).hexdigest()


def _string_list(value: Optional[Iterable[str]]) -> list[str]:
    result = sorted({str(item).strip() for item in (value or []) if str(item).strip()})
    return result


class PostgresFabricQueue:
    """Durable ingress for general five-seat assignments.

    jaytec_jobs remains the scheduling/state authority. This table stores the
    immutable execution envelope only; it is not a second queue.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.signal = PostgresFabricSignal(database_url)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def submit(
        self,
        *,
        task_id: str,
        objective: str,
        worker_kind: str,
        idempotency_key: str,
        source_shared_state_version: int,
        authority_class: str,
        concurrency_class: str,
        priority: int = 100,
        max_attempts: int = 3,
        max_reworks: int = 2,
        project_id: str = "JAYTEC",
        subtask_id: Optional[str] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        read_scope: Optional[Iterable[str]] = None,
        mutation_scope: Optional[Iterable[str]] = None,
        resource_scope: Optional[Mapping[str, Any]] = None,
        dependencies: Optional[Iterable[str]] = None,
        collision_key: Optional[str] = None,
        cost_policy: Optional[Mapping[str, Any]] = None,
        evidence_standard: Optional[Mapping[str, Any]] = None,
        stop_conditions: Optional[Mapping[str, Any]] = None,
        result_destination: Optional[Mapping[str, Any]] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        task_id = str(task_id or "").strip()
        objective = str(objective or "").strip()
        worker_kind = str(worker_kind or "").strip().upper()
        idempotency_key = str(idempotency_key or "").strip()
        authority_class = str(authority_class or "").strip().upper()
        concurrency_class = str(concurrency_class or "").strip().upper()
        if not task_id or not objective or not idempotency_key:
            raise ValueError("task_id, objective and idempotency_key are required")
        if not WORKER_KIND_PATTERN.fullmatch(worker_kind):
            raise ValueError("invalid worker_kind")
        if authority_class not in AUTHORITY_CLASSES:
            raise ValueError("invalid authority_class")
        if concurrency_class not in {"A", "B", "C", "D", "E"}:
            raise ValueError("invalid concurrency_class")
        if source_shared_state_version <= 0:
            raise ValueError("source_shared_state_version must be > 0")
        normalized_cost_policy = normalize_cost_policy(cost_policy)
        approval_required = authority_requires_approval(
            authority_class,
            normalized_cost_policy,
        )
        if priority < 0 or priority > 1_000_000:
            raise ValueError("priority out of range")
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if max_reworks < 0 or max_reworks > 5:
            raise ValueError("max_reworks must be between 0 and 5")

        capabilities = _string_list(required_capabilities)
        if any(not CAPABILITY_PATTERN.fullmatch(item) for item in capabilities):
            raise ValueError("invalid required capability")

        reads = _string_list(read_scope)
        mutations = _string_list(mutation_scope)
        resources = dict(resource_scope or {})
        deps = _string_list(dependencies)

        candidate = {
            "job_id": "preflight",
            "concurrency_class": concurrency_class,
            "read_scope": reads,
            "mutation_scope": mutations,
            "resource_scope": resources,
            "dependencies": [],
        }
        decision = concurrency_decision(candidate, [], completed_job_ids=[])
        if not decision.allowed:
            raise ValueError("unsafe_scope_contract:" + decision.reason)

        envelope = {
            "schema_version": "JAYTEC_FIVE_SEAT_ENVELOPE_V1",
            "task_id": task_id,
            "objective": objective,
            "worker_kind": worker_kind,
            "authority_class": authority_class,
            "concurrency_class": concurrency_class,
            "required_capabilities": capabilities,
            "read_scope": reads,
            "mutation_scope": mutations,
            "resource_scope": resources,
            "dependencies": deps,
            "collision_key": str(collision_key or "").strip(),
            "cost_policy": normalized_cost_policy,
            "approval_required": approval_required,
            "evidence_standard": dict(evidence_standard or {}),
            "stop_conditions": dict(stop_conditions or {}),
            "result_destination": dict(result_destination or {}),
            "payload": dict(payload or {}),
        }
        if contains_secret_material(envelope):
            raise ValueError("fabric_envelope_contains_secret_material")

        digest = _hash(envelope)
        job_id = "fabric-" + hashlib.sha256(
            (idempotency_key + ":" + digest).encode("utf-8")
        ).hexdigest()[:24]
        initial_status = "BLOCKED" if approval_required else "QUEUED"
        initial_fabric_state = "BLOCKED_OWNER" if approval_required else "QUEUED"

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (FABRIC_SUBMIT_LOCK_PREFIX + idempotency_key,),
                )
                cur.execute(
                    """
                    SELECT current_shared_state_version,authority_epoch,fence_token
                    FROM jaytec_fabric_authority_state
                    WHERE authority_id='FABRIC'
                    FOR UPDATE
                    """
                )
                authority = cur.fetchone()
                if authority is None:
                    raise FabricQueueError("fabric_authority_state_missing")
                current_shared_state_version = int(
                    authority["current_shared_state_version"]
                )
                if current_shared_state_version <= 0:
                    raise FabricQueueError("authority_state_uninitialized")
                if int(source_shared_state_version) != current_shared_state_version:
                    raise FabricQueueError(
                        "stale_source_shared_state_version:"
                        + str(source_shared_state_version)
                        + "!="
                        + str(current_shared_state_version)
                    )
                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_fabric_envelopes
                    WHERE idempotency_key=%s
                    FOR UPDATE
                    """,
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if existing["envelope_hash"] != digest:
                        raise FabricEnvelopeConflict("CONFLICTING_DUPLICATE")
                    cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s", (existing["job_id"],))
                    row = cur.fetchone()
                    if row is None:
                        raise FabricQueueError("envelope_without_job")
                    return dict(row)

                cur.execute(
                    """
                    INSERT INTO jaytec_jobs(
                      job_id,project_id,task_id,subtask_id,assignment_type,objective,
                      status,fabric_state,priority,source_shared_state_version,
                      concurrency_class,mutation_scope,read_scope,dependencies,
                      resource_scope,collision_key,task_packet_hash,
                      fabric_attempt_count,fabric_max_attempts,
                      fabric_rework_count,fabric_max_reworks,health
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,
                      0,%s,0,%s,'HEALTHY'
                    )
                    RETURNING *
                    """,
                    (
                        job_id,
                        str(project_id or "JAYTEC"),
                        task_id,
                        subtask_id,
                        FABRIC_ASSIGNMENT_TYPE,
                        objective[:4000],
                        initial_status,
                        initial_fabric_state,
                        priority,
                        int(source_shared_state_version),
                        concurrency_class,
                        _json(mutations),
                        _json(reads),
                        _json(deps),
                        _json(resources),
                        str(collision_key or "").strip() or None,
                        digest,
                        int(max_attempts),
                        int(max_reworks),
                    ),
                )
                job = cur.fetchone()
                if job is None:
                    raise FabricQueueError("job_insert_failed")

                cur.execute(
                    """
                    INSERT INTO jaytec_fabric_envelopes(
                      job_id,envelope_hash,idempotency_key,worker_kind,
                      required_capabilities,approval_required,authority_class,cost_policy,
                      evidence_standard,stop_conditions,result_destination,payload
                    ) VALUES (
                      %s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,
                      %s::jsonb,%s::jsonb,%s::jsonb
                    )
                    """,
                    (
                        job_id,
                        digest,
                        idempotency_key,
                        worker_kind,
                        _json(capabilities),
                        approval_required,
                        authority_class,
                        _json(normalized_cost_policy),
                        _json(dict(evidence_standard or {})),
                        _json(dict(stop_conditions or {})),
                        _json(dict(result_destination or {})),
                        _json(dict(payload or {})),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(
                      job_id,event_type,source,source_version,payload
                    ) VALUES (
                      %s,'FABRIC_TASK_ACCEPTED','FIVE_SEAT_INGRESS',%s,%s::jsonb
                    )
                    """,
                    (
                        job_id,
                        int(source_shared_state_version),
                        _json(
                            {
                                "worker_kind": worker_kind,
                                "authority_class": authority_class,
                                "required_capabilities": capabilities,
                                "envelope_hash": digest,
                                "blocked_owner": approval_required,
                                "approval_required": approval_required,
                                "max_attempts": int(max_attempts),
                                "max_reworks": int(max_reworks),
                            }
                        ),
                    ),
                )

        if not approval_required:
            self.signal.notify(
                WORK_AVAILABLE_CHANNEL,
                reason="fabric_task_submitted",
                job_id=job_id,
            )
        return dict(job)
