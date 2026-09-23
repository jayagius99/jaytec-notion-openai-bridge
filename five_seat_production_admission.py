from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

import psycopg2
import psycopg2.extras

from five_seat_service import submit_low_risk_task_packet
from specialist_adapters import EXPECTED_CODEX_MODEL


PROBE_TASK_ID = "FS08-PRODUCTION-ADMISSION-003"
PROBE_SUBTASK_ID = "NORMAL-LOW-RISK-001"
PROBE_WORKFLOW_ID = "JAYTEC_ENGINEERING_FS08_PRODUCTION_ADMISSION_V1"
PROBE_IDEMPOTENCY_KEY = "fs08-production-admission-v3"
PROBE_SCHEMA = "JAYTEC_FS08_PRODUCTION_ADMISSION_PROBE_V1"

_TERMINAL_FAILURE_STATES = {
    "QUARANTINED",
    "ESCALATED",
    "FAILED_SAFE",
    "BLOCKED_OWNER",
    "BLOCKED_DEPENDENCY",
}


def build_probe_packet(*, deadline: str) -> dict[str, Any]:
    deadline = str(deadline or "").strip()
    if not deadline:
        raise ValueError("probe deadline is required")
    return {
        "packet_version": "1.0",
        "task_id": PROBE_TASK_ID,
        "subtask_id": PROBE_SUBTASK_ID,
        "request": (
            "Validate the literal marker FS08_PRODUCTION_ADMISSION_V1. "
            "Use no external data and perform no side effects. "
            "Return SUCCESS only if the packet is internally consistent."
        ),
        "intent": (
            "Bounded production admission proof through the normal JAYTEC "
            "five-seat TaskPacket path."
        ),
        "workflow_id": PROBE_WORKFLOW_ID,
        "risk_level": "low",
        "specialist_plan": ["codex"],
        "allowed_operations": ["analyze", "validate"],
        "expected_output": (
            "One bounded engineering result confirming the literal marker "
            "with no requested operations and no side effects."
        ),
        "validation_requirements": [
            "exact_free_primary_model",
            "no_side_effects",
            "watch_accept",
        ],
        "side_effect_policy": "none",
        "idempotency_key": PROBE_IDEMPOTENCY_KEY,
        "deadline": deadline,
        "max_fanout": 1,
        "max_retries": 0,
        "return_schema_version": "1.0",
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "probe_marker": "FS08_PRODUCTION_ADMISSION_V1",
        },
        "constraints": [
            "No external data access.",
            "No requested operations.",
            "No side effects.",
            "No fallback model or provider.",
        ],
    }


def _latest_handoff_evidence(
    database_url: str,
    job_id: str,
) -> dict[str, Any]:
    with psycopg2.connect(database_url) as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT h.payload,e.cost_policy
                FROM jaytec_worker_handoffs h
                JOIN jaytec_fabric_envelopes e ON e.job_id=h.job_id
                WHERE h.job_id=%s
                ORDER BY h.created_at DESC
                LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if not row:
        return {}
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    policy = row.get("cost_policy") or {}
    if isinstance(policy, str):
        policy = json.loads(policy)
    return {
        "provider_identity": str(payload.get("provider_identity") or ""),
        "partial_side_effect_status": str(
            payload.get("partial_side_effect_status") or ""
        ),
        "cost_policy": dict(policy) if isinstance(policy, Mapping) else {},
    }


def _seat_evidence(report: Mapping[str, Any], job_id: str) -> dict[str, Any]:
    touched: list[str] = []
    claims = 0
    releases = 0
    activity = report.get("seat_activity") or {}
    if isinstance(activity, Mapping):
        for seat_id, raw in activity.items():
            item = raw if isinstance(raw, Mapping) else {}
            jobs = [str(value) for value in list(item.get("jobs") or [])]
            if job_id not in jobs:
                continue
            touched.append(str(seat_id))
            claims += int(item.get("claims") or 0)
            releases += int(item.get("releases") or 0)
    return {
        "seats_touched": sorted(touched),
        "claim_count": claims,
        "release_count": releases,
    }


def _recovery_count(report: Mapping[str, Any], job_id: str) -> int:
    events = report.get("recovery_and_safety_events") or []
    return sum(
        1
        for event in events
        if isinstance(event, Mapping)
        and str(event.get("job_id") or "") == job_id
    )


