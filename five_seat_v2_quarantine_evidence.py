from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    ProductionMigrationRefused,
    assert_url_identity,
)

DIAGNOSTIC_FLAG = "FIVE_SEAT_PROD_V2_QUARANTINE_DIAGNOSTIC"
TARGET_JOB_ID = "fabric-9e729002f85e75f7de97ee01"
TARGET_TASK_ID = "FS08-PRODUCTION-ADMISSION-002"


def _safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _blocker_summary(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return [{"source": "UNPARSEABLE_BLOCKER"}]
    items = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for raw in items[:20]:
        item = raw if isinstance(raw, Mapping) else {}
        unresolved = item.get("unresolved_operations") or []
        result.append(
            {
                "source": str(item.get("source") or ""),
                "reason": str(item.get("reason") or ""),
                "action": str(item.get("action") or ""),
                "unresolved_operation_count": (
                    len(unresolved) if isinstance(unresolved, list) else 0
                ),
            }
        )
    return result


def quarantine_evidence(
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
                SELECT job_id,task_id,status,health,fabric_state,seat_id,
                       lease_owner,lease_expires_at,ownership_epoch,fence_token,
                       source_shared_state_version,fabric_attempt_count,
                       fabric_max_attempts,blockers,updated_at
                FROM jaytec_jobs
                WHERE job_id=%s AND task_id=%s
                """,
                (TARGET_JOB_ID, TARGET_TASK_ID),
            )
            job = cur.fetchone()

            cur.execute(
                """
                SELECT operation_id,step_id,operation_type,status,
                       source_shared_state_version,ownership_epoch,fence_token,
                       retry_decision,started_at,verified_at,created_at,updated_at
                FROM jaytec_operations
                WHERE job_id=%s
                ORDER BY created_at ASC,operation_id ASC
                """,
                (TARGET_JOB_ID,),
            )
            operations = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT status,attempt_count,max_attempts,last_started_at,
                       completed_at,updated_at,
                       packet->>'side_effect_policy' AS side_effect_policy,
                       packet->'allowed_operations' AS allowed_operations,
                       packet->'specialist_plan' AS specialist_plan,
                       packet->>'max_retries' AS packet_max_retries
                FROM jaytec_task_packets
                WHERE job_id=%s
                """,
                (TARGET_JOB_ID,),
            )
            packet = cur.fetchone()

            cur.execute(
                """
                SELECT event_id,event_type,source,created_at
                FROM jaytec_job_events
                WHERE job_id=%s
                ORDER BY event_id ASC
                LIMIT 200
                """,
                (TARGET_JOB_ID,),
            )
            events = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT handoff_id,seat_id,worker_id,ownership_epoch,
                       job_fence_token,created_at,
                       payload->>'provider_identity' AS provider_identity,
                       payload->>'partial_side_effect_status'
                         AS partial_side_effect_status,
                       payload->>'worker_completion_classification'
                         AS worker_completion_classification
                FROM jaytec_worker_handoffs
                WHERE job_id=%s
                ORDER BY created_at ASC
                LIMIT 20
                """,
                (TARGET_JOB_ID,),
            )
            handoffs = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT review_id,decision,reason,controller_owner,
                       leader_epoch,fence_token,created_at
                FROM jaytec_watch_reviews
                WHERE job_id=%s
                ORDER BY created_at ASC
                LIMIT 20
                """,
                (TARGET_JOB_ID,),
            )
            reviews = [dict(row) for row in cur.fetchall()]

    job_safe = {
        key: _safe(value)
        for key, value in dict(job or {}).items()
        if key != "blockers"
    }
    job_safe["blockers"] = _blocker_summary(
        (job or {}).get("blockers") if job else None
    )

    unresolved = [
        row for row in operations
        if str(row.get("status") or "")
        in {"IN_FLIGHT", "UNCERTAIN_PARTIAL"}
    ]
    return {
        "schema_version": "JAYTEC_FS08_V2_QUARANTINE_EVIDENCE_V1",
        "database": PRODUCTION_DATABASE,
        "host_sha256": identity["host_sha256"],
        "read_only": True,
        "job": job_safe if job else None,
        "packet_control": {
            key: _safe(value)
            for key, value in dict(packet or {}).items()
        } if packet else None,
        "operations": [
            {key: _safe(value) for key, value in row.items()}
            for row in operations
        ],
        "unresolved_operation_count": len(unresolved),
        "unresolved_operation_ids": [
            str(row.get("operation_id") or "") for row in unresolved
        ],
        "events": [
            {key: _safe(value) for key, value in row.items()}
            for row in events
        ],
        "handoffs": [
            {key: _safe(value) for key, value in row.items()}
            for row in handoffs
        ],
        "watch_reviews": [
            {key: _safe(value) for key, value in row.items()}
            for row in reviews
        ],
    }


def main() -> None:
    if os.environ.get(DIAGNOSTIC_FLAG, "0").strip() != "1":
        return
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")
    result = quarantine_evidence(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_PROD_V2_QUARANTINE_DIAGNOSTIC="
        + json.dumps(result, sort_keys=True, default=str),
        flush=True,
    )


if __name__ == "__main__":
    main()
