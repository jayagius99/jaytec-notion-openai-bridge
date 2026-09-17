import json
import os
import subprocess
import sys
import unittest

import compat_server
from durable_tasks_runtime import ReliableDurableTaskQueue
from test_orchestration import base_packet


class _Queue:
    def __init__(self):
        self.submits = []
        self.incidents = []

    def stats(self):
        return {"queued": 0, "running": 0}

    def submit(self, packet_json, *, source_shared_state_version, priority, source_execution_room_id):
        self.submits.append(
            (packet_json, source_shared_state_version, priority, source_execution_room_id)
        )
        packet = json.loads(packet_json)
        return {
            "job_id": "job-1",
            "idempotency_key": packet["idempotency_key"],
            "status": "QUEUED",
        }

    def status(self, *, job_id=None, idempotency_key=None):
        return {
            "job_id": job_id or "job-by-idem",
            "idempotency_key": idempotency_key or "idem",
            "status": "SUCCEEDED",
        }

    def record_incident(self, event_type, *, job_id=None, detail):
        event = {"event_type": event_type, "job_id": job_id, "detail": detail}
        self.incidents.append(event)
        return event


class _ValidationQueue(ReliableDurableTaskQueue):
    """Exercise real packet/secret validation while proving no DB write is reached."""

    def __init__(self):
        self.connect_calls = 0

    def _connect(self):
        self.connect_calls += 1
        raise AssertionError("validation should fail before database access")


class _Worker:
    alive = True
    worker_count = 4
    alive_count = 4

    def __init__(self):
        self.kicks = 0

    def run_once(self):
        self.kicks += 1
        return True


class _Guardian:
    def __init__(self):
        self.calls = []

    def run(self, *, auto_repair):
        self.calls.append(auto_repair)
        return {"status": "HEALTHY", "auto_repair": auto_repair}


class _Loop:
    alive = True


