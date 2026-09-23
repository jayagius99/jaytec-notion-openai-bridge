import unittest

from five_seat_quarantine_evidence import _blocker_summary


class V1QuarantineDiagnosticTests(unittest.TestCase):
    def test_blocker_summary_redacts_operation_details(self):
        raw = [
            {
                "source": "EXPIRED_WORKER_RECONCILIATION",
                "action": "EXPIRED_WORKER_QUARANTINED",
                "unresolved_operations": [
                    {"operation_id": "op-secretish", "target": "hidden"},
                    {"operation_id": "op-two"},
                ],
            }
        ]
        result = _blocker_summary(raw)
        self.assertEqual(
            result,
            [
                {
                    "source": "EXPIRED_WORKER_RECONCILIATION",
                    "reason": "",
                    "action": "EXPIRED_WORKER_QUARANTINED",
                    "unresolved_operation_count": 2,
                }
            ],
        )
        self.assertNotIn("target", result[0])

    def test_unparseable_blocker_fails_bounded(self):
        self.assertEqual(
            _blocker_summary("not-json"),
            [{"source": "UNPARSEABLE_BLOCKER"}],
        )


if __name__ == "__main__":
    unittest.main()
