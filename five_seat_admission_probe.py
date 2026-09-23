from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from five_seat_service import submit_low_risk_task_packet


PROBE_FLAG = "FIVE_SEAT_PROD_ADMISSION_PROBE"
PROBE_ID_ENV = "FIVE_SEAT_PROD_ADMISSION_PROBE_ID"
PROBE_TASK_ID = "FS08-NORMAL-ADMISSION-001"


def _enabled() -> bool:
    return os.environ.get(PROBE_FLAG, "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def build_probe_packet(probe_id: str) -> dict[str, Any]:
    probe_id = str(probe_id or "").strip()
    if not probe_id:
        raise ValueError("probe_id is required")
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    return {
        "packet_version": "1.0",
        "task_id": PROBE_TASK_ID,
        "subtask_id": "WATCH-ADMISSION-PROBE",
        "request": (
            "Independently review the supplied five-seat production activation "
            "facts. Return SUCCESS only if the facts support one bounded "
            "read-only admission with no side effects or paid fallback."
        ),
        "intent": "First bounded normal production admission proof",
        "workflow_id": "JAYTEC_V2_PRODUCTION_ADMISSION_PROBE",
        "risk_level": "low",
        "specialist_plan": ["codex"],
        "allowed_operations": ["read", "analyze", "validate"],
        "expected_output": (
            "One bounded JSON engineering review with explicit evidence and "
            "no requested side effects."
        ),
        "validation_requirements": [
            "whole packet SUCCESS",
            "zero side effects",
            "WATCH ACCEPT on the same production controller generation",
        ],
        "side_effect_policy": "none",
        "idempotency_key": probe_id,
        "deadline": deadline.isoformat().replace("+00:00", "Z"),
        "max_fanout": 1,
        "max_retries": 0,
        "return_schema_version": "1.0",
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "activation_scope": "ONE_READ_ONLY_JOB_ONLY",
        },
        "known_facts": [
            "The production fabric has exactly five configured worker seats.",
            "All five worker threads are alive and the seats were free before admission.",
            "The singleton WATCH controller is fenced and instance-owned.",
            "Migration and legacy-row reconciliation are disabled after completion.",
            "The admission path is zero-spend and disallows paid provider fallback.",
            "The task requests no mutation, deployment, credential, ROOT, Genesis, or Uren action.",
        ],
        "constraints": [
            "Do not request writes or follow-on execution.",
            "Do not widen scope.",
            "Do not recommend paid fallback.",
        ],
    }


def _safe_job_view(job: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(job or {})
    return {
        "job_id": row.get("job_id"),
        "task_id": row.get("task_id"),
        "status": row.get("status"),
        "fabric_state": row.get("fabric_state"),
        "seat_id": row.get("seat_id"),
        "watch_decision": row.get("watch_decision"),
        "watch_controller_owner": row.get("watch_controller_owner"),
        "watch_leader_epoch": row.get("watch_leader_epoch"),
        "watch_fence_generation": row.get("watch_fence_token"),
        "health": row.get("health"),
    }


def run_probe(runtime: Any, *, timeout_seconds: float = 180.0) -> dict[str, Any]:
    if not getattr(runtime, "fabric_enabled", False):
        return {"pass": False, "reason": "FABRIC_DISABLED"}
    if not runtime.fabric_queue or not runtime.fabric_authority or not runtime.fabric_service:
        return {"pass": False, "reason": "FABRIC_COMPONENTS_MISSING"}

    wait_deadline = time.time() + min(90.0, max(10.0, timeout_seconds / 2))
    leader = None
    while time.time() < wait_deadline:
        leader = runtime.fabric_service.watch_leader_snapshot()
        if leader:
            break
        time.sleep(0.25)
    if not leader:
        return {"pass": False, "reason": "LOCAL_WATCH_LEADER_NOT_OWNED"}

    authority = runtime.fabric_authority.current_state()
    source_version = int(authority.get("current_shared_state_version") or 0)
    if source_version <= 0:
        return {"pass": False, "reason": "FABRIC_AUTHORITY_UNINITIALIZED"}

    probe_id = os.environ.get(
        PROBE_ID_ENV,
        "fs08-normal-admission-20260923-v1",
    ).strip()
    packet = build_probe_packet(probe_id)
    snapshot = submit_low_risk_task_packet(
        runtime.fabric_queue,
        packet_json=json.dumps(packet, ensure_ascii=False, sort_keys=True),
        source_shared_state_version=source_version,
        priority=1,
    )
    job_id = str(snapshot.get("job_id") or "")
    print(
        "FIVE_SEAT_PROD_ADMISSION_SUBMITTED="
        + json.dumps(
            {
                "job_id": job_id,
                "task_id": PROBE_TASK_ID,
                "source_shared_state_version": source_version,
                "watch_owner": leader["owner"],
                "watch_leader_epoch": leader["leader_epoch"],
                "watch_fence_generation": leader["fence_token"],
            },
            sort_keys=True,
        ),
        flush=True,
    )

    deadline = time.time() + max(20.0, timeout_seconds)
    last = None
    terminal = {
        "SUCCEEDED",
        "QUARANTINED",
        "ESCALATED",
        "FAILED_SAFE",
        "BLOCKED_OWNER",
    }
    while time.time() < deadline:
        status = runtime.fabric_service.job_status(job_id)
        last = dict(status.get("job") or {})
        if str(last.get("fabric_state") or "") in terminal:
            break
        time.sleep(0.5)

    safe = _safe_job_view(last)
    passed = bool(
        safe.get("fabric_state") == "SUCCEEDED"
        and safe.get("watch_decision") == "ACCEPT"
        and safe.get("watch_controller_owner") == leader["owner"]
        and int(safe.get("watch_leader_epoch") or -1) == int(leader["leader_epoch"])
        and int(safe.get("watch_fence_generation") or -1) == int(leader["fence_token"])
    )
    result = {
        "pass": passed,
        "probe_id": probe_id,
        "job": safe,
        "reason": "WATCH_ACCEPT_SAME_GENERATION" if passed else "ADMISSION_PROBE_NOT_ACCEPTED",
    }
    print(
        "FIVE_SEAT_PROD_ADMISSION_RESULT="
        + json.dumps(result, sort_keys=True, default=str),
        flush=True,
    )
    return result


def start_probe(runtime: Any) -> threading.Thread | None:
    if not _enabled():
        return None

    def _target() -> None:
        try:
            result = run_probe(runtime)
            if not result.get("pass"):
                print(
                    "FIVE_SEAT_PROD_ADMISSION_PROBE_FAILED="
                    + json.dumps(result, sort_keys=True, default=str),
                    flush=True,
                )
        except Exception as exc:
            print(
                "FIVE_SEAT_PROD_ADMISSION_PROBE_FAILED="
                + json.dumps(
                    {
                        "pass": False,
                        "error_class": type(exc).__name__,
                        "detail": str(exc)[:300],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    thread = threading.Thread(
        target=_target,
        name="five-seat-production-admission-probe",
        daemon=True,
    )
    thread.start()
    return thread
