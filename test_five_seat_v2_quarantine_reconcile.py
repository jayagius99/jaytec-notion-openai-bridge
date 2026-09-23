import unittest

from five_seat_v2_quarantine_reconcile import (
    EXPECTED_DEADLINE,
    TASK_ID,
    V2QuarantineReconcileRefused,
    _validate_packet,
)


def packet():
    return {
        "task_id": TASK_ID,
        "deadline": EXPECTED_DEADLINE,
        "side_effect_policy": "none",
        "allowed_operations": ["analyze", "validate"],
        "specialist_plan": ["codex"],
        "max_retries": 0,
    }


class V2QuarantineReconcileTests(unittest.TestCase):
    def test_exact_expired_read_only_contract_is_accepted(self):
        _validate_packet(packet())

    def test_mutating_operation_is_refused(self):
        value = packet()
        value["allowed_operations"].append("write")
        with self.assertRaises(V2QuarantineReconcileRefused):
            _validate_packet(value)

    def test_deadline_lineage_drift_is_refused(self):
        value = packet()
        value["deadline"] = "2099-01-01T00:00:00Z"
        with self.assertRaises(V2QuarantineReconcileRefused):
            _validate_packet(value)

    def test_retry_or_specialist_drift_is_refused(self):
        value = packet()
        value["max_retries"] = 1
        with self.assertRaises(V2QuarantineReconcileRefused):
            _validate_packet(value)
        value = packet()
        value["specialist_plan"] = ["gemini"]
        with self.assertRaises(V2QuarantineReconcileRefused):
            _validate_packet(value)


if __name__ == "__main__":
    unittest.main()
