from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

import uvicorn
from starlette.middleware import Middleware

import reliable_server
import server as legacy_server


COMPAT_RUNTIME_ID = "JAYTEC_RELIABILITY_LEGACY_CATALOG_COMPAT_V2"
COMPAT_MAX_BODY_BYTES = 1_048_576
MCP_COMPAT_PATHS = frozenset({"/mcp", "/mcp/"})
RELIABILITY_STATUS_COMMAND = "JAYTEC_RELIABILITY_STATUS"
WORKLOAD_STATUS_COMMAND = "JAYTEC_WORKLOAD_STATUS"
RUN_GUARDIAN_PREFIX = "JAYTEC_RELIABILITY_RUN_GUARDIAN_JSON:"
DURABLE_SUBMIT_PREFIX = "JAYTEC_DURABLE_SUBMIT_JSON:"
DURABLE_STATUS_PREFIX = "JAYTEC_DURABLE_STATUS_JSON:"
DURABLE_WORKER_KICK_COMMAND = "JAYTEC_DURABLE_WORKER_KICK"
RECORD_INCIDENT_PREFIX = "JAYTEC_RECORD_RELIABILITY_INCIDENT_JSON:"
RESERVED_PREFIXES = (
    "JAYTEC_RELIABILITY_",
    "JAYTEC_DURABLE_",
    "JAYTEC_RECORD_RELIABILITY_",
    "JAYTEC_WORKLOAD_",
)
REJECTED_TOOL_NAME = "__jaytec_compat_command_rejected__"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _payload(task: str, prefix: str) -> Mapping[str, Any]:
    raw = task[len(prefix) :].strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("compatibility command payload must encode a JSON object")
    return value


