import unittest
from pathlib import Path

from five_seat_watch import (
    WATCH_REVIEW_DECISIONS,
    review_target,
)


class TestFiveSeatWatchContract(unittest.TestCase):
    def test_exact_review_decisions(self):
        self.assertEqual(
            WATCH_REVIEW_DECISIONS,
            ("ACCEPT", "REWORK", "BLOCK", "ESCALATE"),
        )

    def test_review_targets(self):
        self.assertEqual(review_target("ACCEPT"), ("SUCCEEDED", "SUCCEEDED"))
        self.assertEqual(review_target("REWORK"), ("QUEUED", "REWORK_QUEUED"))
        self.assertEqual(review_target("BLOCK"), ("BLOCKED", "QUARANTINED"))
        self.assertEqual(review_target("ESCALATE"), ("BLOCKED", "ESCALATED"))

    def test_schema_has_exactly_one_control_plane_definition_each(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS jaytec_worker_handoffs"), 1)
        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS jaytec_watch_leader"), 1)
        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS jaytec_watch_reviews"), 1)
        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS jaytec_worker_seats"), 1)
        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS jaytec_fabric_envelopes"), 1)

    def test_schema_has_singleton_watch_not_sixth_seat(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        self.assertIn("controller_id TEXT PRIMARY KEY CHECK (controller_id='WATCH')", schema)
        self.assertNotIn("WORKER-SEAT-6", schema)

    def test_worker_handoff_is_immutable_candidate_evidence(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        source = Path(__file__).with_name("five_seat_runtime.py").read_text(encoding="utf-8")
        self.assertIn("handoff_digest TEXT NOT NULL UNIQUE", schema)
        self.assertIn("UNIQUE(job_id,ownership_epoch,job_fence_token)", schema)
        self.assertIn("INSERT INTO jaytec_worker_handoffs", source)
        self.assertIn("conflicting_handoff_replay", source)

    def test_worker_retires_before_review(self):
        source = Path(__file__).with_name("five_seat_runtime.py").read_text(encoding="utf-8")
        insert_pos = source.index("INSERT INTO jaytec_worker_handoffs")
        release_pos = source.index("UPDATE jaytec_worker_seats", insert_pos)
        self.assertLess(insert_pos, release_pos)
        self.assertIn("HANDOFF_PENDING_REVIEW", source)

    def test_no_self_approval_and_fenced_review(self):
        source = Path(__file__).with_name("five_seat_watch.py").read_text(encoding="utf-8")
        self.assertIn("self._assert_leader(cur, token)", source)
        self.assertIn("WatchSelfApprovalForbidden", source)
        self.assertIn('str(handoff["worker_id"]) == token.owner', source)

    def test_accept_requires_independent_evidence(self):
        source = Path(__file__).with_name("five_seat_watch.py").read_text(encoding="utf-8")
        self.assertIn('if decision == "ACCEPT" and not dict(evidence or {})', source)

    def test_unresolved_side_effects_block_handoff(self):
        source = Path(__file__).with_name("five_seat_runtime.py").read_text(encoding="utf-8")
        self.assertIn("UNRESOLVED_OPERATION_STATUSES", source)
        self.assertIn("FiveSeatReleaseBlocked", source)

    def test_watch_emits_explicit_owner_notification_events(self):
        source = Path(__file__).with_name("five_seat_watch.py").read_text(encoding="utf-8")
        self.assertIn("'OWNER_NOTIFICATION_REQUIRED'", source)
        self.assertIn('"WHOLE_JOB_COMPLETE"', source)
        self.assertIn('"NEEDS_OWNER"', source)
        self.assertIn('if decision in {"ACCEPT", "ESCALATE"}', source)

    def test_watch_review_can_wake_runnable_work_but_signal_is_not_authority(self):
        watch = Path(__file__).with_name("five_seat_watch.py").read_text(encoding="utf-8")
        signals = Path(__file__).with_name("five_seat_signals.py").read_text(encoding="utf-8")
        self.assertIn("WORK_AVAILABLE_CHANNEL", watch)
        self.assertIn("pg_notify", watch)
        self.assertIn("Durable Postgres rows remain authority", signals)

    def test_watch_leader_has_exact_fenced_release(self):
        source = Path(__file__).with_name("five_seat_watch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def release_leader", source)
        self.assertIn("AND lease_owner=%s", source)
        self.assertIn("AND leader_epoch=%s", source)
        self.assertIn("AND fence_token=%s", source)
        self.assertIn("SET lease_owner=NULL", source)
        self.assertIn("lease_expires_at=NULL", source)


if __name__ == "__main__":
    unittest.main()
