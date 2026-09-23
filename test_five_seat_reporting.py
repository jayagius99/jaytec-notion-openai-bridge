import os
import unittest

from five_seat_reporting import FiveSeatReporter, REPORT_SCHEMA, SEAT_IDS, _next_action


class TestFiveSeatReportingContract(unittest.TestCase):
    def test_exact_five_seats(self):
        self.assertEqual(
            SEAT_IDS,
            (
                "WORKER-SEAT-1",
                "WORKER-SEAT-2",
                "WORKER-SEAT-3",
                "WORKER-SEAT-4",
                "WORKER-SEAT-5",
            ),
        )

    def test_next_action_is_fail_closed(self):
        self.assertIn(
            "blocker",
            _next_action("BLOCKED", "QUARANTINED", True).lower(),
        )
        self.assertIn(
            "watch",
            _next_action("SUCCEEDED", "HANDOFF_PENDING_REVIEW", False).lower(),
        )


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required")
class TestFiveSeatReportingPostgres(unittest.TestCase):
    def test_last_60_minutes_report_shape(self):
        report = FiveSeatReporter(os.environ["DATABASE_URL"]).last_60_minutes()
        self.assertEqual(report["schema_version"], REPORT_SCHEMA)
        self.assertEqual(report["window_minutes"], 60)
        self.assertEqual(len(report["seats"]), 5)
        self.assertEqual(
            [row["seat_id"] for row in report["seats"]],
            list(SEAT_IDS),
        )
        self.assertEqual(report["summary"]["seats_total"], 5)
        self.assertIn("watch", report)
        self.assertIn("completed_jobs", report)
        self.assertIn("attempted_not_completed", report)
        self.assertIn("queued_jobs", report)
        self.assertIn("provider_and_spend", report)
        self.assertIn("jay_action_required", report)


if __name__ == "__main__":
    unittest.main()
