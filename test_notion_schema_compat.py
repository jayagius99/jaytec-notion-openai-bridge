import json
import unittest

import notion_compat_server


class _Queue:
    def __init__(self):
        self.submit_calls = []
        self.status_calls = []
        self.incidents = []

    def stats(self):
        return {
            "jobs": {"SUCCEEDED": 1},
            "task_packets": {"SUCCEEDED": 1},
            "guardian_findings": {},
            "recent_incidents_24h": {},
        }

    def submit(
        self,
        packet_json,
        *,
        source_shared_state_version,
        priority,
        source_execution_room_id,
    ):
        packet = json.loads(packet_json)
        self.submit_calls.append(
            (
                packet,
                source_shared_state_version,
                priority,
                source_execution_room_id,
            )
        )
        return {
            "job_id": "packet-same-job",
            "idempotency_key": packet["idempotency_key"],
            "packet_status": "QUEUED",
        }

    def status(self, *, job_id=None, idempotency_key=None):
        self.status_calls.append((job_id, idempotency_key))
        return {
            "job_id": job_id or "packet-from-idem",
            "idempotency_key": idempotency_key or "idem",
            "packet_status": "SUCCEEDED",
        }

    def record_incident(self, event_type, *, job_id=None, detail):
        self.incidents.append((event_type, job_id, detail))
        return {"event_type": event_type, "job_id": job_id, "payload": detail}


class _Worker:
    alive = True
    worker_count = 4
    alive_count = 4

    def run_once(self):
        return False


class _Guardian:
    def run(self, *, auto_repair=True):
        return {
            "status": "HEALTHY",
            "finding_count": 0,
            "findings": [],
            "repairs": {},
            "auto_repair": auto_repair,
        }


class _GuardianLoop:
    alive = True


class TestNotionSchemaCompatibility(unittest.TestCase):
    def setUp(self):
        self.queue = _Queue()
        state = {
            "queue": self.queue,
            "worker": _Worker(),
            "guardian": _Guardian(),
            "guardian_loop": _GuardianLoop(),
        }

        def original(task, _status_fn, _packet_fn):
            if task == "LEGACY_KNOWN":
                return "legacy-result"
            return None

        self.command = notion_compat_server._build_legacy_command_adapter(
            original,
            state,
        )

    def call(self, task):
        return self.command(task, lambda: "status", lambda _packet: "packet")

    def test_preserves_original_legacy_commands(self):
        self.assertEqual(self.call("LEGACY_KNOWN"), "legacy-result")
        self.assertIsNone(self.call("ordinary collaboration request"))

    def test_reliability_status_is_available_through_collaborate(self):
        payload = json.loads(self.call(notion_compat_server.RELIABILITY_STATUS_COMMAND))
        self.assertEqual(payload["runtime_id"], notion_compat_server.reliable_server.RUNTIME_ID)
        self.assertTrue(payload["database_available"])
        self.assertTrue(payload["durable_worker_alive"])
        self.assertEqual(payload["durable_worker_count"], 4)
        self.assertEqual(payload["durable_worker_alive_count"], 4)
        self.assertTrue(payload["guardian_loop_alive"])
        self.assertEqual(payload["compatibility_surface"], "collaborate")

    def test_duplicate_submit_uses_same_durable_queue_path(self):
        packet = {
            "idempotency_key": "idem-1",
            "request": "bounded acceptance",
        }
        command = notion_compat_server.DURABLE_SUBMIT_PREFIX + json.dumps(
            {
                "packet": packet,
                "source_shared_state_version": 18,
                "priority": 40,
                "execution_room_id": "room-1",
            }
        )
        first = json.loads(self.call(command))
        second = json.loads(self.call(command))
        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertEqual(first["snapshot"]["job_id"], "packet-same-job")
        self.assertEqual(second["snapshot"]["job_id"], "packet-same-job")
        self.assertEqual(len(self.queue.submit_calls), 2)
        self.assertEqual(self.queue.submit_calls[0][1:], (18, 40, "room-1"))

    def test_status_and_guardian_commands_are_pollable(self):
        status_command = notion_compat_server.TASK_PACKET_STATUS_PREFIX + json.dumps(
            {"job_id": "packet-1"}
        )
        status = json.loads(self.call(status_command))
        self.assertEqual(status["snapshot"]["packet_status"], "SUCCEEDED")
        self.assertEqual(self.queue.status_calls, [("packet-1", None)])

        guardian_command = notion_compat_server.GUARDIAN_PREFIX + json.dumps(
            {"auto_repair": True}
        )
        guardian = json.loads(self.call(guardian_command))
        self.assertEqual(guardian["result"]["status"], "HEALTHY")
        self.assertEqual(guardian["result"]["finding_count"], 0)

    def test_bad_compat_payload_fails_closed(self):
        result = json.loads(
            self.call(notion_compat_server.DURABLE_SUBMIT_PREFIX + "[]")
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["error_class"], "ValueError")


if __name__ == "__main__":
    unittest.main()
