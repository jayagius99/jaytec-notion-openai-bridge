from __future__ import annotations

import json
from typing import Any, Mapping, Optional

import reliable_server
import server as legacy_server


COMPAT_RUNTIME_ID = "JAYTEC_RELIABILITY_LEGACY_CATALOG_COMPAT_V1"
RELIABILITY_STATUS_COMMAND = "JAYTEC_RELIABILITY_STATUS"
RUN_GUARDIAN_PREFIX = "JAYTEC_RELIABILITY_RUN_GUARDIAN_JSON:"
DURABLE_SUBMIT_PREFIX = "JAYTEC_DURABLE_SUBMIT_JSON:"
DURABLE_STATUS_PREFIX = "JAYTEC_DURABLE_STATUS_JSON:"
DURABLE_WORKER_KICK_COMMAND = "JAYTEC_DURABLE_WORKER_KICK"
RECORD_INCIDENT_PREFIX = "JAYTEC_RECORD_RELIABILITY_INCIDENT_JSON:"

# Preserve the original legacy command router exactly once so unknown commands
# still behave exactly as server.py defined them, even if this module is reloaded.
_ORIGINAL_LEGACY_COMMAND = getattr(
    legacy_server,
    "_jaytec_original_legacy_collaborate_command",
    legacy_server._legacy_collaborate_command,
)
setattr(
    legacy_server,
    "_jaytec_original_legacy_collaborate_command",
    _ORIGINAL_LEGACY_COMMAND,
)

_APP = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _runtime() -> Optional[Mapping[str, Any]]:
    if _APP is None:
        return None
    value = getattr(_APP, "_jaytec_reliability", None)
    return value if isinstance(value, Mapping) else None


def _payload(task: str, prefix: str) -> Mapping[str, Any]:
    raw = task[len(prefix) :].strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("compatibility command payload must encode a JSON object")
    return value


def _strict_bool(value: Any, *, field: str, default: bool = False) -> bool:
    """Accept only a real JSON boolean for safety-sensitive compatibility flags."""
    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError(f"{field} must be a JSON boolean")
    return value


def _strict_int(value: Any, *, field: str, default: int) -> int:
    """Reject bool/string coercion for control-plane numeric fields."""
    if value is None:
        return default
    if type(value) is not int:
        raise ValueError(f"{field} must be a JSON integer")
    return value


def _runtime_unavailable() -> str:
    return _json(
        {
            "available": False,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "reason": "RELIABILITY_RUNTIME_NOT_READY",
        }
    )


def _reliability_status() -> str:
    runtime = _runtime()
    if not runtime or runtime.get("queue") is None:
        return _runtime_unavailable()

    queue = runtime["queue"]
    worker = runtime.get("worker")
    guardian = runtime.get("guardian")
    guardian_loop = runtime.get("guardian_loop")
    return _json(
        {
            "available": True,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "runtime_id": reliable_server.RUNTIME_ID,
            "database_available": True,
            "preferred_specialist_execution": "durable_queue_via_legacy_collaborate_compat",
            "durable_worker_enabled": reliable_server.DURABLE_WORKER_ENABLED,
            "durable_worker_alive": bool(worker and worker.alive),
            "durable_worker_count": worker.worker_count if worker else 0,
            "durable_worker_alive_count": worker.alive_count if worker else 0,
            "durable_worker_has_lease_heartbeat": bool(worker),
            "guardian_available": guardian is not None,
            "guardian_loop_enabled": reliable_server.GUARDIAN_LOOP_ENABLED,
            "guardian_loop_alive": bool(guardian_loop and guardian_loop.alive),
            "stats": queue.stats(),
        }
    )


def _run_guardian(task: str) -> str:
    runtime = _runtime()
    if not runtime or runtime.get("guardian") is None:
        return _runtime_unavailable()
    body = _payload(task, RUN_GUARDIAN_PREFIX)
    auto_repair = _strict_bool(
        body.get("auto_repair"),
        field="auto_repair",
        default=False,
    )
    result = runtime["guardian"].run(auto_repair=auto_repair)
    return _json(
        {
            "available": True,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "auto_repair": auto_repair,
            "result": result,
        }
    )