def _strict_bool(value: Any, *, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError(f"{field} must be a JSON boolean")
    return value


def _strict_int(value: Any, *, field: str, default: int) -> int:
    if value is None:
        return default
    if type(value) is not int:
        raise ValueError(f"{field} must be a JSON integer")
    return value


def _strict_string(value: Any, *, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _compat_target(task: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Map reserved legacy collaborate commands to native reliability tools.

    None means the task is not a compatibility command and must pass through
    unchanged to the original collaborate implementation.
    """

    if task == RELIABILITY_STATUS_COMMAND:
        return "reliability_status", {}
    if task == WORKLOAD_STATUS_COMMAND:
        return "workload_snapshot", {}

    if task.startswith(RUN_GUARDIAN_PREFIX):
        body = _payload(task, RUN_GUARDIAN_PREFIX)
        return "run_guardian_lite", {
            # Compatibility calls default read-only even though the native
            # diagnostic tool supports explicit repair when directly invoked.
            "auto_repair": _strict_bool(
                body.get("auto_repair"), field="auto_repair", default=False
            )
        }

    if task.startswith(DURABLE_SUBMIT_PREFIX):
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
        return "submit_task_packet_durable", {
            "packet_json": packet_json,
            "source_shared_state_version": _strict_int(
                body.get("source_shared_state_version"),
                field="source_shared_state_version",
                default=0,
            ),
            "priority": _strict_int(body.get("priority"), field="priority", default=100),
            "execution_room_id": _strict_string(
                body.get("execution_room_id"), field="execution_room_id", default=""
            ).strip(),
        }

    if task.startswith(DURABLE_STATUS_PREFIX):
        body = _payload(task, DURABLE_STATUS_PREFIX)
        job_id = _strict_string(body.get("job_id"), field="job_id", default="").strip()
        idempotency_key = _strict_string(
            body.get("idempotency_key"), field="idempotency_key", default=""
        ).strip()
        if not job_id and not idempotency_key:
            raise ValueError("job_id or idempotency_key is required")
        return "task_packet_status", {
            "job_id": job_id,
            "idempotency_key": idempotency_key,
        }

    if task == DURABLE_WORKER_KICK_COMMAND:
        return "durable_worker_kick", {}

    if task.startswith(RECORD_INCIDENT_PREFIX):
        body = _payload(task, RECORD_INCIDENT_PREFIX)
        event_type = _strict_string(
            body.get("event_type"), field="event_type", default=""
        ).strip()
        if not event_type:
            raise ValueError("event_type is required")
        job_id = _strict_string(body.get("job_id"), field="job_id", default="").strip()
        detail = body.get("detail", {})
        if not isinstance(detail, Mapping):
            raise ValueError("detail must be a JSON object")
        return "record_reliability_incident", {
            "event_type": event_type,
            "detail_json": _json(dict(detail)),
            "job_id": job_id,
        }

    if task.startswith(RESERVED_PREFIXES):
        raise ValueError("unknown reserved compatibility command")

    return None


def _rewrite_call(payload: Mapping[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(dict(payload))
    if value.get("method") != "tools/call":
        return value

    params = value.get("params")
    if not isinstance(params, dict) or params.get("name") != "collaborate":
        return value

    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return value
    task = arguments.get("task")
    if not isinstance(task, str):
        return value

    try:
        target = _compat_target(task)
    except Exception:
        params["name"] = REJECTED_TOOL_NAME
        params["arguments"] = {}
        return value

    if target is None:
        return value

    tool_name, tool_arguments = target
    params["name"] = tool_name
    params["arguments"] = tool_arguments
    return value


def rewrite_jsonrpc_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return _rewrite_call(payload)
    if isinstance(payload, list):
        return [
            _rewrite_call(item) if isinstance(item, dict) else copy.deepcopy(item)
            for item in payload
        ]
    return payload


def rewrite_jsonrpc_body(body: bytes) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    rewritten = rewrite_jsonrpc_payload(payload)
    if rewritten == payload:
        return body
    return _json(rewritten).encode("utf-8")


def _header(scope: Mapping[str, Any], name: bytes) -> Optional[bytes]:
    target = name.lower()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value
    return None


def _is_json_content_type(scope: Mapping[str, Any]) -> bool:
    value = _header(scope, b"content-type")
    if value is None:
        return False
    return value.split(b";", 1)[0].strip().lower() == b"application/json"


class LegacyCatalogCompatMiddleware:
    """Per-process ASGI compatibility shim for stale MCP client catalogs.

    The shim rewrites only reserved calls made through the already-cached
    `collaborate` tool at the expected MCP JSON endpoint. It never mutates
    server.py/reliable_server.py functions, process-global dispatchers,
    provider state, or the FastMCP tool registry.
    """

    def __init__(self, app, *, max_body_bytes: int = COMPAT_MAX_BODY_BYTES):
        self.app = app
        if type(max_body_bytes) is not int or max_body_bytes < 1024:
            raise ValueError("max_body_bytes must be an integer >= 1024")
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in MCP_COMPAT_PATHS
            or not _is_json_content_type(scope)
        ):
            return await self.app(scope, receive, send)

        declared_length = _header(scope, b"content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > self.max_body_bytes:
                    return await self._reject_oversized(send)
            except (TypeError, ValueError):
                # Let the native HTTP stack handle malformed length syntax;
                # the incremental cap below still limits memory consumption.
                pass

        received: List[Dict[str, Any]] = []
        total_bytes = 0
        while True:
            message = await receive()
            received.append(message)
            if message.get("type") == "http.request":
                total_bytes += len(message.get("body", b""))
                if total_bytes > self.max_body_bytes:
                    return await self._reject_oversized(send)
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break

        request_messages = [m for m in received if m.get("type") == "http.request"]
        if not request_messages:
            return await self._replay(scope, received, receive, send)

        body = b"".join(m.get("body", b"") for m in request_messages)
        rewritten = rewrite_jsonrpc_body(body)
        if rewritten == body:
            return await self._replay(scope, received, receive, send)

        new_scope = dict(scope)
        headers = []
        for key, value in scope.get("headers", []):
            if key.lower() != b"content-length":
                headers.append((key, value))
        headers.append((b"content-length", str(len(rewritten)).encode("ascii")))
        new_scope["headers"] = headers

        delivered = False

        async def rewritten_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": rewritten, "more_body": False}
            return await receive()

        return await self.app(new_scope, rewritten_receive, send)

    async def _reject_oversized(self, send):
        body = b"request body too large"
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _replay(self, scope, messages, receive, send):
        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        return await self.app(scope, replay_receive, send)


def create_mcp_app():
    """Return the unchanged reliable FastMCP server for catalog/native tests."""
    return reliable_server.create_mcp_app()


def create_http_app(mcp=None):
    """Wrap the reliable HTTP app while preserving production transport settings."""
    server = mcp or create_mcp_app()
    return server.http_app(
        middleware=[Middleware(LegacyCatalogCompatMiddleware)],
        stateless_http=True,
        host_origin_protection=False,
    )


def main() -> None:
    uvicorn.run(
        create_http_app(),
        host="0.0.0.0",
        port=legacy_server.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
