import unittest

import five_seat_v1_quarantine_reconcile as reconcile


class V1SyntheticQuarantineReconcileTests(unittest.TestCase):
    def test_exact_packet_contract_accepts_read_only_probe(self):
        reconcile._validate_packet_contract(
            {
                "task_id": reconcile.V1_TASK_ID,
                "side_effect_policy": "none",
                "allowed_operations": ["analyze", "validate"],
                "specialist_plan": ["codex"],
                "max_retries": 0,
            }
        )

    def test_packet_contract_rejects_any_mutation_operation(self):
        with self.assertRaisesRegex(
            reconcile.SyntheticQuarantineReconcileRefused,
            "PACKET_ALLOWED_OPERATIONS_MISMATCH",
        ):
            reconcile._validate_packet_contract(
                {
                    "task_id": reconcile.V1_TASK_ID,
                    "side_effect_policy": "none",
                    "allowed_operations": ["analyze", "validate", "write"],
                    "specialist_plan": ["codex"],
                    "max_retries": 0,
                }
            )

    def test_packet_contract_rejects_side_effect_policy(self):
        with self.assertRaisesRegex(
            reconcile.SyntheticQuarantineReconcileRefused,
            "PACKET_SIDE_EFFECT_POLICY_NOT_NONE",
        ):
            reconcile._validate_packet_contract(
                {
                    "task_id": reconcile.V1_TASK_ID,
                    "side_effect_policy": "allowed",
                    "allowed_operations": ["analyze", "validate"],
                    "specialist_plan": ["codex"],
                    "max_retries": 0,
                }
            )


if __name__ == "__main__":
    unittest.main()