class TestCompatServer(unittest.TestCase):
    def setUp(self):
        self.queue = _Queue()
        self.worker = _Worker()
        self.guardian = _Guardian()
        self.runtime = {
            "queue": self.queue,
            "worker": self.worker,
            "guardian": self.guardian,
            "guardian_loop": _Loop(),
        }
        self.middleware = compat_server.ReliabilityCompatMiddleware(self.runtime)

    def _call(self, task):
        text = self.middleware.dispatch(task)
        self.assertIsNotNone(text, f"compat command unexpectedly delegated: {task}")
        return json.loads(text)

    def test_reliability_status_uses_existing_collaborate_surface(self):
        result = self._call(compat_server.RELIABILITY_STATUS_COMMAND)
        self.assertTrue(result["available"])
        self.assertTrue(result["durable_worker_alive"])
        self.assertEqual(result["durable_worker_count"], 4)
        self.assertTrue(result["guardian_loop_alive"])
        self.assertEqual(result["stats"]["queued"], 0)

    def test_guardian_defaults_to_read_only(self):
        result = self._call(compat_server.RUN_GUARDIAN_PREFIX + "{}")
        self.assertTrue(result["available"])
        self.assertFalse(result["auto_repair"])
        self.assertEqual(self.guardian.calls, [False])

    def test_guardian_rejects_string_false_instead_of_enabling_repairs(self):
        result = self._call(
            compat_server.RUN_GUARDIAN_PREFIX + json.dumps({"auto_repair": "false"})
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_class"], "ValueError")
        self.assertIn("JSON boolean", result["error"])
        self.assertEqual(self.guardian.calls, [])

    def test_guardian_accepts_explicit_true_boolean_only(self):
        result = self._call(
            compat_server.RUN_GUARDIAN_PREFIX + json.dumps({"auto_repair": True})
        )
        self.assertTrue(result["available"])
        self.assertTrue(result["auto_repair"])
        self.assertEqual(self.guardian.calls, [True])

    def test_durable_submit_accepts_packet_object(self):
        task = compat_server.DURABLE_SUBMIT_PREFIX + json.dumps(
            {
                "packet": {"idempotency_key": "idem-1"},
                "source_shared_state_version": 19,
                "priority": 50,
                "execution_room_id": "acceptance",
            }
        )
        result = self._call(task)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["snapshot"]["job_id"], "job-1")
        self.assertEqual(self.queue.submits[0][1:], (19, 50, "acceptance"))

    def test_durable_submit_rejects_coerced_control_types(self):
        result = self._call(
            compat_server.DURABLE_SUBMIT_PREFIX
            + json.dumps(
                {
                    "packet": {"idempotency_key": "idem-1"},
                    "source_shared_state_version": "19",
                }
            )
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_class"], "ValueError")
        self.assertEqual(self.queue.submits, [])

    def test_durable_submit_rejects_ambiguous_packet_sources(self):
        result = self._call(
            compat_server.DURABLE_SUBMIT_PREFIX
            + json.dumps(
                {
                    "packet": {"idempotency_key": "idem-1"},
                    "packet_json": "{}",
                    "source_shared_state_version": 19,
                }
            )
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_class"], "ValueError")
        self.assertEqual(self.queue.submits, [])

    def test_compat_path_preserves_real_packet_validation_before_db(self):
        validation_queue = _ValidationQueue()
        self.middleware = compat_server.ReliabilityCompatMiddleware(
            {
                "queue": validation_queue,
                "worker": self.worker,
                "guardian": self.guardian,
                "guardian_loop": _Loop(),
            }
        )
        packet = base_packet()
        packet["surprise"] = True
        result = self._call(
            compat_server.DURABLE_SUBMIT_PREFIX
            + json.dumps(
                {
                    "packet": packet,
                    "source_shared_state_version": 19,
                }
            )
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_class"], "PacketValidationError")
        self.assertIn("unknown_fields", result["error"])
        self.assertEqual(validation_queue.connect_calls, 0)

    def test_compat_path_preserves_real_secret_rejection_before_db(self):
        validation_queue = _ValidationQueue()
        self.middleware = compat_server.ReliabilityCompatMiddleware(
            {
                "queue": validation_queue,
                "worker": self.worker,
                "guardian": self.guardian,
                "guardian_loop": _Loop(),
            }
        )
        packet = base_packet()
        packet["required_context"] = {"api_key": "sk-proj-do-not-persist"}
        result = self._call(
            compat_server.DURABLE_SUBMIT_PREFIX
            + json.dumps(
                {
                    "packet": packet,
                    "source_shared_state_version": 19,
                }
            )
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_class"], "PacketValidationError")
        self.assertIn("packet_contains_secret_material", result["error"])
        self.assertEqual(validation_queue.connect_calls, 0)

    def test_durable_status_supports_idempotency_key(self):
        result = self._call(
            compat_server.DURABLE_STATUS_PREFIX
            + json.dumps({"idempotency_key": "idem-1"})
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["snapshot"]["status"], "SUCCEEDED")

    def test_worker_kick_and_incident_commands_are_bounded(self):
        kick = self._call(compat_server.DURABLE_WORKER_KICK_COMMAND)
        self.assertTrue(kick["worked"])
        self.assertEqual(self.worker.kicks, 1)

        incident = self._call(
            compat_server.RECORD_INCIDENT_PREFIX
            + json.dumps(
                {
                    "event_type": "ACCEPTANCE_PROBE",
                    "job_id": "job-1",
                    "detail": {"safe": True},
                }
            )
        )
        self.assertTrue(incident["available"])
        self.assertEqual(self.queue.incidents[0]["event_type"], "ACCEPTANCE_PROBE")

    def test_reserved_typo_fails_closed(self):
        result = self._call("JAYTEC_DURABLE_UNKNOWN")
        self.assertFalse(result["available"])
        self.assertEqual(result["error_class"], "UNKNOWN_COMPAT_COMMAND")

    def test_ordinary_legacy_command_is_delegated_by_middleware(self):
        result = self.middleware.dispatch("JAYTEC_ORCHESTRATION_STATUS")
        self.assertIsNone(result)

    def test_fastmcp_catalog_and_cached_collaborate_path_survive_repeat_startup(self):
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
from fastmcp import Client
import compat_server

legacy = {
    "ask_openai",
    "review_notion_answer",
    "collaborate",
    "bridge_status",
    "orchestration_status",
    "execute_task_packet",
}
native_reliability = {
    "reliability_status",
    "run_guardian_lite",
    "submit_task_packet_durable",
    "task_packet_status",
    "record_reliability_incident",
    "durable_worker_kick",
}

async def inspect(app):
    tools = await app.list_tools()
    names = {tool.name for tool in tools}
    async with Client(app) as client:
        result = await client.call_tool(
            "collaborate",
            {"task": "JAYTEC_RELIABILITY_STATUS", "notion_analysis": "", "context": ""},
        )
        text = result.content[0].text
    return names, json.loads(text)

first = compat_server.create_mcp_app()
first_names, first_status = asyncio.run(inspect(first))
second = compat_server.create_mcp_app()
second_names, second_status = asyncio.run(inspect(second))
print(json.dumps({
    "legacy_first": sorted(legacy - first_names),
    "native_first": sorted(native_reliability - first_names),
    "legacy_second": sorted(legacy - second_names),
    "native_second": sorted(native_reliability - second_names),
    "first_reason": first_status.get("reason"),
    "second_reason": second_status.get("reason"),
    "first_tool_count": len(first_names),
    "second_tool_count": len(second_names),
}))
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload["legacy_first"], [])
        self.assertEqual(payload["native_first"], [])
        self.assertEqual(payload["legacy_second"], [])
        self.assertEqual(payload["native_second"], [])
        self.assertEqual(payload["first_reason"], "RELIABILITY_RUNTIME_NOT_READY")
        self.assertEqual(payload["second_reason"], "RELIABILITY_RUNTIME_NOT_READY")
        self.assertEqual(payload["first_tool_count"], payload["second_tool_count"])


if __name__ == "__main__":
    unittest.main()
