from __future__ import annotations

import unittest

from jaytec_protocol_portal import PortalError, PortalTaskRequest, portal_job_id


class PortalParsingTests(unittest.TestCase):
    def test_deterministic_job_id(self):
        a = portal_job_id("FORGE-GENESIS-ACTIVATION-001")
        b = portal_job_id("FORGE-GENESIS-ACTIVATION-001")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("watch-"))

    def test_chat_ui_registration_defaults(self):
        req = PortalTaskRequest.parse({
            "task_id": "task-1",
            "objective": "Do the bounded task.",
            "source_chat_ref": "chat-a",
            "current_task_authorized": True,
        })
        self.assertEqual(req.worker_kind, "CHATGPT_UI")
        self.assertEqual(req.worker_route, "chatgpt_ui")
        self.assertTrue(req.watch_enabled)
        self.assertEqual(req.status, "RUNNING")

    def test_callable_requires_route(self):
        with self.assertRaisesRegex(PortalError, "WORKER_ROUTE_REQUIRED"):
            PortalTaskRequest.parse({
                "task_id": "task-2",
                "objective": "Do it.",
                "source_chat_ref": "chat-b",
                "current_task_authorized": True,
                "worker_kind": "JAYTEC_CALLABLE",
            })

    def test_current_task_authority_required(self):
        with self.assertRaisesRegex(PortalError, "CURRENT_TASK_AUTH_REQUIRED"):
            PortalTaskRequest.parse({
                "task_id": "task-3",
                "objective": "No stale auth.",
                "source_chat_ref": "chat-c",
                "current_task_authorized": False,
            })

    def test_terminal_status_cannot_be_registered(self):
        with self.assertRaisesRegex(PortalError, "REGISTER_STATUS_INVALID"):
            PortalTaskRequest.parse({
                "task_id": "task-4",
                "objective": "No.",
                "source_chat_ref": "chat-d",
                "current_task_authorized": True,
                "status": "SUCCEEDED",
            })


if __name__ == "__main__":
    unittest.main()
