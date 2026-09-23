import json
import unittest
from pathlib import Path


class TestFiveSeatFabricContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(
            Path(__file__).with_name("config").joinpath("five_seat_fabric_v1.json").read_text(encoding="utf-8")
        )

    def test_exactly_five_worker_seats(self):
        self.assertEqual(self.config["worker_seat_count"], 5)
        self.assertEqual(
            self.config["worker_seat_ids"],
            [f"WORKER-SEAT-{index}" for index in range(1, 6)],
        )

    def test_watch_is_out_of_band(self):
        controller = self.config["controller"]
        self.assertTrue(controller["out_of_band"])
        self.assertFalse(controller["consumes_worker_seat"])
        self.assertTrue(controller["singleton_leader_required"])

    def test_worker_cannot_self_approve(self):
        self.assertFalse(self.config["controller"]["may_self_approve_worker_output"])
        self.assertEqual(
            self.config["controller"]["review_decisions"],
            ["ACCEPT", "REWORK", "BLOCK", "ESCALATE"],
        )

    def test_handoff_precedes_success(self):
        lifecycle = self.config["task_lifecycle"]
        self.assertLess(lifecycle.index("HANDOFF_PENDING_REVIEW"), lifecycle.index("REVIEWING"))
        self.assertLess(lifecycle.index("REVIEWING"), lifecycle.index("SUCCEEDED"))

    def test_safety_invariants_are_enabled(self):
        invariants = self.config["invariants"]
        required = (
            "durable_before_execution",
            "stale_authority_rejected",
            "worker_result_requires_watch_review",
            "seat_release_requires_durable_handoff",
            "partial_side_effects_require_reconciliation",
            "silent_provider_fallback_forbidden",
            "silent_model_fallback_forbidden",
            "canonical_high_risk_serialized",
            "chat_is_not_worker_lifetime",
            "scheduled_automation_is_not_worker_seat",
        )
        for key in required:
            self.assertIs(invariants[key], True, key)


if __name__ == "__main__":
    unittest.main()
