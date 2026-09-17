import json
import unittest

import compat_server
from durable_tasks_runtime import ReliableDurableTaskQueue
from orchestration import PacketValidationError
from test_orchestration import base_packet


def _rpc(task):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "collaborate",
            "arguments": {"task": task, "notion_analysis": "", "context": ""},
        },
    }


def _native_submit_args(body):
    task = compat_server.DURABLE_SUBMIT_PREFIX + json.dumps(body)
    rewritten = compat_server.rewrite_jsonrpc_payload(_rpc(task))
    return rewritten["params"]["name"], rewritten["params"]["arguments"]


class _ValidationQueue(ReliableDurableTaskQueue):
    """Use real validation while proving malformed/secret packets never touch DB."""

    def __init__(self):
        self.connect_calls = 0

    def _connect(self):
        self.connect_calls += 1
        raise AssertionError("validation must fail before database access")


class TestCompatNativeValidation(unittest.TestCase):
    def test_compat_mapping_preserves_real_packet_validation_before_db(self):
        packet = base_packet()
        packet["surprise"] = True
        tool_name, args = _native_submit_args(
            {
                "packet": packet,
                "source_shared_state_version": 21,
            }
        )
        self.assertEqual(tool_name, "submit_task_packet_durable")

        queue = _ValidationQueue()
        with self.assertRaises(PacketValidationError) as ctx:
            queue.submit(
                args["packet_json"],
                source_shared_state_version=args["source_shared_state_version"],
                priority=args["priority"],
                source_execution_room_id=args["execution_room_id"] or None,
            )
        self.assertIn("unknown_fields", str(ctx.exception))
        self.assertEqual(queue.connect_calls, 0)

    def test_compat_mapping_preserves_secret_rejection_before_db(self):
        packet = base_packet()
        packet["required_context"] = {"api_key": "sk-proj-do-not-persist"}
        tool_name, args = _native_submit_args(
            {
                "packet": packet,
                "source_shared_state_version": 21,
            }
        )
        self.assertEqual(tool_name, "submit_task_packet_durable")

        queue = _ValidationQueue()
        with self.assertRaises(PacketValidationError) as ctx:
            queue.submit(
                args["packet_json"],
                source_shared_state_version=args["source_shared_state_version"],
                priority=args["priority"],
                source_execution_room_id=args["execution_room_id"] or None,
            )
        self.assertIn("packet_contains_secret_material", str(ctx.exception))
        self.assertEqual(queue.connect_calls, 0)

    def test_priority_rejects_string_coercion(self):
        tool_name, args = _native_submit_args(
            {
                "packet_json": "{}",
                "source_shared_state_version": 21,
                "priority": "7",
            }
        )
        self.assertEqual(tool_name, compat_server.REJECTED_TOOL_NAME)
        self.assertEqual(args, {})

    def test_boolean_is_not_accepted_as_integer_control(self):
        tool_name, args = _native_submit_args(
            {
                "packet_json": "{}",
                "source_shared_state_version": True,
            }
        )
        self.assertEqual(tool_name, compat_server.REJECTED_TOOL_NAME)
        self.assertEqual(args, {})


if __name__ == "__main__":
    unittest.main()
