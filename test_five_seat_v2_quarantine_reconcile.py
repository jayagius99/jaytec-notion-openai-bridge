import unittest
from datetime import datetime, timezone

import five_seat_v2_quarantine_reconcile as reconcile


class V2SyntheticQuarantineReconcileTests(unittest.TestCase):
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

    def test_exact_packet_contract_accepts_expired_read_only_v2(self):
        reconcile._validate_packet_contract(
            self.packet(),
            now=datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc),
        )

    def test_packet_contract_rejects_nonexpired_deadline(self):
        with self.assertRaisesRegex(
            reconcile.V2SyntheticQuarantineReconcileRefused,
            "PACKET_DEADLINE_NOT_EXPIRED",
        ):
            reconcile._validate_packet_contract(
                self.packet(),
                now=datetime(2026, 9, 23, 11, 30, tzinfo=timezone.utc),
            )

    def test_packet_contract_rejects_mutation(self):
        packet = self.packet()
        packet["allowed_operations"].append("write")
        with self.assertRaisesRegex(
            reconcile.V2SyntheticQuarantineReconcileRefused,
            "PACKET_ALLOWED_OPERATIONS_MISMATCH",
        ):
            reconcile._validate_packet_contract(
                packet,
                now=datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc),
            )

    def test_packet_contract_rejects_wrong_lineage(self):
        packet = self.packet()
        packet["idempotency_key"] = "other"
        with self.assertRaisesRegex(
            reconcile.V2SyntheticQuarantineReconcileRefused,
            "PACKET_IDEMPOTENCY_KEY_MISMATCH",
        ):
            reconcile._validate_packet_contract(
                packet,
                now=datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
