from __future__ import annotations

import json
import os
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    ProductionMigrationRefused,
    assert_url_identity,
)

RECONCILE_FLAG = "FIVE_SEAT_PROD_RECONCILE_LEGACY_JOBS"
RECONCILE_LOCK = "JAYTEC_FS08_LEGACY_ACTIVE_ROW_RECONCILIATION_V1"
FREEZE_REF = "https://github.com/jayagius99/jaytec-work-engine-v2-g1/issues/126"
FREEZE_REASON = "FIVE_SEAT_TRANSFORMATION_SOLE_PRIORITY"

EXPECTED_JOBS: dict[str, dict[str, Any]] = {
    "watch-FORGE-GENESIS-ACTIVATION-001": {
        "task_id": "FORGE-GENESIS-ACTIVATION-001",
        "assignment_type": "OWNER_CHAT_WATCH",
        "checkpoint_ref": "event:84",
        "source_shared_state_version": 55,
        "ownership_epoch": 1,
        "fence_token": 1,
    },
    "watch-e1a11f3e7e2e3eea408a971a": {
        "task_id": "FORGE-COGNITION-PERFORMANCE-REVIEW-001",
        "assignment_type": "ARCHITECTURE_REVIEW",
        "checkpoint_ref": None,
        "source_shared_state_version": 55,
        "ownership_epoch": 1,
        "fence_token": 1,
    },
}


class LegacyJobReconciliationRefused(RuntimeError):
    pass


