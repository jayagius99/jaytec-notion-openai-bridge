from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import EXPECTED_HOST_SHA_ENV, PRODUCTION_DATABASE, ProductionMigrationRefused, assert_url_identity

RECONCILE_FLAG = "FIVE_SEAT_PROD_V3_QUARANTINE_RECONCILE"
LOCK_KEY = "JAYTEC_FS08_V3_SYNTHETIC_QUARANTINE_RECLASSIFY_V1"
JOB_ID = "fabric-b0dfc7bb5516013444ed9480"
TASK_ID = "FS08-PRODUCTION-ADMISSION-003"
EXPECTED_SOURCE_VERSION = 55
EXPECTED_DEADLINE = "2026-09-23T14:30:00Z"


class V3QuarantineReconcileRefused(RuntimeError):
    pass


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception as exc:
            raise V3QuarantineReconcileRefused(label + "_JSON_INVALID") from exc
    if not isinstance(value, Mapping):
        raise V3QuarantineReconcileRefused(label + "_NOT_MAPPING")
    return value


def _packet(payload: Any) -> Mapping[str, Any]:
    envelope = _mapping(payload, "ENVELOPE_PAYLOAD")
    raw = envelope.get("packet_json")
    if not isinstance(raw, str):
        raise V3QuarantineReconcileRefused("PACKET_JSON_MISSING")
    try:
        packet = json.loads(raw)
    except Exception as exc:
        raise V3QuarantineReconcileRefused("PACKET_JSON_INVALID") from exc
    return _mapping(packet, "PACKET")


def _validate_packet(packet: Mapping[str, Any]) -> None:
    if str(packet.get("task_id") or "") != TASK_ID:
        raise V3QuarantineReconcileRefused("PACKET_TASK_ID_MISMATCH")
    if str(packet.get("deadline") or "") != EXPECTED_DEADLINE:
        raise V3QuarantineReconcileRefused("PACKET_DEADLINE_MISMATCH")
    deadline = datetime.fromisoformat(EXPECTED_DEADLINE.replace("Z", "+00:00"))
    if deadline >= datetime.now(timezone.utc):
        raise V3QuarantineReconcileRefused("PACKET_DEADLINE_NOT_EXPIRED")
    if str(packet.get("side_effect_policy") or "").lower() != "none":
        raise V3QuarantineReconcileRefused("PACKET_SIDE_EFFECT_POLICY_MISMATCH")
    if {str(x).lower() for x in list(packet.get("allowed_operations") or [])} != {"analyze", "validate"}:
        raise V3QuarantineReconcileRefused("PACKET_ALLOWED_OPERATIONS_MISMATCH")
    if [str(x).lower() for x in list(packet.get("specialist_plan") or [])] != ["codex"]:
        raise V3QuarantineReconcileRefused("PACKET_SPECIALIST_PLAN_MISMATCH")
    if int(packet.get("max_retries") or 0) != 0:
        raise V3QuarantineReconcileRefused("PACKET_RETRY_BUDGET_MISMATCH")


