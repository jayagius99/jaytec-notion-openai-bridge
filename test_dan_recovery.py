import json
import unittest
from datetime import datetime, timedelta, timezone

import dan_recovery
import dan_pc_relay


class TestDanRecoveryPolicy(unittest.TestCase):
    def test_worker_failure_is_eligible(self):
        ok, reason = dan_recovery.recoverable_watch_block(
            decision="BLOCK",
            reason="worker evidence does not satisfy acceptance contract",
            result={
                "whole_packet_status": "FAILED_CLOSED",
                "partial_side_effect_status": "NONE",
                "unresolved_items": ["worker could not complete bounded transform"],
            },
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "eligible_worker_capability_failure")

    def test_hard_boundaries_never_escape_to_dan(self):
        for reason in (
            "owner approval required",
            "permission missing",
            "secret boundary",
            "HOLD active",
            "unauthorized spend",
        ):
            ok, _ = dan_recovery.recoverable_watch_block(
                decision="BLOCK",
                reason=reason,
                result={"whole_packet_status": "FAILED_CLOSED", "partial_side_effect_status": "NONE"},
            )
            self.assertFalse(ok)

    def test_uncertain_side_effect_is_not_recoverable(self):
        ok, reason = dan_recovery.recoverable_watch_block(
            decision="BLOCK",
            reason="worker failed",
            result={"whole_packet_status": "FAILED_CLOSED", "partial_side_effect_status": "UNCERTAIN_PARTIAL"},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "ambiguous_or_partial_side_effect")

    def test_two_principals_are_distinct(self):
        self.assertNotEqual(dan_recovery.DAN_OWNER_ID, dan_recovery.DAN_RECOVERY_ID)
        self.assertEqual(dan_recovery.DAN_OWNER_ID, "DAN-OWNER")
        self.assertEqual(dan_recovery.DAN_RECOVERY_ID, "DAN-RECOVERY-SEAT")


class TestPcRelayEnvelope(unittest.TestCase):
    def _job(self):
        job = {
            "schema": dan_pc_relay.JOB_MARKER,
            "principal": "DAN-OWNER",
            "task_id": "test-1",
            "assignment_name": "TEST",
            "objective": "Return a harmless deterministic result.",
            "cost_policy": "ZERO_SPEND",
            "side_effect_policy": "PACKAGE_ONLY",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        job["request_digest"] = dan_pc_relay.digest(job)
        return job

    def test_valid_owner_job(self):
        dan_pc_relay.validate_job(self._job())

    def test_tamper_fails_digest(self):
        job = self._job()
        job["objective"] = "changed"
        with self.assertRaisesRegex(ValueError, "REQUEST_DIGEST_MISMATCH"):
            dan_pc_relay.validate_job(job)

    def test_nonzero_spend_refused(self):
        job = self._job()
        job["cost_policy"] = "PAID"
        copy = dict(job)
        copy.pop("request_digest", None)
        job["request_digest"] = dan_pc_relay.digest(copy)
        with self.assertRaisesRegex(ValueError, "NONZERO_SPEND_REFUSED"):
            dan_pc_relay.validate_job(job)


if __name__ == "__main__":
    unittest.main()