def _submit_durable(task: str) -> str:
    runtime = _runtime()
    if not runtime or runtime.get("queue") is None:
        return _runtime_unavailable()
    body = _payload(task, DURABLE_SUBMIT_PREFIX)

    packet = body.get("packet")
    packet_json = body.get("packet_json")
    if packet is not None:
        if not isinstance(packet, Mapping):
            raise ValueError("packet must be a JSON object")
        if packet_json is not None:
            raise ValueError("provide packet or packet_json, not both")
        packet_json = _json(dict(packet))
    if not isinstance(packet_json, str) or not packet_json.strip():
        raise ValueError("packet or packet_json is required")

    source_version = _strict_int(
        body.get("source_shared_state_version"),
        field="source_shared_state_version",
        default=0,
    )
    priority = _strict_int(body.get("priority"), field="priority", default=100)
    room_value = body.get("execution_room_id", "")
    if not isinstance(room_value, str):
        raise ValueError("execution_room_id must be a string")
    room = room_value.strip()
    snapshot = runtime["queue"].submit(
        packet_json,
        source_shared_state_version=source_version,
        priority=priority,
        source_execution_room_id=room or None,
    )
    return _json(
        {
            "available": True,
            "accepted": True,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "preferred_status_command": DURABLE_STATUS_PREFIX,
            "snapshot": snapshot,
        }
    )


def _durable_status(task: str) -> str:
    runtime = _runtime()
    if not runtime or runtime.get("queue") is None:
        return _runtime_unavailable()
    body = _payload(task, DURABLE_STATUS_PREFIX)
    job_id_value = body.get("job_id", "")
    idempotency_value = body.get("idempotency_key", "")
    if not isinstance(job_id_value, str) or not isinstance(idempotency_value, str):
        raise ValueError("job_id and idempotency_key must be strings")
    job_id = job_id_value.strip()
    idempotency_key = idempotency_value.strip()
    if not job_id and not idempotency_key:
        raise ValueError("job_id or idempotency_key is required")
    snapshot = runtime["queue"].status(
        job_id=job_id or None,
        idempotency_key=idempotency_key or None,
    )
    return _json(
        {
            "available": True,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "snapshot": snapshot,
        }
    )


def _record_incident(task: str) -> str:
    runtime = _runtime()
    if not runtime or runtime.get("queue") is None:
        return _runtime_unavailable()
    body = _payload(task, RECORD_INCIDENT_PREFIX)
    event_type_value = body.get("event_type", "")
    job_id_value = body.get("job_id", "")
    if not isinstance(event_type_value, str) or not isinstance(job_id_value, str):
        raise ValueError("event_type and job_id must be strings")
    event_type = event_type_value.strip()
    if not event_type:
        raise ValueError("event_type is required")
    detail = body.get("detail", {})
    if not isinstance(detail, Mapping):
        raise ValueError("detail must be a JSON object")
    job_id = job_id_value.strip()
    event = runtime["queue"].record_incident(
        event_type,
        job_id=job_id or None,
        detail=dict(detail),
    )
    return _json(
        {
            "available": True,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "event": event,
        }
    )


def _worker_kick() -> str:
    runtime = _runtime()
    if not runtime or runtime.get("worker") is None:
        return _runtime_unavailable()
    worked = runtime["worker"].run_once()
    return _json(
        {
            "available": True,
            "compat_runtime_id": COMPAT_RUNTIME_ID,
            "worked": bool(worked),
        }
    )


def _compat_legacy_command(task: str, status_fn: Any, packet_fn: Any) -> Optional[str]:
    try:
        if task == RELIABILITY_STATUS_COMMAND:
            return _reliability_status()
        if task.startswith(RUN_GUARDIAN_PREFIX):
            return _run_guardian(task)
        if task.startswith(DURABLE_SUBMIT_PREFIX):
            return _submit_durable(task)
        if task.startswith(DURABLE_STATUS_PREFIX):
            return _durable_status(task)
        if task == DURABLE_WORKER_KICK_COMMAND:
            return _worker_kick()
        if task.startswith(RECORD_INCIDENT_PREFIX):
            return _record_incident(task)

        # Reserved compatibility namespaces fail closed rather than being sent
        # to the general OpenAI collaborate prompt because of a typo.
        if task.startswith(("JAYTEC_RELIABILITY_", "JAYTEC_DURABLE_", "JAYTEC_RECORD_RELIABILITY_")):
            return _json(
                {
                    "available": False,
                    "compat_runtime_id": COMPAT_RUNTIME_ID,
                    "error_class": "UNKNOWN_COMPAT_COMMAND",
                    "error": task.split(":", 1)[0],
                }
            )
    except Exception as exc:
        return _json(
            {
                "available": True,
                "compat_runtime_id": COMPAT_RUNTIME_ID,
                "accepted": False,
                "error_class": type(exc).__name__,
                "error": str(exc),
            }
        )

    return _ORIGINAL_LEGACY_COMMAND(task, status_fn, packet_fn)


def create_mcp_app():
    """Create the reliability runtime while preserving cached legacy tool callers."""
    global _APP
    legacy_server._legacy_collaborate_command = _compat_legacy_command
    app = reliable_server.create_mcp_app()
    _APP = app
    return app


def main() -> None:
    app = create_mcp_app()
    app.run(
        transport="http",
        host="0.0.0.0",
        port=legacy_server.PORT,
        stateless_http=True,
        host_origin_protection=False,
    )


if __name__ == "__main__":
    main()
