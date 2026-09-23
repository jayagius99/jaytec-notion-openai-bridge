from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    ProductionMigrationRefused,
    assert_url_identity,
)

RECONCILE_FLAG = "FIVE_SEAT_PROD_V2_EXPIRED_RECONCILE"
LOCK_KEY = "JAYTEC_FS08_V2_EXPIRED_PROBE_RECLASSIFY_V1"

V2_JOB_ID = "fabric-9e729002f85e75f7de97ee01"
V2_TASK_ID = "FS08-PRODUCTION-ADMISSION-002"
V2_IDEMPOTENCY_KEY = "fs08-production-admission-v2"
V2_EXPECTED_DEADLINE = "2026-09-23T11:40:00Z"
V1_JOB_ID = "fabric-91df7f14706752173566201d"
EXPECTED_SOURCE_VERSION = 55


class ExpiredV2ReconcileRefused(RuntimeError):
    pass


def _packet_from_envelope(payload: Any) -> dict[str, Any]:
    value = payload
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise ExpiredV2ReconcileRefused(
                "ENVELOPE_PAYLOAD_JSON_INVALID"
            ) from exc
    if not isinstance(value, Mapping):
        raise ExpiredV2ReconcileRefused(
            "ENVELOPE_PAYLOAD_NOT_MAPPING"
        )
    packet_json = value.get("packet_json")
    if not isinstance(packet_json, str):
        raise ExpiredV2ReconcileRefused("PACKET_JSON_MISSING")
    try:
        packet = json.loads(packet_json)
    except Exception as exc:
        raise ExpiredV2ReconcileRefused("PACKET_JSON_INVALID") from exc
    if not isinstance(packet, Mapping):
        raise ExpiredV2ReconcileRefused("PACKET_NOT_MAPPING")
    return dict(packet)


