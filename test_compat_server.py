import asyncio
import json
import os
import subprocess
import sys
import unittest

import compat_server


def _rpc(task, *, name="collaborate", request_id=1):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": {"task": task, "notion_analysis": "", "context": ""},
        },
    }


class _CaptureApp:
    def __init__(self):
        self.body = None
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        chunks = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.body = b"".join(chunks)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _run_middleware(payload):
    capture = _CaptureApp()
    middleware = compat_server.LegacyCatalogCompatMiddleware(capture)
    body = json.dumps(payload).encode("utf-8")
    messages = [
        {"type": "http.request", "body": body[: max(1, len(body) // 2)], "more_body": True},
        {"type": "http.request", "body": body[max(1, len(body) // 2) :], "more_body": False},
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    }
    await middleware(scope, receive, send)
    return capture, sent


class TestCompatRewrite(unittest.TestCase):
    def test_ordinary_collaborate_is_unchanged(self):
        payload = _rpc("ordinary engineering question")
        self.assertEqual(compat_server.rewrite_jsonrpc_payload(payload), payload)

    def test_non_collaborate_tool_is_unchanged(self):
        payload = _rpc(compat_server.RELIABILITY_STATUS_COMMAND, name="bridge_status")
        self.assertEqual(compat_server.rewrite_jsonrpc_payload(payload), payload)

    def test_reliability_status_maps_to_native_tool(self):
        result = compat_server.rewrite_jsonrpc_payload(_rpc(compat_server.RELIABILITY_STATUS_COMMAND))
        self.assertEqual(result["params"]["name"], "reliability_status")
        self.assertEqual(result["params"]["arguments"], {})

    def test_guardian_defaults_read_only(self):
        result = compat_server.rewrite_jsonrpc_payload(
            _rpc(compat_server.RUN_GUARDIAN_PREFIX + "{}")
        )
        self.assertEqual(result["params"]["name"], "run_guardian_lite")
        self.assertEqual(result["params"]["arguments"], {"auto_repair": False})

    def test_guardian_requires_real_boolean(self):
        result = compat_server.rewrite_jsonrpc_payload(
            _rpc(compat_server.RUN_GUARDIAN_PREFIX + json.dumps({"auto_repair": "false"}))
        )
        self.assertEqual(result["params"]["name"], compat_server.REJECTED_TOOL_NAME)
        self.assertEqual(result["params"]["arguments"], {})

    def test_durable_submit_maps_without_loosening_native_validation(self):
        packet = {"packet_version": "1.0", "idempotency_key": "idem-compat"}
        result = compat_server.rewrite_jsonrpc_payload(
            _rpc(
                compat_server.DURABLE_SUBMIT_PREFIX
                + json.dumps(
                    {
                        "packet": packet,
                        "source_shared_state_version": 19,
                        "priority": 7,
                        "execution_room_id": "acceptance-room",
                    }
                )
            )
        )
        self.assertEqual(result["params"]["name"], "submit_task_packet_durable")
        args = result["params"]["arguments"]
        self.assertEqual(json.loads(args["packet_json"]), packet)
        self.assertEqual(args["source_shared_state_version"], 19)
        self.assertEqual(args["priority"], 7)
        self.assertEqual(args["execution_room_id"], "acceptance-room")

    def test_durable_submit_rejects_coerced_integer(self):
        result = compat_server.rewrite_jsonrpc_payload(
            _rpc(
                compat_server.DURABLE_SUBMIT_PREFIX
                + json.dumps(
                    {
                        "packet_json": "{}",
                        "source_shared_state_version": "19",
                    }
                )
            )
        )
        self.assertEqual(result["params"]["name"], compat_server.REJECTED_TOOL_NAME)

    def test_durable_status_requires_identifier(self):
        rejected = compat_server.rewrite_jsonrpc_payload(
            _rpc(compat_server.DURABLE_STATUS_PREFIX + "{}")
        )
        self.assertEqual(rejected["params"]["name"], compat_server.REJECTED_TOOL_NAME)

        accepted = compat_server.rewrite_jsonrpc_payload(
            _rpc(compat_server.DURABLE_STATUS_PREFIX + json.dumps({"job_id": "packet-1"}))
        )
        self.assertEqual(accepted["params"]["name"], "task_packet_status")
        self.assertEqual(accepted["params"]["arguments"]["job_id"], "packet-1")

    def test_incident_maps_detail_to_native_json_string(self):
        result = compat_server.rewrite_jsonrpc_payload(
            _rpc(
                compat_server.RECORD_INCIDENT_PREFIX
                + json.dumps(
                    {
                        "event_type": "ACCEPTANCE_PROBE",
                        "job_id": "packet-1",
                        "detail": {"safe": True},
                    }
                )
            )
        )
        self.assertEqual(result["params"]["name"], "record_reliability_incident")
        self.assertEqual(json.loads(result["params"]["arguments"]["detail_json"]), {"safe": True})

    def test_unknown_reserved_command_fails_closed_without_openai_fallback(self):
        result = compat_server.rewrite_jsonrpc_payload(_rpc("JAYTEC_DURABLE_UNKNOWN"))
        self.assertEqual(result["params"]["name"], compat_server.REJECTED_TOOL_NAME)
        self.assertEqual(result["params"]["arguments"], {})

    def test_batch_payload_rewrites_only_reserved_call(self):
        payload = [_rpc("ordinary", request_id=1), _rpc(compat_server.RELIABILITY_STATUS_COMMAND, request_id=2)]
        result = compat_server.rewrite_jsonrpc_payload(payload)
        self.assertEqual(result[0], payload[0])
        self.assertEqual(result[1]["params"]["name"], "reliability_status")

    def test_malformed_json_body_passes_through_for_native_protocol_handling(self):
        body = b'{"jsonrpc":"2.0"'
        self.assertEqual(compat_server.rewrite_jsonrpc_body(body), body)

    def test_asgi_middleware_rewrites_fragmented_request_and_content_length(self):
        capture, sent = asyncio.run(
            _run_middleware(_rpc(compat_server.RELIABILITY_STATUS_COMMAND))
        )
        rewritten = json.loads(capture.body.decode("utf-8"))
        self.assertEqual(rewritten["params"]["name"], "reliability_status")
        lengths = [value for key, value in capture.scope["headers"] if key.lower() == b"content-length"]
        self.assertEqual(lengths, [str(len(capture.body)).encode("ascii")])
        self.assertEqual(sent[0]["status"], 204)

    def test_asgi_middleware_leaves_normal_collaborate_body_byte_identical(self):
        payload = _rpc("normal collaboration request")
        capture, _ = asyncio.run(_run_middleware(payload))
        self.assertEqual(json.loads(capture.body), payload)


class TestCompatProcessIsolation(unittest.TestCase):
    def test_four_processes_construct_isolated_compat_runtime_under_load(self):
        env = os.environ.copy()
        env.update(
            {
                "RUNTIME_MODE": "staging_candidate",
                "MCP_AUTH_TOKEN": "test",
                "OPENAI_API_KEY": "test",
                "CODEX_MODEL": "gpt-5.3-codex",
                "GEMINI_MODEL": "google/gemini-3.1-pro-preview",
                "DURABLE_WORKER_ENABLED": "0",
                "GUARDIAN_LOOP_ENABLED": "0",
            }
        )
        env.pop("DATABASE_URL", None)
        code = r'''
import asyncio
import json
import compat_server
import server

before = server._legacy_collaborate_command
mcp = compat_server.create_mcp_app()
http_app = compat_server.create_http_app(mcp)
after = server._legacy_collaborate_command
for index in range(100):
    payload = {
        "jsonrpc": "2.0",
        "id": index,
        "method": "tools/call",
        "params": {"name": "collaborate", "arguments": {"task": "JAYTEC_RELIABILITY_STATUS"}},
    }
    rewritten = compat_server.rewrite_jsonrpc_payload(payload)
    assert rewritten["params"]["name"] == "reliability_status"
    ordinary = dict(payload)
    ordinary["params"] = {"name": "collaborate", "arguments": {"task": "ordinary"}}
    assert compat_server.rewrite_jsonrpc_payload(ordinary) == ordinary
names = {tool.name for tool in asyncio.run(mcp.list_tools())}
legacy = {"ask_openai", "review_notion_answer", "collaborate", "bridge_status", "orchestration_status", "execute_task_packet"}
native = {"reliability_status", "run_guardian_lite", "submit_task_packet_durable", "task_packet_status", "record_reliability_incident", "durable_worker_kick"}
print(json.dumps({
    "legacy_missing": sorted(legacy - names),
    "native_missing": sorted(native - names),
    "router_identity_preserved": before is after,
    "http_app_created": http_app is not None,
    "tool_count": len(names),
}))
'''
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            if process.returncode != 0:
                self.fail(f"compat subprocess failed: {stderr}")
            results.append(json.loads(stdout.strip()))

        for result in results:
            self.assertEqual(result["legacy_missing"], [])
            self.assertEqual(result["native_missing"], [])
            self.assertTrue(result["router_identity_preserved"])
            self.assertTrue(result["http_app_created"])
        self.assertEqual(len({result["tool_count"] for result in results}), 1)


if __name__ == "__main__":
    unittest.main()
