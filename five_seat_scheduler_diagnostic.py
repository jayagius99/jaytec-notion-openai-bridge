from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

from concurrency import concurrency_decision, duplicate_assignment
from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    ProductionMigrationRefused,
    assert_url_identity,
)
from five_seat_runtime import quarantine_conflict

DIAGNOSTIC_FLAG = "FIVE_SEAT_PROD_SCHEDULER_DIAGNOSTIC"
TARGET_JOB_ID = "fabric-9e729002f85e75f7de97ee01"
TARGET_TASK_ID = "FS08-PRODUCTION-ADMISSION-002"
TARGET_WORKER_KIND = "TASK_PACKET"


def _safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _safe_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: _safe(value)
        for key, value in dict(row or {}).items()
    }


def _diagnose_rows(
    *,
    target: Mapping[str, Any] | None,
    circuit: Mapping[str, Any] | None,
    quarantined: list[Mapping[str, Any]],
    active: list[Mapping[str, Any]],
    seats: list[Mapping[str, Any]],
    authority: Mapping[str, Any] | None,
) -> dict[str, Any]:
    target_row = dict(target or {})
    authority_row = dict(authority or {})
    current_version = int(
        authority_row.get("current_shared_state_version") or 0
    )
    source_version = int(
        target_row.get("source_shared_state_version") or -1
    )
    attempts = int(target_row.get("fabric_attempt_count") or 0)
    max_attempts = int(target_row.get("fabric_max_attempts") or 0)

    conflicting_quarantine: list[dict[str, Any]] = []
    if target_row:
        for row in quarantined:
            if quarantine_conflict(target_row, [row]):
                conflicting_quarantine.append(_safe_row(row))

    duplicate = (
        duplicate_assignment(target_row, active)
        if target_row
        else None
    )
    concurrency = None
    if target_row:
        decision = concurrency_decision(target_row, active)
        concurrency = {
            "allowed": bool(decision.allowed),
            "reason": str(decision.reason),
            "conflicts": list(decision.conflicts),
        }

    circuit_row = dict(circuit or {})
    circuit_state = str(circuit_row.get("state") or "MISSING").upper()
    open_until = circuit_row.get("open_until")
    now = datetime.now(timezone.utc)
    if isinstance(open_until, datetime) and open_until.tzinfo is None:
        open_until = open_until.replace(tzinfo=timezone.utc)
    circuit_window_elapsed = bool(
        circuit_state == "OPEN"
        and isinstance(open_until, datetime)
        and open_until <= now
    )
    circuit_claim_eligible = bool(
        circuit_state in {"MISSING", "CLOSED"} or circuit_window_elapsed
    )

    blockers: list[str] = []
    if not target_row:
        blockers.append("TARGET_JOB_NOT_FOUND")
    else:
        if str(target_row.get("status") or "") not in {"QUEUED", "PAUSED"}:
            blockers.append("TARGET_STATUS_NOT_RUNNABLE")
        if str(target_row.get("fabric_state") or "") not in {
            "QUEUED", "REWORK_QUEUED"
        }:
            blockers.append("TARGET_FABRIC_STATE_NOT_RUNNABLE")
        if target_row.get("seat_id") is not None:
            blockers.append("TARGET_ALREADY_HAS_SEAT")
        if not bool(target_row.get("lease_ready")):
            blockers.append("TARGET_LEASE_NOT_READY")
        if not bool(target_row.get("next_attempt_ready")):
            blockers.append("TARGET_NEXT_ATTEMPT_NOT_READY")
        if attempts >= max_attempts:
            blockers.append("ATTEMPT_BUDGET_EXHAUSTED")
        if current_version <= 0 or source_version != current_version:
            blockers.append("AUTHORITY_VERSION_MISMATCH")
        if conflicting_quarantine:
            blockers.append("OVERLAPPING_QUARANTINE")
        if duplicate:
            blockers.append("DUPLICATE_ACTIVE_ASSIGNMENT")
        if concurrency and not concurrency["allowed"]:
            blockers.append("CONCURRENCY_POLICY_BLOCK")
        if not circuit_claim_eligible:
            blockers.append("WORKER_KIND_CIRCUIT_BLOCK")

    return {
        "schema_version": "JAYTEC_FS08_SCHEDULER_DIAGNOSTIC_V1",
        "read_only": True,
        "target_job": _safe_row(target_row) if target_row else None,
        "authority": _safe_row(authority_row),
        "worker_kind_circuit": _safe_row(circuit_row) if circuit_row else None,
        "circuit_claim_eligible": circuit_claim_eligible,
        "circuit_open_window_elapsed": circuit_window_elapsed,
        "overlapping_quarantine": conflicting_quarantine,
        "duplicate_active_job_id": duplicate,
        "concurrency": concurrency,
        "active_fabric_jobs": [_safe_row(row) for row in active],
        "seats": [_safe_row(row) for row in seats],
        "mechanical_blockers": blockers,
    }


