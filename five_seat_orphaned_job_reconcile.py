from __future__ import annotations

import json
import os
from typing import Any

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    ProductionMigrationRefused,
    assert_url_identity,
)

RECONCILE_FLAG = "FIVE_SEAT_PROD_RECONCILE_ORPHANED_JOBS"
RECONCILE_LOCK = "JAYTEC_FS08_ORPHANED_RUNNING_RECONCILE_V1"

EXPECTED = {
    "watch-FORGE-GENESIS-ACTIVATION-001": {
        "task_id": "FORGE-GENESIS-ACTIVATION-001",
        "assignment_type": "OWNER_CHAT_WATCH",
        "ownership_epoch": 1,
        "fence_token": 1,
        "source_shared_state_version": 55,
    },
    "watch-e1a11f3e7e2e3eea408a971a": {
        "task_id": "FORGE-COGNITION-PERFORMANCE-REVIEW-001",
        "assignment_type": "ARCHITECTURE_REVIEW",
        "ownership_epoch": 1,
        "fence_token": 1,
        "source_shared_state_version": 55,
    },
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_expected(row: dict[str, Any]) -> None:
    job_id = str(row.get("job_id") or "")
    expected = EXPECTED.get(job_id)
    if expected is None:
        raise ProductionMigrationRefused("UNEXPECTED_JOB:" + job_id)
    for key, value in expected.items():
        if row.get(key) != value:
            raise ProductionMigrationRefused(
                f"JOB_METADATA_DRIFT:{job_id}:{key}"
            )
    if row.get("status") != "RUNNING":
        raise ProductionMigrationRefused("JOB_NOT_RUNNING:" + job_id)
    if row.get("lease_owner") is not None:
        raise ProductionMigrationRefused("JOB_HAS_LEASE_OWNER:" + job_id)
    if row.get("lease_expires_at") is not None:
        raise ProductionMigrationRefused("JOB_HAS_LEASE_EXPIRY:" + job_id)


def reconcile(database_url: str, expected_host_sha256: str) -> dict[str, Any]:
    identity = assert_url_identity(database_url, expected_host_sha256)
    expected_ids = sorted(EXPECTED)
    with psycopg2.connect(database_url) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (RECONCILE_LOCK,),
            )
            cur.execute("SELECT current_database() AS db")
            db = str((cur.fetchone() or {}).get("db") or "")
            if db != PRODUCTION_DATABASE:
                raise ProductionMigrationRefused(
                    "CONNECTED_DATABASE_NAME_MISMATCH:" + db
                )
            cur.execute(
                """
                SELECT job_id,task_id,assignment_type,status,health,
                       lease_owner,lease_expires_at,ownership_epoch,fence_token,
                       source_shared_state_version,checkpoint_ref
                FROM jaytec_jobs
                WHERE job_id = ANY(%s)
                ORDER BY job_id
                FOR UPDATE
                """,
                (expected_ids,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if sorted(row["job_id"] for row in rows) != expected_ids:
                raise ProductionMigrationRefused(
                    "EXPECTED_JOB_SET_MISMATCH"
                )
            for row in rows:
                _assert_expected(row)

            cur.execute(
                """
                SELECT job_id,operation_id,status
                FROM jaytec_operations
                WHERE job_id = ANY(%s)
                  AND status IN ('IN_FLIGHT','UNCERTAIN_PARTIAL')
                ORDER BY job_id,operation_id
                """,
                (expected_ids,),
            )
            unresolved = [dict(row) for row in cur.fetchall()]
            if unresolved:
                raise ProductionMigrationRefused(
                    "UNRESOLVED_OPERATIONS_PRESENT:" + str(len(unresolved))
                )

            parked: list[dict[str, Any]] = []
            for row in rows:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status='PAUSED',
                        health='DEGRADED',
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        execution_room_id=NULL,
                        ownership_epoch=ownership_epoch+1,
                        fence_token=fence_token+1,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND task_id=%s
                      AND assignment_type=%s
                      AND status='RUNNING'
                      AND lease_owner IS NULL
                      AND lease_expires_at IS NULL
                      AND ownership_epoch=%s
                      AND fence_token=%s
                      AND source_shared_state_version=%s
                    RETURNING job_id,task_id,status,health,
                              ownership_epoch,fence_token,checkpoint_ref
                    """,
                    (
                        row["job_id"],
                        row["task_id"],
                        row["assignment_type"],
                        row["ownership_epoch"],
                        row["fence_token"],
                        row["source_shared_state_version"],
                    ),
                )
                updated = cur.fetchone()
                if updated is None:
                    raise ProductionMigrationRefused(
                        "JOB_CHANGED_DURING_RECONCILE:" + row["job_id"]
                    )
                parked.append(dict(updated))
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(
                      job_id,event_type,source,source_version,payload
                    ) VALUES (
                      %s,'FS08_LEGACY_ORPHANED_RUNNING_PARKED',
                      'FS08_CUTOVER_RECONCILER',%s,%s::jsonb
                    )
                    """,
                    (
                        row["job_id"],
                        row["source_shared_state_version"],
                        _json({
                            "reason": "OWNER_FREEZE_126_CUTOVER_RECONCILIATION",
                            "prior_status": "RUNNING",
                            "prior_ownership_epoch": row["ownership_epoch"],
                            "prior_fence_token": row["fence_token"],
                            "prior_checkpoint_ref": row.get("checkpoint_ref"),
                            "lease_owner": None,
                            "lease_expires_at": None,
                        }),
                    ),
                )

    return {
        "status": "PARKED_SAFE",
        "database": PRODUCTION_DATABASE,
        "host_sha256": identity["host_sha256"],
        "parked": parked,
    }


def main() -> None:
    if os.environ.get(RECONCILE_FLAG, "0").strip() != "1":
        return
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")
    result = reconcile(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_ORPHANED_JOB_RECONCILIATION="
        + json.dumps(result, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
