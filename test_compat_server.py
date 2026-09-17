import json
import unittest
from types import SimpleNamespace

import compat_server


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
        compat_server._APP = SimpleNamespace(
            _jaytec_reliability={
                "queue": self.queue,
                "worker": self.worker,
                "guardian": self.guardian,
                "guardian_loop": _Loop(),
            }
        )

    def tearDown(self):
        compat_server._APP = None

    def _call(self, task):
        text = compat_server._compat_legacy_command(
            task,
            lambda: "legacy-status",
            lambda packet: "legacy-packet:" + packet,
        )
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

    def test_legacy_status_command_still_delegates_to_original_router(self):
        text = compat_server._compat_legacy_command(
            "JAYTEC_ORCHESTRATION_STATUS",
            lambda: "legacy-status",
            lambda packet: "legacy-packet:" + packet,
        )
        self.assertEqual(text, "legacy-status")


if __name__ == "__main__":
    unittest.main()
