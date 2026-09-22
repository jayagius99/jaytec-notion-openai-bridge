import unittest
from pathlib import Path

from five_seat_runtime import (
    WORKER_SEAT_COUNT,
    WORKER_SEAT_IDS,
    FiveSeatRuntimeError,
    PostgresFiveSeatScheduler,
)


class TestFiveSeatRuntimeContract(unittest.TestCase):
    def test_exact_five_seats(self):
        self.assertEqual(WORKER_SEAT_COUNT, 5)
        self.assertEqual(
            WORKER_SEAT_IDS,
            (
                "WORKER-SEAT-1",
                "WORKER-SEAT-2",
                "WORKER-SEAT-3",
                "WORKER-SEAT-4",
                "WORKER-SEAT-5",
            ),
        )

    def test_token_from_claim_rejects_missing_seat_fence(self):
        claim = {
            "job_id": "job-1",
            "seat_id": "WORKER-SEAT-1",
            "lease_owner": "worker-1",
            "ownership_epoch": 2,
            "fence_token": 3,
            "seat_epoch": 4,
            "lease_expires_at": object(),
        }
        with self.assertRaises(FiveSeatRuntimeError):
            PostgresFiveSeatScheduler.token_from_claim(claim)

    def test_token_from_claim_rejects_unknown_seat(self):
        claim = {
            "job_id": "job-1",
            "seat_id": "WORKER-SEAT-6",
            "lease_owner": "worker-1",
            "ownership_epoch": 2,
            "fence_token": 3,
            "seat_epoch": 4,
            "seat_fence_token": 5,
            "lease_expires_at": object(),
        }
        with self.assertRaises(FiveSeatRuntimeError):
            PostgresFiveSeatScheduler.token_from_claim(claim)

    def test_schema_is_additive_and_seeds_exact_seats(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        upper = schema.upper()
        self.assertNotIn("DROP TABLE", upper)
        self.assertNotIn("TRUNCATE", upper)
        self.assertIn("CREATE TABLE IF NOT EXISTS jaytec_worker_seats", schema)
        self.assertIn("ADD COLUMN IF NOT EXISTS seat_id TEXT", schema)
        self.assertIn("ADD COLUMN IF NOT EXISTS fabric_state TEXT", schema)
        self.assertIn("INSERT INTO jaytec_worker_seats(seat_id)", schema)
        for index in range(1, 6):
            self.assertIn(f"('WORKER-SEAT-{index}')", schema)

    def test_one_running_job_per_seat_is_database_enforced(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        self.assertIn("jaytec_jobs_one_running_job_per_seat_idx", schema)
        self.assertIn("WHERE seat_id IS NOT NULL AND status='RUNNING'", schema)

    def test_runtime_reuses_existing_scheduler_lock_and_never_applies_ddl(self):
        source = Path(__file__).with_name("five_seat_runtime.py").read_text(encoding="utf-8")
        self.assertIn("SCHEDULER_LOCK_KEY", source)
        self.assertIn("pg_advisory_xact_lock", source)
        self.assertNotIn("CREATE TABLE", source)
        self.assertNotIn("ALTER TABLE", source)

    def test_release_requires_handoff_and_blocks_unresolved_side_effects(self):
        source = Path(__file__).with_name("five_seat_runtime.py").read_text(encoding="utf-8")
        self.assertIn('if not handoff_ref:', source)
        self.assertIn("UNRESOLVED_OPERATION_STATUSES", source)
        self.assertIn("HANDOFF_PENDING_REVIEW", source)
        self.assertIn("WORKER_HANDOFF_COMMITTED_AND_SEAT_RELEASED", source)
        self.assertIn("INSERT INTO jaytec_worker_handoffs", source)


if __name__ == "__main__":
    unittest.main()
