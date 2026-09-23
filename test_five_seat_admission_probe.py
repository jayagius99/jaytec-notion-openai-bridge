import os
import unittest
from unittest.mock import patch

from durable_tasks import contains_secret_material
from five_seat_admission_probe import (
    PROBE_TASK_ID,
    build_probe_packet,
    run_probe,
    start_probe,
)
from orchestration import validate_packet


class _Authority:
    def current_state(self):
        return {"current_shared_state_version": 1}


class _Service:
    def watch_leader_snapshot(self):
        return {
            "owner": "five-seat-watch:prod-instance",
            "leader_epoch": 9,
            "fence_token": 11,
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
        }

    def job_status(self, job_id):
        return {
            "found": True,
            "job": {
                "job_id": job_id,
                "task_id": PROBE_TASK_ID,
                "status": "SUCCEEDED",
                "fabric_state": "SUCCEEDED",
                "seat_id": None,
                "watch_decision": "ACCEPT",
                "watch_controller_owner": "five-seat-watch:prod-instance",
                "watch_leader_epoch": 9,
                "watch_fence_token": 11,
                "health": "HEALTHY",
            },
        }


class _Runtime:
    fabric_enabled = True
    fabric_queue = object()
    fabric_authority = _Authority()
    fabric_service = _Service()


class TestFiveSeatAdmissionProbe(unittest.TestCase):
    def test_packet_is_valid_zero_side_effect_engineering_work(self):
        packet = build_probe_packet("probe-unit-v1")
        validation = validate_packet(packet)
        self.assertTrue(validation.ok, validation.errors)
        self.assertFalse(contains_secret_material(packet))
        self.assertEqual(packet["task_id"], PROBE_TASK_ID)
        self.assertEqual(packet["specialist_plan"], ["codex"])
        self.assertEqual(packet["side_effect_policy"], "none")
        self.assertEqual(packet["max_retries"], 0)
        self.assertTrue(packet["workflow_id"].startswith("JAYTEC_V2_"))
        self.assertEqual(
            packet["required_context"]["authority_controller"],
            "CHATGPT_OPENAI_LEAD",
        )
        self.assertEqual(
            packet["required_context"]["specialist_authority"],
            "SUBORDINATE",
        )

    def test_probe_is_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(start_probe(object()))

    def test_probe_requires_same_watch_generation_acceptance(self):
        with patch(
            "five_seat_admission_probe.submit_low_risk_task_packet",
            return_value={"job_id": "fabric-probe-unit"},
        ) as submit:
            with patch.dict(
                os.environ,
                {"FIVE_SEAT_PROD_ADMISSION_PROBE_ID": "probe-unit-v1"},
                clear=False,
            ):
                result = run_probe(_Runtime(), timeout_seconds=20)
        self.assertTrue(result["pass"], result)
        self.assertEqual(result["reason"], "WATCH_ACCEPT_SAME_GENERATION")
        self.assertEqual(result["job"]["watch_leader_epoch"], 9)
        self.assertEqual(result["job"]["watch_fence_generation"], 11)
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