def reconcile_connection(conn) -> dict[str, Any]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        cur.execute("SELECT current_database() AS db")
        if str((cur.fetchone() or {}).get("db") or "") != PRODUCTION_DATABASE:
            raise V3QuarantineReconcileRefused("DATABASE_MISMATCH")

        cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s AND task_id=%s FOR UPDATE", (JOB_ID, TASK_ID))
        job = cur.fetchone()
        if job is None:
            raise V3QuarantineReconcileRefused("JOB_NOT_FOUND")
        expected = {
            "status": "BLOCKED", "fabric_state": "QUARANTINED", "seat_id": None,
            "lease_owner": None, "lease_expires_at": None, "ownership_epoch": 3,
            "fence_token": 3, "source_shared_state_version": EXPECTED_SOURCE_VERSION,
            "fabric_attempt_count": 1, "fabric_max_attempts": 1,
        }
        for key, value in expected.items():
            if job.get(key) != value:
                raise V3QuarantineReconcileRefused("JOB_FIELD_MISMATCH:" + key)

        cur.execute("SELECT worker_kind,authority_class,approval_required,cost_policy,payload FROM jaytec_fabric_envelopes WHERE job_id=%s", (JOB_ID,))
        env = cur.fetchone()
        if env is None:
            raise V3QuarantineReconcileRefused("ENVELOPE_NOT_FOUND")
        if str(env["worker_kind"]).upper() != "TASK_PACKET" or str(env["authority_class"]).upper() != "READ_ONLY" or bool(env["approval_required"]):
            raise V3QuarantineReconcileRefused("ENVELOPE_AUTHORITY_MISMATCH")
        policy = _mapping(env["cost_policy"] or {}, "COST_POLICY")
        if str(policy.get("mode") or "").upper() != "ZERO_SPEND" or bool(policy.get("allow_paid")) or float(policy.get("max_cost_usd") or 0) != 0.0 or str(policy.get("provider_mode") or "").upper() != "FREE_ONLY":
            raise V3QuarantineReconcileRefused("COST_POLICY_MISMATCH")
        _validate_packet(_packet(env["payload"]))

        cur.execute("SELECT operation_id,status FROM jaytec_operations WHERE job_id=%s", (JOB_ID,))
        if cur.fetchall():
            raise V3QuarantineReconcileRefused("DURABLE_OPERATIONS_PRESENT")

        cur.execute("SELECT handoff_id,ownership_epoch,job_fence_token,payload FROM jaytec_worker_handoffs WHERE job_id=%s ORDER BY created_at", (JOB_ID,))
        handoffs = [dict(x) for x in cur.fetchall()]
        if len(handoffs) != 1:
            raise V3QuarantineReconcileRefused("HANDOFF_COUNT_MISMATCH")
        handoff = handoffs[0]
        if int(handoff["ownership_epoch"]) != 2 or int(handoff["job_fence_token"]) != 2:
            raise V3QuarantineReconcileRefused("HANDOFF_FENCE_MISMATCH")
        hp = _mapping(handoff["payload"] or {}, "HANDOFF_PAYLOAD")
        if str(hp.get("partial_side_effect_status") or "").upper() != "UNCERTAIN_PARTIAL":
            raise V3QuarantineReconcileRefused("HANDOFF_PARTIAL_MISMATCH")
        if hp.get("provider_identity") not in {None, ""} or list(hp.get("operations") or []) or list(hp.get("artifacts") or []):
            raise V3QuarantineReconcileRefused("HANDOFF_SIDE_EFFECT_EVIDENCE_PRESENT")
        if str(hp.get("worker_completion_classification") or "") != "QUARANTINE_CANDIDATE":
            raise V3QuarantineReconcileRefused("HANDOFF_CLASSIFICATION_MISMATCH")

        cur.execute("SELECT decision,reason,leader_epoch,fence_token FROM jaytec_watch_reviews WHERE job_id=%s ORDER BY created_at", (JOB_ID,))
        reviews = [dict(x) for x in cur.fetchall()]
        if len(reviews) != 1 or reviews[0]["decision"] != "BLOCK" or reviews[0]["reason"] != "uncertain side-effect state requires quarantine":
            raise V3QuarantineReconcileRefused("WATCH_REVIEW_MISMATCH")

        cur.execute("SELECT current_shared_state_version FROM jaytec_fabric_authority_state WHERE authority_id='FABRIC'")
        authority = cur.fetchone()
        if authority is None or int(authority["current_shared_state_version"]) != EXPECTED_SOURCE_VERSION:
            raise V3QuarantineReconcileRefused("AUTHORITY_VERSION_MISMATCH")
        cur.execute("SELECT count(*)::int AS n FROM jaytec_jobs WHERE assignment_type='FIVE_SEAT_FABRIC' AND status='RUNNING'")
        if int((cur.fetchone() or {}).get("n") or 0):
            raise V3QuarantineReconcileRefused("ACTIVE_FABRIC_JOB_PRESENT")
        cur.execute("SELECT count(*)::int AS n FROM jaytec_worker_seats WHERE state<>'FREE' OR current_job_id IS NOT NULL OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL")
        if int((cur.fetchone() or {}).get("n") or 0):
            raise V3QuarantineReconcileRefused("NONFREE_SEAT_PRESENT")
        cur.execute("SELECT job_id FROM jaytec_jobs WHERE fabric_state='QUARANTINED' ORDER BY job_id")
        quarantined = [str(x["job_id"]) for x in cur.fetchall()]
        if quarantined != [JOB_ID]:
            raise V3QuarantineReconcileRefused("QUARANTINE_SET_MISMATCH:" + ",".join(quarantined))

        blocker = json.dumps([{
            "source": "FS08_V3_SYNTHETIC_QUARANTINE_RECONCILIATION",
            "reason": "synthetic read-only probe hit the pre-fix JSON-string adapter boundary; zero durable operations/provider identity; immutable WATCH BLOCK and handoff retained",
            "previous_fabric_state": "QUARANTINED", "watch_decision": "BLOCK",
            "side_effect_policy": "none", "durable_operation_count": 0,
        }], sort_keys=True)
        cur.execute("""
            UPDATE jaytec_jobs
            SET status='FAILED_SAFE',fabric_state='FAILED_SAFE',health='FAILED_SAFE',
                blockers=%s::jsonb,ownership_epoch=ownership_epoch+1,
                fence_token=fence_token+1,version=version+1,updated_at=now()
            WHERE job_id=%s AND task_id=%s AND status='BLOCKED'
              AND fabric_state='QUARANTINED' AND seat_id IS NULL
              AND lease_owner IS NULL AND lease_expires_at IS NULL
              AND ownership_epoch=3 AND fence_token=3
              AND source_shared_state_version=%s
              AND fabric_attempt_count=1 AND fabric_max_attempts=1
            RETURNING job_id,status,fabric_state,health,ownership_epoch,fence_token
        """, (blocker, JOB_ID, TASK_ID, EXPECTED_SOURCE_VERSION))
        updated = cur.fetchone()
        if updated is None:
            raise V3QuarantineReconcileRefused("JOB_CHANGED_DURING_RECONCILIATION")
        cur.execute("""
            INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
            VALUES (%s,'FS08_V3_SYNTHETIC_QUARANTINE_RECLASSIFIED','FS08_RECONCILIATION',%s,%s::jsonb)
        """, (JOB_ID, EXPECTED_SOURCE_VERSION, json.dumps({
            "previous_status":"BLOCKED","previous_fabric_state":"QUARANTINED",
            "new_status":"FAILED_SAFE","new_fabric_state":"FAILED_SAFE",
            "durable_operation_count":0,"watch_decision_preserved":"BLOCK",
            "handoff_preserved":str(handoff["handoff_id"]),
        }, sort_keys=True)))
    return {
        "status":"V3_SYNTHETIC_QUARANTINE_RECLASSIFIED","job_id":JOB_ID,
        "new_status":str(updated["status"]),"new_fabric_state":str(updated["fabric_state"]),
        "new_health":str(updated["health"]),"new_ownership_epoch":int(updated["ownership_epoch"]),
        "new_fence_token":int(updated["fence_token"]),"watch_block_preserved":True,
        "handoff_preserved":True,"durable_operation_count":0,
    }


def reconcile_v3_quarantine(database_url: str, expected_host_sha256: str) -> dict[str, Any]:
    identity = assert_url_identity(database_url, expected_host_sha256)
    with psycopg2.connect(database_url) as conn:
        result = reconcile_connection(conn)
    return {**result, "database": PRODUCTION_DATABASE, "host_sha256": identity["host_sha256"]}


def main() -> None:
    if os.environ.get(RECONCILE_FLAG, "0").strip() != "1":
        return
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")
    result = reconcile_v3_quarantine(database_url, os.environ.get(EXPECTED_HOST_SHA_ENV, ""))
    print("FIVE_SEAT_PROD_V3_QUARANTINE_RECONCILE=" + json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
