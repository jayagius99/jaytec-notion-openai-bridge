import unittest

from five_seat_v2_quarantine_evidence import (
    DIAGNOSTIC_FLAG,
    TARGET_JOB_ID,
    TARGET_TASK_ID,
    _blocker_summary,
)


class V2QuarantineDiagnosticTests(unittest.TestCase):
    def test_exact_v2_target_is_pinned(self):
        self.assertEqual(
            TARGET_JOB_ID,
            "fabric-9e729002f85e75f7de97ee01",
        )
        self.assertEqual(
            TARGET_TASK_ID,
            "FS08-PRODUCTION-ADMISSION-002",
        )
        self.assertEqual(
            DIAGNOSTIC_FLAG,
            "FIVE_SEAT_PROD_V2_QUARANTINE_DIAGNOSTIC",
        )

    def test_blocker_summary_redacts_operation_details(self):
        raw = [
            {
                "source": "FABRIC_QUARANTINE",
                "reason": "TEST",
                "unresolved_operations": [
                    {"operation_id": "op-one", "target": "hidden"},
                ],
            }
        ]
        result = _blocker_summary(raw)
        self.assertEqual(result[0]["unresolved_operation_count"], 1)
        self.assertNotIn("target", result[0])
        self.assertNotIn("unresolved_operations", result[0])


if __name__ == "__main__":
    unittest.main()