def scheduler_snapshot(
    database_url: str,
    expected_host_sha256: str,
) -> dict[str, Any]:
    identity = assert_url_identity(database_url, expected_host_sha256)
    with psycopg2.connect(database_url) as conn:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT current_database() AS db")
            db = str((cur.fetchone() or {}).get("db") or "")
            if db != PRODUCTION_DATABASE:
                raise ProductionMigrationRefused(
                    "CONNECTED_DATABASE_NAME_MISMATCH:" + db
                )

            cur.execute(
                """
                SELECT
                  j.job_id,j.task_id,j.assignment_type,j.status,j.health,
                  j.fabric_state,j.seat_id,j.lease_owner,j.lease_expires_at,
                  j.ownership_epoch,j.fence_token,
                  j.source_shared_state_version,j.fabric_attempt_count,
                  j.fabric_max_attempts,j.next_attempt_at,j.concurrency_class,
                  j.mutation_scope,j.read_scope,j.resource_scope,
                  j.dependencies,j.collision_key,j.updated_at,
                  (j.lease_expires_at IS NULL OR j.lease_expires_at < now())
                    AS lease_ready,
                  (j.next_attempt_at IS NULL OR j.next_attempt_at <= now())
                    AS next_attempt_ready,
                  e.worker_kind,e.required_capabilities,e.authority_class,
                  e.approval_required,e.cost_policy
                FROM jaytec_jobs j
                JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                WHERE j.job_id=%s AND j.task_id=%s
                """,
                (TARGET_JOB_ID, TARGET_TASK_ID),
            )
            target = cur.fetchone()

            cur.execute(
                """
                SELECT worker_kind,state,consecutive_failures,
                       failure_threshold,open_until,probe_job_id,
                       probe_started_at,version,updated_at
                FROM jaytec_fabric_circuits
                WHERE worker_kind=%s
                """,
                (TARGET_WORKER_KIND,),
            )
            circuit = cur.fetchone()

            cur.execute(
                """
                SELECT j.job_id,j.task_id,j.assignment_type,j.status,
                       j.fabric_state,j.concurrency_class,j.mutation_scope,
                       j.read_scope,j.resource_scope,j.dependencies,
                       j.collision_key,j.updated_at
                FROM jaytec_jobs j
                WHERE j.fabric_state='QUARANTINED'
                ORDER BY j.updated_at DESC,j.job_id ASC
                LIMIT 100
                """
            )
            quarantined = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT j.job_id,j.task_id,j.assignment_type,j.status,
                       j.fabric_state,j.concurrency_class,j.mutation_scope,
                       j.read_scope,j.resource_scope,j.dependencies,
                       j.collision_key,j.seat_id,j.updated_at
                FROM jaytec_jobs j
                WHERE j.status='RUNNING'
                  AND j.assignment_type='FIVE_SEAT_FABRIC'
                ORDER BY j.updated_at DESC,j.job_id ASC
                LIMIT 20
                """
            )
            active = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT seat_id,state,current_job_id,lease_owner,
                       lease_expires_at,seat_epoch,fence_token,updated_at
                FROM jaytec_worker_seats
                ORDER BY seat_id
                """
            )
            seats = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT authority_id,current_shared_state_version,
                       authority_epoch,fence_token,updated_by,updated_at
                FROM jaytec_fabric_authority_state
                WHERE authority_id='FABRIC'
                """
            )
            authority = cur.fetchone()

    result = _diagnose_rows(
        target=target,
        circuit=circuit,
        quarantined=quarantined,
        active=active,
        seats=seats,
        authority=authority,
    )
    result["database"] = PRODUCTION_DATABASE
    result["host_sha256"] = identity["host_sha256"]
    return result


def main() -> None:
    if os.environ.get(DIAGNOSTIC_FLAG, "0").strip() != "1":
        return
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")
    result = scheduler_snapshot(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_PROD_SCHEDULER_DIAGNOSTIC="
        + json.dumps(result, sort_keys=True, default=str),
        flush=True,
    )


if __name__ == "__main__":
    main()
