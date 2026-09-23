from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    ProductionMigrationRefused,
    assert_url_identity,
)

DIAGNOSTIC_FLAG = "FIVE_SEAT_PROD_ACTIVE_JOB_DIAGNOSTIC"


def _safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def active_job_snapshot(
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
                SELECT job_id,task_id,subtask_id,assignment_type,status,health,
                       lease_owner,lease_expires_at,ownership_epoch,fence_token,
                       checkpoint_ref,source_shared_state_version,
                       created_at,updated_at
                FROM jaytec_jobs
                WHERE status='RUNNING'
                   OR (
                     lease_expires_at IS NOT NULL
                     AND lease_expires_at > now()
                   )
                ORDER BY updated_at DESC,job_id ASC
                """
            )
            jobs = [
                {key: _safe(value) for key, value in dict(row).items()}
                for row in cur.fetchall()
            ]
    return {
        "database": PRODUCTION_DATABASE,
        "host_sha256": identity["host_sha256"],
        "active_job_count": len(jobs),
        "jobs": jobs,
        "read_only": True,
    }


def main() -> None:
    if os.environ.get(DIAGNOSTIC_FLAG, "0").strip() != "1":
        return
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")
    snapshot = active_job_snapshot(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_ACTIVE_JOB_DIAGNOSTIC="
        + json.dumps(snapshot, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
