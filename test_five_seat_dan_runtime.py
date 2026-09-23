import unittest
from unittest.mock import patch

import dan_pc_relay
import dan_recovery


class TestDanRuntimeHardening(unittest.TestCase):
    def test_dan_is_recovery_lane_not_sixth_normal_worker(self):
        service = open("five_seat_service.py", encoding="utf-8").read()
        self.assertIn("for index in range(1, 6):", service)
        self.assertNotIn("range(1, 7)", service)
        self.assertIn("DanRecoveryManager", service)

    def test_worker_continuation_gets_recovery_context_and_fresh_idempotency(self):
        worker = open("five_seat_worker.py", encoding="utf-8").read()
        self.assertIn('required_context["dan_recovery"]', worker)
        self.assertIn('"dan-recovery-" + hashlib.sha256', worker)
        self.assertIn('response_digest', worker)

    def test_recovery_is_bounded_and_exact_runtime_is_required(self):
        self.assertEqual(dan_recovery.MAX_DAN_RECOVERIES_PER_JOB, 2)
        self.assertEqual(
            dan_recovery.EXPECTED_QWEN_SERVER_SHA256,
            "06f5c5463753a7a6fe729bb436a6d3ab5e71373527559339b42cec9fd7f1d27f",
        )
        self.assertEqual(
            dan_pc_relay.SERVER_SHA256,
            dan_recovery.EXPECTED_QWEN_SERVER_SHA256,
        )

    def test_complete_recovery_learns_but_does_not_self_promote_recipe(self):
        class FakeExperience:
            def __init__(self):
                self.events = []
                self.capabilities = []
                self.recipes = []
            def append_event(self, event):
                row = dict(event)
                row["event_id"] = "evt-proof"
                self.events.append(row)
                return row
            def update_capability(self, record):
                self.capabilities.append(dict(record))
                return {"capability_id": record["capability_id"]}
            def upsert_recipe(self, record):
                self.recipes.append(dict(record))
                return record

        fake = FakeExperience()
        job = {
            "principal": "DAN-RECOVERY-SEAT",
            "task_id": "proof-task",
            "assignment_name": "proof",
            "original_job_id": "fabric-job",
            "original_handoff_id": "handoff",
            "original_worker_kind": "TASK_PACKET",
            "watch_reason": "worker capability failure",
            "worker_failure": {"unresolved_items": ["model unavailable"]},
        }
        receipt = {"status": "DAN_COMPLETE", "response_digest": "a" * 64}
        with patch.object(dan_pc_relay, "experience_module", return_value=fake):
            result = dan_pc_relay.record_experience(job, receipt)

        self.assertEqual(result["event_id"], "evt-proof")
        self.assertEqual(result["capability_id"], "qwen.local.bounded_reasoning")
        self.assertTrue(result["repair_candidate_id"].startswith("candidate."))
        self.assertEqual(fake.recipes, [])
        self.assertTrue(
            any(x.get("type") == "repair_recipe_candidate" for x in fake.events)
        )

    def test_hard_boundaries_remain_nonrecoverable(self):
        for reason in (
            "permission missing",
            "credential required",
            "STOP active",
            "HOLD active",
            "owner approval required",
            "paid spend not authorized",
        ):
            ok, _ = dan_recovery.recoverable_watch_block(
                decision="BLOCK",
                reason=reason,
                result={
                    "whole_packet_status": "FAILED_CLOSED",
                    "partial_side_effect_status": "NONE",
                },
            )
            self.assertFalse(ok, reason)


if __name__ == "__main__":
    unittest.main()