def wait_for_local_watch(
    *,
    reporter: Any,
    expected_leader: str,
    timeout_seconds: float = 90.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    expected = str(expected_leader or "").strip()
    if not expected:
        raise ValueError("expected_leader is required")
    stop_at = monotonic_fn() + max(1.0, float(timeout_seconds))
    last_watch: dict[str, Any] = {}
    while True:
        report = reporter.last_60_minutes(window_minutes=60)
        watch = dict(report.get("watch") or {})
        last_watch = watch
        if bool(watch.get("healthy")) and str(watch.get("leader") or "") == expected:
            return watch
        if monotonic_fn() >= stop_at:
            raise RuntimeError("admission_probe_local_watch_not_ready")
        sleep_fn(0.25)


def run_probe(
    *,
    database_url: str,
    queue: Any,
    authority: Any,
    service: Any,
    reporter: Any,
    deadline: str,
    timeout_seconds: float = 120.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    packet = build_probe_packet(deadline=deadline)
    authority_state = authority.current_state()
    source_version = int(
        authority_state.get("current_shared_state_version") or 0
    )
    if source_version <= 0:
        raise RuntimeError("fabric_authority_state_uninitialized")

    snapshot = submit_low_risk_task_packet(
        queue,
        packet_json=json.dumps(packet, ensure_ascii=False, sort_keys=True),
        source_shared_state_version=source_version,
        priority=1,
    )
    job_id = str(snapshot.get("job_id") or "")
    if not job_id:
        raise RuntimeError("admission_probe_job_id_missing")

    stop_at = monotonic_fn() + max(1.0, float(timeout_seconds))
    observed: dict[str, Any] = {}
    while True:
        status = service.job_status(job_id)
        observed = dict(status.get("job") or {})
        fabric_state = str(observed.get("fabric_state") or "")
        watch_decision = str(observed.get("watch_decision") or "")
        if fabric_state == "SUCCEEDED" and watch_decision == "ACCEPT":
            break
        if fabric_state in _TERMINAL_FAILURE_STATES:
            break
        if monotonic_fn() >= stop_at:
            break
        sleep_fn(0.25)

    report = reporter.last_60_minutes(window_minutes=60)
    handoff = _latest_handoff_evidence(database_url, job_id)
    seat = _seat_evidence(report, job_id)
    recovery_count = _recovery_count(report, job_id)
    summary = report.get("summary") or {}
    watch = report.get("watch") or {}
    cost_policy = handoff.get("cost_policy") or {}

    fabric_state = str(observed.get("fabric_state") or "")
    watch_decision = str(observed.get("watch_decision") or "")
    provider_identity = str(handoff.get("provider_identity") or "")
    partial = str(handoff.get("partial_side_effect_status") or "")
    zero_spend_policy = (
        str(cost_policy.get("mode") or "").upper() == "ZERO_SPEND"
        and cost_policy.get("allow_paid") is False
        and float(cost_policy.get("max_cost_usd") or 0) == 0.0
        and str(cost_policy.get("provider_mode") or "").upper() == "FREE_ONLY"
    )

    passed = bool(
        fabric_state == "SUCCEEDED"
        and watch_decision == "ACCEPT"
        and provider_identity == EXPECTED_CODEX_MODEL
        and partial == "NONE"
        and zero_spend_policy
        and len(seat["seats_touched"]) == 1
        and int(seat["claim_count"]) >= 1
        and int(seat["release_count"]) >= 1
        and recovery_count == 0
        and bool(watch.get("healthy"))
        and int(summary.get("seats_total") or 0) == 5
        and int(summary.get("seats_free") or 0) == 5
    )

    return {
        "schema_version": PROBE_SCHEMA,
        "passed": passed,
        "task_id": PROBE_TASK_ID,
        "job_id": job_id,
        "fabric_state": fabric_state,
        "watch_decision": watch_decision or None,
        "expected_provider": EXPECTED_CODEX_MODEL,
        "provider_identity": provider_identity or None,
        "zero_spend_policy": zero_spend_policy,
        "paid_fallback_allowed": bool(cost_policy.get("allow_paid")),
        "partial_side_effect_status": partial or None,
        "seats_touched": seat["seats_touched"],
        "claim_count": int(seat["claim_count"]),
        "release_count": int(seat["release_count"]),
        "recovery_event_count": recovery_count,
        "watch_healthy": bool(watch.get("healthy")),
        "seats_free_after": int(summary.get("seats_free") or 0),
        "seats_total": int(summary.get("seats_total") or 0),
    }
