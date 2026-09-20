from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
