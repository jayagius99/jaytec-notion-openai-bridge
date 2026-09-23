import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import dan_pc_relay
import dan_recovery
from five_seat_service import build_task_packet_adapter


class TestDanRecoveryEligibility(unittest.TestCase):
    def test_recoverable_worker_block_is_eligible(self):
        ok, reason = dan_recovery.recoverable_watch_block(
            decision="BLOCK",
            reason="worker evidence does not satisfy acceptance contract",
            result={
                "partial_side_effect_status": "NONE",
                "whole_packet_status": "FAILED_CLOSED",
                "unresolved_items": ["worker capability unavailable"],
            },
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "eligible_worker_capability_failure")

    def test_policy_hold_and_permission_blocks_never_escape_to_dan(self):
        for term in ("policy", "permission", "credential", "HOLD", "STOP", "spend"):
            ok, _ = dan_recovery.recoverable_watch_block(
                decision="BLOCK",
                reason=f"{term} boundary",
                result={"partial_side_effect_status": "NONE", "whole_packet_status": "FAILED_CLOSED"},
            )
            self.assertFalse(ok, term)

    def test_uncertain_side_effect_and_non_block_never_escape(self):
        ok, _ = dan_recovery.recoverable_watch_block(
            decision="BLOCK",
            reason="worker failed",
            result={"partial_side_effect_status": "UNCERTAIN_PARTIAL", "whole_packet_status": "FAILED_CLOSED"},
        )
        self.assertFalse(ok)
        ok, _ = dan_recovery.recoverable_watch_block(
            decision="REWORK",
            reason="worker failed",
            result={"partial_side_effect_status": "NONE", "whole_packet_status": "FAILED_CLOSED"},
        )
        self.assertFalse(ok)

    def test_recovery_is_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(dan_recovery.DanRecoveryManager.from_env("postgres://unused"))


class TestDanPcRelayEnvelope(unittest.TestCase):
    def valid_job(self):
        job = {
            "schema": dan_pc_relay.JOB_MARKER,
            "principal": "DAN-RECOVERY-SEAT",
            "task_id": "dan-test-001",
            "assignment_name": "RECOVERY_TEST",
            "objective": "Recover one bounded worker subtask.",
            "cost_policy": "ZERO_SPEND",
            "side_effect_policy": "PACKAGE_ONLY",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }
        job["request_digest"] = dan_pc_relay.digest(job)
        return job

    def test_valid_worker_envelope(self):
        dan_pc_relay.validate_job(self.valid_job())

    def test_owner_and_recovery_are_distinct_valid_principals(self):
        job = self.valid_job()
        for principal in ("DAN-OWNER", "DAN-RECOVERY-SEAT"):
            candidate = dict(job)
            candidate["principal"] = principal
            candidate.pop("request_digest", None)
            candidate["request_digest"] = dan_pc_relay.digest(candidate)
            dan_pc_relay.validate_job(candidate)

    def test_spend_side_effect_and_digest_fail_closed(self):
        cases = [
            ("cost_policy", "PAID"),
            ("side_effect_policy", "SYSTEM_WRITE"),
            ("principal", "WORKER-SEAT-6"),
        ]
        for field, value in cases:
            job = self.valid_job()
            job[field] = value
            job.pop("request_digest", None)
            job["request_digest"] = dan_pc_relay.digest(job)
            with self.assertRaises(ValueError):
                dan_pc_relay.validate_job(job)
        job = self.valid_job()
        job["request_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            dan_pc_relay.validate_job(job)


class TestDanFiveSeatWiring(unittest.TestCase):
    def test_complete_recovery_records_candidate_but_does_not_self_promote_recipe(self):
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
        receipt = {
            "status": "DAN_COMPLETE",
            "response_digest": "a" * 64,
        }
        with patch.object(dan_pc_relay, "experience_module", return_value=fake):
            result = dan_pc_relay.record_experience(job, receipt)

        self.assertEqual(result["event_id"], "evt-proof")
        self.assertEqual(result["capability_id"], "qwen.local.bounded_reasoning")
        self.assertTrue(result["repair_candidate_id"].startswith("candidate."))
        self.assertEqual(fake.recipes, [])
        self.assertTrue(any(x.get("type") == "repair_recipe_candidate" for x in fake.events))

    def test_recovered_worker_receives_dan_context_with_fresh_idempotency(self):
        captured = {}
        def fake_execute(packet_json):
            captured["packet"] = json.loads(packet_json)
            return {"overall_status": "SUCCESS", "unresolved_items": []}

        adapter = build_task_packet_adapter(fake_execute)
        adapter({
            "packet_json": json.dumps({
                "idempotency_key": "original-idem",
                "required_context": {"existing": True},
            }),
            "_fabric_context": {
                "dan_recovery": {
                    "request_digest": "b" * 64,
                    "response_digest": "a" * 64,
                    "result": "bounded recovery evidence",
                    "evidence": ["proof"],
                    "limitations": [],
                    "recommended_next_action": "resume original worker",
                }
            },
        })
        rebound = captured["packet"]
        self.assertEqual(rebound["required_context"]["existing"], True)
        self.assertEqual(rebound["required_context"]["dan_recovery"]["source"], "DAN_RECOVERY")
        self.assertNotEqual(rebound["idempotency_key"], "original-idem")
        self.assertTrue(rebound["idempotency_key"].endswith(":dan:" + "a" * 16))

    def test_dan_is_out_of_band_not_a_sixth_normal_worker(self):
        source = open("five_seat_service.py", encoding="utf-8").read()
        self.assertIn("for index in range(1, 6):", source)
        self.assertNotIn("range(1, 7)", source)
        self.assertIn("DanRecoveryManager.from_env(database_url)", source)
        self.assertIn("dispatch_watch_block", source)
        self.assertIn("poll_results_and_requeue", source)


if __name__ == "__main__":
    unittest.main()
