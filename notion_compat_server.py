from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Optional

import reliable_server
import server as legacy_server


RELIABILITY_STATUS_COMMAND = "JAYTEC_RELIABILITY_STATUS"
DURABLE_SUBMIT_PREFIX = "JAYTEC_SUBMIT_TASK_PACKET_DURABLE_JSON:"
TASK_PACKET_STATUS_PREFIX = "JAYTEC_TASK_PACKET_STATUS_JSON:"
GUARDIAN_PREFIX = "JAYTEC_RUN_GUARDIAN_LITE_JSON:"
RECORD_INCIDENT_PREFIX = "JAYTEC_RECORD_RELIABILITY_INCIDENT_JSON:"
DURABLE_WORKER_KICK_COMMAND = "JAYTEC_DURABLE_WORKER_KICK"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _payload_after_prefix(task: str, prefix: str) -> Mapping[str, Any]:
    raw = task[len(prefix) :].strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("compatibility command payload must be a JSON object")
    return value


def _build_legacy_command_adapter(
    original_command: Callable[[str, Any, Any], Optional[str]],
    state: Mapping[str, Any],
):
    queue = state.get("queue")
    worker = state.get("worker")
    guardian = state.get("guardian")
    guardian_loop = state.get("guardian_loop")

    def compatibility_command(task: str, status_fn: Any, packet_fn: Any) -> Optional[str]:
        legacy_result = original_command(task, status_fn, packet_fn)
        if legacy_result is not None:
            return legacy_result

        command = str(task or "").strip()
        try:
            if command == RELIABILITY_STATUS_COMMAND:
                if queue is None:
                    return _json({
                        "runtime_id": reliable_server.RUNTIME_ID,
                        "database_available": False,
                        "compatibility_surface": "collaborate",
                    })
                return _json({
                    "runtime_id": reliable_server.RUNTIME_ID,
                    "database_available": True,
                    "compatibility_surface": "collaborate",
                    "native_tools_may_require_notion_connection_refresh": True,
                    "preferred_specialist_execution": "submit_task_packet_durable -> task_packet_status",
                    "durable_worker_enabled": reliable_server.DURABLE_WORKER_ENABLED,
                    "durable_worker_alive": bool(worker and worker.alive),
                    "durable_worker_count": worker.worker_count if worker else 0,
                    "durable_worker_alive_count": worker.alive_count if worker else 0,
                    "durable_worker_has_lease_heartbeat": bool(worker),
                    "guardian_loop_enabled": reliable_server.GUARDIAN_LOOP_ENABLED,
                    "guardian_loop_alive": bool(guardian_loop and guardian_loop.alive),
                    "guardian_runtime": "ReliabilityGuardian" if guardian is not None else None,
                    "stats": queue.stats(),
                })

            if command.startswith(DURABLE_SUBMIT_PREFIX):
                if queue is None:
                    return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
                payload = _payload_after_prefix(command, DURABLE_SUBMIT_PREFIX)
                packet = payload.get("packet")
                if isinstance(packet, str):
                    packet_json = packet
                elif isinstance(packet, dict):
                    packet_json = _json(packet)
                else:
                    raise ValueError("packet must be a JSON object or encoded JSON string")
                snapshot = queue.submit(
                    packet_json,
                    source_shared_state_version=int(payload.get("source_shared_state_version") or 0),
                    priority=int(payload.get("priority", 100)),
                    source_execution_room_id=(
                        str(payload.get("execution_room_id"))
                        if payload.get("execution_room_id")
                        else None
                    ),
                )
                return _json({
                    "available": True,
                    "accepted": True,
                    "preferred_poll_command": TASK_PACKET_STATUS_PREFIX,
                    "snapshot": snapshot,
                })

            if command.startswith(TASK_PACKET_STATUS_PREFIX):
                if queue is None:
                    return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
                payload = _payload_after_prefix(command, TASK_PACKET_STATUS_PREFIX)
                snapshot = queue.status(
                    job_id=str(payload.get("job_id") or "") or None,
                    idempotency_key=str(payload.get("idempotency_key") or "") or None,
                )
                return _json({"available": True, "snapshot": snapshot})

            if command.startswith(GUARDIAN_PREFIX):
                if guardian is None:
                    return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
                payload = _payload_after_prefix(command, GUARDIAN_PREFIX)
                auto_repair = payload.get("auto_repair", True) is not False
                return _json({
                    "available": True,
                    "result": guardian.run(auto_repair=auto_repair),
                })

            if command.startswith(RECORD_INCIDENT_PREFIX):
                if queue is None:
                    return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
                payload = _payload_after_prefix(command, RECORD_INCIDENT_PREFIX)
                detail = payload.get("detail") or {}
                if not isinstance(detail, dict):
                    raise ValueError("detail must be a JSON object")
                event = queue.record_incident(
                    str(payload.get("event_type") or ""),
                    job_id=str(payload.get("job_id") or "") or None,
                    detail=detail,
                )
                return _json({"available": True, "event": event})

            if command == DURABLE_WORKER_KICK_COMMAND:
                if worker is None:
                    return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
                return _json({"available": True, "worked": worker.run_once()})
        except Exception as exc:
            return _json({
                "available": queue is not None,
                "accepted": False,
                "error_class": type(exc).__name__,
                "error": str(exc),
            })

        return None

    return compatibility_command


def create_mcp_app():
    mcp = reliable_server.create_mcp_app()
    state = getattr(mcp, "_jaytec_reliability", {})
    original_command = legacy_server._legacy_collaborate_command
    legacy_server._legacy_collaborate_command = _build_legacy_command_adapter(
        original_command,
        state,
    )
    return mcp


def main() -> None:
    mcp = create_mcp_app()
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=legacy_server.PORT,
        stateless_http=True,
        host_origin_protection=False,
    )


if __name__ == "__main__":
    main()
