import unittest
from datetime import datetime, timezone

import five_seat_v2_expired_reconcile as reconcile


class V2ExpiredReconcileTests(unittest.TestCase):
    def packet(self):
        return {
            "task_id": reconcile.V2_TASK_ID,
            "idempotency_key": reconcile.V2_IDEMPOTENCY_KEY,
            "side_effect_policy": "none",
            "allowed_operations": ["analyze", "validate"],
            "specialist_plan": ["codex"],
            "max_retries": 0,
            "deadline": reconcile.V2_EXPECTED_DEADLINE,
        }

    def test_accepts_exact_expired_never_run_packet(self):
        reconcile._validate_packet_contract(
            self.packet(),
            now=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
        )

    def test_refuses_if_deadline_not_yet_expired(self):
        with self.assertRaisesRegex(
            reconcile.ExpiredV2ReconcileRefused,
            "V2_DEADLINE_NOT_EXPIRED",
        ):
            reconcile._validate_packet_contract(
                self.packet(),
                now=datetime(2026, 9, 23, 11, 30, tzinfo=timezone.utc),
            )

    def test_refuses_different_deadline_lineage(self):
        packet = self.packet()
        packet["deadline"] = "2026-09-23T14:00:00Z"
        with self.assertRaisesRegex(
            reconcile.ExpiredV2ReconcileRefused,
            "V2_DEADLINE_LINEAGE_MISMATCH",
        ):
            reconcile._validate_packet_contract(
                packet,
                now=datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc),
            )

    def test_refuses_mutating_operation(self):
        packet = self.packet()
        packet["allowed_operations"].append("write")
        with self.assertRaisesRegex(
            reconcile.ExpiredV2ReconcileRefused,
            "PACKET_ALLOWED_OPERATIONS_MISMATCH",
        ):
            reconcile._validate_packet_contract(
                packet,
                now=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
