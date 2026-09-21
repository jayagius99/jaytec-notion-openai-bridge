from __future__ import annotations

import unittest
from unittest.mock import patch

from manus_runtime import ManusLiteRuntime


class FakeClient:
    def __init__(self, *, status="completed", profile="lite", project_id="project-1", messages=None):
        self.status = status
        self.profile = profile
        self.project_id = project_id
        self.messages = messages or []
        self.stop_calls = []

    def task_detail(self, task_id):
        return {
            "task": {
                "id": task_id,
                "status": self.status,
                "agent_profile": self.profile,
                "project_id": self.project_id,
            }
        }

    def resolve_manus_project(self):
        return ("project-1", "JAYTEC")

    def list_messages(self, task_id, limit=100):
        return list(self.messages)

    def stop_task(self, task_id):
        self.stop_calls.append(task_id)
        raise AssertionError("readonly status must never stop a task")


class ReadOnlyStatusTests(unittest.TestCase):
    def test_profile_mismatch_fails_closed_without_stop(self):
        client = FakeClient(profile="fast")
        runtime = ManusLiteRuntime(client)
        result = runtime.task_status_readonly("worker-1")
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertEqual(result["reason"], "MANUS_RUNTIME_PROFILE_MISMATCH")
        self.assertEqual(client.stop_calls, [])

    def test_project_mismatch_fails_closed_without_stop(self):
        client = FakeClient(project_id="other")
        runtime = ManusLiteRuntime(client)
        result = runtime.task_status_readonly("worker-1")
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertEqual(result["reason"], "MANUS_RUNTIME_PROJECT_MISMATCH")
        self.assertEqual(client.stop_calls, [])

    def test_pending_is_read_only(self):
        client = FakeClient(status="running")
        runtime = ManusLiteRuntime(client)
        result = runtime.task_status_readonly("worker-1")
        self.assertEqual(result["status"], "PENDING")
        self.assertTrue(result["read_only"])
        self.assertEqual(client.stop_calls, [])

    def test_governance_rejection_is_safe_and_read_only(self):
        client = FakeClient(status="completed", messages=[{"ignored": True}])
        runtime = ManusLiteRuntime(client)
        invalid_result = {
            "status": "SUCCESS",
            "verification": {},
            "evidence": [],
            "specialist_requests": [],
        }
        with patch("manus_runtime._latest_structured_value", return_value=invalid_result):
            result = runtime.task_status_readonly("worker-1")
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertTrue(
            result["reason"].startswith(
                "MANUS_RUNTIME_GOVERNANCE_REJECTED:MANUS_SUCCESS_NOT_VERIFIED:"
            )
        )
        self.assertEqual(client.stop_calls, [])

    def test_bad_specialist_request_reports_governance_code_without_stop(self):
        client = FakeClient(status="completed", messages=[{"ignored": True}])
        runtime = ManusLiteRuntime(client)
        invalid_result = {
            "status": "NEEDS_JAYTEC",
            "verification": {},
            "evidence": [],
            "specialist_requests": ['{"type":"SPECIALIST_REQUEST"}'],
        }
        with patch("manus_runtime._latest_structured_value", return_value=invalid_result):
            result = runtime.task_status_readonly("worker-1")
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertEqual(
            result["reason"],
            "MANUS_RUNTIME_GOVERNANCE_REJECTED:MANUS_SPECIALIST_REQUEST_FIELDS_INVALID",
        )
        shape = result["specialist_request_shape_diagnostics"]
        self.assertFalse(shape["values_included"])
        self.assertEqual(shape["request_count"], 1)
        self.assertEqual(shape["requests"][0]["present_fields"], ["type"])
        self.assertIn("packet_sha256", shape["requests"][0]["missing_fields"])
        self.assertEqual(shape["requests"][0]["extra_fields"], [])
        self.assertNotIn("SPECIALIST_REQUEST", json.dumps(shape))
        self.assertEqual(client.stop_calls, [])


if __name__ == "__main__":
    unittest.main()