def _parse_deadline(value: Any) -> datetime:
    text = str(value or "").strip()
    if text != V2_EXPECTED_DEADLINE:
        raise ExpiredV2ReconcileRefused(
            "V2_DEADLINE_LINEAGE_MISMATCH"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExpiredV2ReconcileRefused("V2_DEADLINE_INVALID") from exc
    if parsed.tzinfo is None:
        raise ExpiredV2ReconcileRefused("V2_DEADLINE_NOT_UTC")
    return parsed.astimezone(timezone.utc)


def _validate_packet_contract(
    packet: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    if str(packet.get("task_id") or "") != V2_TASK_ID:
        raise ExpiredV2ReconcileRefused("PACKET_TASK_ID_MISMATCH")
    if str(packet.get("idempotency_key") or "") != V2_IDEMPOTENCY_KEY:
        raise ExpiredV2ReconcileRefused(
            "PACKET_IDEMPOTENCY_KEY_MISMATCH"
        )
    if str(packet.get("side_effect_policy") or "").strip().lower() != "none":
        raise ExpiredV2ReconcileRefused(
            "PACKET_SIDE_EFFECT_POLICY_NOT_NONE"
        )
    allowed = {
        str(item).strip().lower()
        for item in list(packet.get("allowed_operations") or [])
    }
    if allowed != {"analyze", "validate"}:
        raise ExpiredV2ReconcileRefused(
            "PACKET_ALLOWED_OPERATIONS_MISMATCH"
        )
    plan = [
        str(item).strip().lower()
        for item in list(packet.get("specialist_plan") or [])
    ]
    if plan != ["codex"]:
        raise ExpiredV2ReconcileRefused(
            "PACKET_SPECIALIST_PLAN_MISMATCH"
        )
    if int(packet.get("max_retries") or 0) != 0:
        raise ExpiredV2ReconcileRefused(
            "PACKET_RETRY_BUDGET_MISMATCH"
        )
    deadline = _parse_deadline(packet.get("deadline"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if deadline > current:
        raise ExpiredV2ReconcileRefused(
            "V2_DEADLINE_NOT_EXPIRED"
        )


def reconcile_connection(conn) -> dict[str, Any]:
    with conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (LOCK_KEY,),
        )
        cur.execute("SELECT current_database() AS db")
        db = str((cur.fetchone() or {}).get("db") or "")
        if db != PRODUCTION_DATABASE:
            raise ExpiredV2ReconcileRefused(
                "CONNECTED_DATABASE_NAME_MISMATCH:" + db
            )

        cur.execute(
            """
            SELECT *
            FROM jaytec_jobs
            WHERE job_id=%s AND task_id=%s
            FOR UPDATE
            """,
            (V2_JOB_ID, V2_TASK_ID),
        )
        v2 = cur.fetchone()
        if v2 is None:
            raise ExpiredV2ReconcileRefused("V2_JOB_NOT_FOUND")
        expected = {
            "status": "QUEUED",
            "fabric_state": "QUEUED",
            "health": "HEALTHY",
            "seat_id": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "ownership_epoch": 1,
            "fence_token": 1,
            "source_shared_state_version": EXPECTED_SOURCE_VERSION,
            "fabric_attempt_count": 0,
            "fabric_max_attempts": 1,
        }
        for field, expected_value in expected.items():
            if v2.get(field) != expected_value:
                raise ExpiredV2ReconcileRefused(
                    f"V2_FIELD_MISMATCH:{field}"
                )

        cur.execute(
            """
            SELECT worker_kind,authority_class,approval_required,
                   cost_policy,payload
            FROM jaytec_fabric_envelopes
            WHERE job_id=%s
            """,
            (V2_JOB_ID,),
        )
        envelope = cur.fetchone()
        if envelope is None:
            raise ExpiredV2ReconcileRefused(
                "V2_ENVELOPE_NOT_FOUND"
            )
        if str(envelope["worker_kind"]).upper() != "TASK_PACKET":
            raise ExpiredV2ReconcileRefused(
                "V2_WORKER_KIND_MISMATCH"
            )
        if str(envelope["authority_class"]).upper() != "READ_ONLY":
            raise ExpiredV2ReconcileRefused(
                "V2_AUTHORITY_CLASS_MISMATCH"
            )
        if bool(envelope["approval_required"]):
            raise ExpiredV2ReconcileRefused(
                "V2_UNEXPECTED_APPROVAL_REQUIRED"
            )
        policy = envelope["cost_policy"] or {}
        if isinstance(policy, str):
            policy = json.loads(policy)
        if not isinstance(policy, Mapping):
            raise ExpiredV2ReconcileRefused(
                "V2_COST_POLICY_INVALID"
            )
        if (
            str(policy.get("mode") or "").upper() != "ZERO_SPEND"
            or bool(policy.get("allow_paid"))
            or float(policy.get("max_cost_usd") or 0) != 0.0
            or str(policy.get("provider_mode") or "").upper() != "FREE_ONLY"
        ):
            raise ExpiredV2ReconcileRefused(
                "V2_COST_POLICY_MISMATCH"
            )
        _validate_packet_contract(
            _packet_from_envelope(envelope["payload"])
        )

        for table, error in (
            ("jaytec_operations", "V2_DURABLE_OPERATIONS_PRESENT"),
            ("jaytec_worker_handoffs", "V2_HANDOFF_PRESENT"),
            ("jaytec_watch_reviews", "V2_WATCH_REVIEW_PRESENT"),
        ):
            cur.execute(
                f"SELECT count(*)::int AS n FROM {table} WHERE job_id=%s",
                (V2_JOB_ID,),
            )
            if int((cur.fetchone() or {}).get("n") or 0) != 0:
                raise ExpiredV2ReconcileRefused(error)

        cur.execute(
            """
            SELECT status,fabric_state,health,ownership_epoch,fence_token
            FROM jaytec_jobs
            WHERE job_id=%s
            """,
            (V1_JOB_ID,),
        )
        v1 = cur.fetchone()
        if (
            v1 is None
            or str(v1["status"]) != "FAILED_SAFE"
            or str(v1["fabric_state"]) != "FAILED_SAFE"
            or str(v1["health"]) != "FAILED_SAFE"
            or int(v1["ownership_epoch"]) != 6
            or int(v1["fence_token"]) != 6
        ):
            raise ExpiredV2ReconcileRefused(
                "V1_RECONCILIATION_NOT_SETTLED"
            )

        cur.execute(
            """
            SELECT current_shared_state_version
            FROM jaytec_fabric_authority_state
            WHERE authority_id='FABRIC'
            """
        )
        authority = cur.fetchone()
        if (
            authority is None
            or int(authority["current_shared_state_version"])
            != EXPECTED_SOURCE_VERSION
        ):
            raise ExpiredV2ReconcileRefused(
                "AUTHORITY_VERSION_MISMATCH"
            )

        cur.execute(
            """
            SELECT job_id
            FROM jaytec_jobs
            WHERE assignment_type='FIVE_SEAT_FABRIC'
              AND status IN ('QUEUED','RUNNING','PAUSED')
            ORDER BY job_id
            FOR UPDATE
            """
        )
        active = [str(row["job_id"]) for row in cur.fetchall()]
        if active != [V2_JOB_ID]:
            raise ExpiredV2ReconcileRefused(
                "ACTIVE_FABRIC_SET_MISMATCH:" + ",".join(active)
            )

        cur.execute(
            """
            SELECT count(*)::int AS n
            FROM jaytec_worker_seats
            WHERE state <> 'FREE'
               OR current_job_id IS NOT NULL
               OR lease_owner IS NOT NULL
               OR lease_expires_at IS NOT NULL
            """
        )
        if int((cur.fetchone() or {}).get("n") or 0) != 0:
            raise ExpiredV2ReconcileRefused(
                "NONFREE_SEAT_PRESENT"
            )

        cur.execute(
            """
            SELECT state,consecutive_failures,failure_threshold,
                   open_until,probe_job_id
            FROM jaytec_fabric_circuits
            WHERE worker_kind='TASK_PACKET'
            """
        )
        circuit = cur.fetchone()
        if (
            circuit is None
            or str(circuit["state"]).upper() != "CLOSED"
            or circuit.get("open_until") is not None
            or circuit.get("probe_job_id") is not None
        ):
            raise ExpiredV2ReconcileRefused(
                "TASK_PACKET_CIRCUIT_NOT_CLOSED"
            )

        blocker = json.dumps(
            [
                {
                    "source": "FS08_EXPIRED_SYNTHETIC_PROBE_RECONCILIATION",
                    "reason": (
                        "synthetic read-only proof never claimed a seat; "
                        "stored deadline expired while fabric was safely parked"
                    ),
                    "previous_fabric_state": "QUEUED",
                    "deadline": V2_EXPECTED_DEADLINE,
                    "durable_operation_count": 0,
                    "attempt_count": 0,
                }
            ],
            sort_keys=True,
        )
        cur.execute(
            """
            UPDATE jaytec_jobs
            SET status='FAILED_SAFE',
                fabric_state='FAILED_SAFE',
                health='FAILED_SAFE',
                blockers=%s::jsonb,
                ownership_epoch=ownership_epoch+1,
                fence_token=fence_token+1,
                version=version+1,
                updated_at=now()
            WHERE job_id=%s
              AND task_id=%s
              AND status='QUEUED'
              AND fabric_state='QUEUED'
              AND health='HEALTHY'
              AND seat_id IS NULL
              AND lease_owner IS NULL
              AND lease_expires_at IS NULL
              AND ownership_epoch=1
              AND fence_token=1
              AND source_shared_state_version=%s
              AND fabric_attempt_count=0
              AND fabric_max_attempts=1
            RETURNING job_id,status,fabric_state,health,
                      ownership_epoch,fence_token
            """,
            (
                blocker,
                V2_JOB_ID,
                V2_TASK_ID,
                EXPECTED_SOURCE_VERSION,
            ),
        )
        updated = cur.fetchone()
        if updated is None:
            raise ExpiredV2ReconcileRefused(
                "V2_CHANGED_DURING_RECONCILIATION"
            )

        cur.execute(
            """
            INSERT INTO jaytec_job_events(
                job_id,event_type,source,source_version,payload
            ) VALUES (
                %s,'FS08_EXPIRED_SYNTHETIC_PROBE_RECLASSIFIED',
                'FS08_RECONCILIATION',%s,%s::jsonb
            )
            """,
            (
                V2_JOB_ID,
                EXPECTED_SOURCE_VERSION,
                json.dumps(
                    {
                        "previous_status": "QUEUED",
                        "previous_fabric_state": "QUEUED",
                        "new_status": "FAILED_SAFE",
                        "new_fabric_state": "FAILED_SAFE",
                        "deadline": V2_EXPECTED_DEADLINE,
                        "attempt_count": 0,
                        "seat_claimed": False,
                        "durable_operation_count": 0,
                    },
                    sort_keys=True,
                ),
            ),
        )

        cur.execute(
            """
            SELECT count(*)::int AS n
            FROM jaytec_jobs
            WHERE assignment_type='FIVE_SEAT_FABRIC'
              AND status IN ('QUEUED','RUNNING','PAUSED')
            """
        )
        remaining_active = int((cur.fetchone() or {}).get("n") or 0)
        if remaining_active != 0:
            raise ExpiredV2ReconcileRefused(
                "ACTIVE_FABRIC_REMAINS_AFTER_RECLASSIFY"
            )

    return {
        "status": "V2_EXPIRED_SYNTHETIC_PROBE_RECLASSIFIED",
        "job_id": str(updated["job_id"]),
        "new_status": str(updated["status"]),
        "new_fabric_state": str(updated["fabric_state"]),
        "new_health": str(updated["health"]),
        "new_ownership_epoch": int(updated["ownership_epoch"]),
        "new_fence_token": int(updated["fence_token"]),
        "deadline": V2_EXPECTED_DEADLINE,
        "attempt_count": 0,
        "seat_claimed": False,
        "durable_operation_count": 0,
        "active_fabric_jobs_after": 0,
    }


def reconcile_v2_expired(
    database_url: str,
    expected_host_sha256: str,
) -> dict[str, Any]:
    identity = assert_url_identity(
        database_url,
        expected_host_sha256,
    )
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
    result = reconcile_v2_expired(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_PROD_V2_EXPIRED_RECONCILE="
        + json.dumps(result, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