def _active_rows(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT job_id,task_id,assignment_type,status,health,checkpoint_ref,
               source_shared_state_version,ownership_epoch,fence_token,
               lease_owner,lease_expires_at
        FROM jaytec_jobs
        WHERE status='RUNNING'
           OR (lease_expires_at IS NOT NULL AND lease_expires_at > now())
        ORDER BY job_id
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _validate_exact_active_set(rows: list[Mapping[str, Any]]) -> None:
    observed = {str(row.get("job_id") or "") for row in rows}
    expected = set(EXPECTED_JOBS)
    if observed != expected:
        raise LegacyJobReconciliationRefused(
            "ACTIVE_JOB_SET_MISMATCH:"
            + ",".join(sorted(observed))
        )

    for row in rows:
        job_id = str(row["job_id"])
        spec = EXPECTED_JOBS[job_id]
        checks = {
            "task_id": spec["task_id"],
            "assignment_type": spec["assignment_type"],
            "status": "RUNNING",
            "checkpoint_ref": spec["checkpoint_ref"],
            "source_shared_state_version": spec["source_shared_state_version"],
            "ownership_epoch": spec["ownership_epoch"],
            "fence_token": spec["fence_token"],
            "lease_owner": None,
            "lease_expires_at": None,
        }
        for field, expected_value in checks.items():
            if row.get(field) != expected_value:
                raise LegacyJobReconciliationRefused(
                    f"LEGACY_JOB_FIELD_MISMATCH:{job_id}:{field}"
                )


def _assert_no_live_child_state(cur, job_id: str) -> None:
    cur.execute(
        """
        SELECT count(*)::int AS n
        FROM jaytec_operations
        WHERE job_id=%s AND status IN ('IN_FLIGHT','UNCERTAIN_PARTIAL')
        """,
        (job_id,),
    )
    if int((cur.fetchone() or {}).get("n") or 0):
        raise LegacyJobReconciliationRefused(
            "UNRESOLVED_OPERATION_PRESENT:" + job_id
        )

    cur.execute(
        """
        SELECT count(*)::int AS n
        FROM jaytec_job_steps
        WHERE job_id=%s AND status IN ('RUNNING','WAITING')
        """,
        (job_id,),
    )
    if int((cur.fetchone() or {}).get("n") or 0):
        raise LegacyJobReconciliationRefused(
            "LIVE_STEP_PRESENT:" + job_id
        )

    cur.execute(
        "SELECT status FROM jaytec_task_packets WHERE job_id=%s",
        (job_id,),
    )
    packet = cur.fetchone()
    if packet and str(packet.get("status") or "") == "RUNNING":
        raise LegacyJobReconciliationRefused(
            "RUNNING_TASK_PACKET_PRESENT:" + job_id
        )


def reconcile_connection(
    conn,
    *,
    expected_database: str = PRODUCTION_DATABASE,
) -> dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (RECONCILE_LOCK,),
        )
        cur.execute("SELECT current_database() AS db")
        db = str((cur.fetchone() or {}).get("db") or "")
        if db != expected_database:
            raise LegacyJobReconciliationRefused(
                "CONNECTED_DATABASE_NAME_MISMATCH:" + db
            )

        rows = _active_rows(cur)
        _validate_exact_active_set(rows)
        for row in rows:
            _assert_no_live_child_state(cur, str(row["job_id"]))

        parked: list[dict[str, Any]] = []
        blocker = json.dumps(
            [
                {
                    "type": "OWNER_FREEZE",
                    "ref": FREEZE_REF,
                    "reason": FREEZE_REASON,
                    "resume_rule": "EXPLICIT_OWNER_RELEASE_AND_RECONCILIATION",
                }
            ],
            sort_keys=True,
        )
        for row in rows:
            job_id = str(row["job_id"])
            spec = EXPECTED_JOBS[job_id]
            cur.execute(
                """
                UPDATE jaytec_jobs
                SET status='BLOCKED',
                    health='BLOCKED',
                    blockers=COALESCE(blockers,'[]'::jsonb) || %s::jsonb,
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    execution_room_id=NULL,
                    next_attempt_at=NULL,
                    ownership_epoch=ownership_epoch+1,
                    fence_token=fence_token+1,
                    version=version+1,
                    updated_at=now()
                WHERE job_id=%s
                  AND task_id=%s
                  AND assignment_type=%s
                  AND status='RUNNING'
                  AND checkpoint_ref IS NOT DISTINCT FROM %s
                  AND source_shared_state_version=%s
                  AND ownership_epoch=%s
                  AND fence_token=%s
                  AND lease_owner IS NULL
                  AND lease_expires_at IS NULL
                RETURNING job_id,task_id,status,health,checkpoint_ref,
                          ownership_epoch,fence_token
                """,
                (
                    blocker,
                    job_id,
                    spec["task_id"],
                    spec["assignment_type"],
                    spec["checkpoint_ref"],
                    spec["source_shared_state_version"],
                    spec["ownership_epoch"],
                    spec["fence_token"],
                ),
            )
            updated = cur.fetchone()
            if updated is None:
                raise LegacyJobReconciliationRefused(
                    "TARGET_CHANGED_DURING_RECONCILIATION:" + job_id
                )

            cur.execute(
                """
                INSERT INTO jaytec_job_events(
                    job_id,event_type,source,source_version,payload
                ) VALUES (
                    %s,'OWNER_FREEZE_PARKED','FS08_RECONCILIATION',
                    %s,%s::jsonb
                )
                """,
                (
                    job_id,
                    spec["source_shared_state_version"],
                    json.dumps(
                        {
                            "freeze_ref": FREEZE_REF,
                            "reason": FREEZE_REASON,
                            "previous_status": "RUNNING",
                            "previous_ownership_epoch": spec["ownership_epoch"],
                            "previous_fence_token": spec["fence_token"],
                            "new_status": "BLOCKED",
                            "new_ownership_epoch": int(updated["ownership_epoch"]),
                            "new_fence_token": int(updated["fence_token"]),
                            "checkpoint_ref": updated["checkpoint_ref"],
                        },
                        sort_keys=True,
                    ),
                ),
            )
            parked.append(
                {
                    "job_id": str(updated["job_id"]),
                    "task_id": str(updated["task_id"]),
                    "status": str(updated["status"]),
                    "checkpoint_ref": updated["checkpoint_ref"],
                    "ownership_epoch": int(updated["ownership_epoch"]),
                    "fence_token": int(updated["fence_token"]),
                }
            )

        remaining = _active_rows(cur)
        if remaining:
            raise LegacyJobReconciliationRefused(
                "ACTIVE_JOBS_REMAIN_AFTER_PARK:"
                + ",".join(sorted(str(row["job_id"]) for row in remaining))
            )

    return {
        "status": "LEGACY_JOBS_PARKED",
        "freeze_ref": FREEZE_REF,
        "parked_count": len(parked),
        "parked": parked,
        "active_job_count_after": 0,
    }


def reconcile_legacy_jobs(
    database_url: str,
    expected_host_sha256: str,
) -> dict[str, Any]:
    identity = assert_url_identity(database_url, expected_host_sha256)
    with psycopg2.connect(database_url) as conn:
        result = reconcile_connection(conn)
    return {
        **result,
        "database": PRODUCTION_DATABASE,
        "host_sha256": identity["host_sha256"],
    }


def main() -> None:
    if os.environ.get(RECONCILE_FLAG, "0").strip() != "1":
        return
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")
    result = reconcile_legacy_jobs(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_LEGACY_JOB_RECONCILIATION="
        + json.dumps(result, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
