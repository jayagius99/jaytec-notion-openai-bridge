import unittest
from pathlib import Path

from job_runtime import operation_transition_allowed, stale_fence


class TestDurableJobRuntimeInvariants(unittest.TestCase):
    def test_same_epoch_and_fence_is_current(self):
        self.assertFalse(
            stale_fence(
                expected_epoch=4,
                expected_fence=9,
                actual_epoch=4,
                actual_fence=9,
            )
        )

    def test_superseded_epoch_is_stale(self):
        self.assertTrue(
            stale_fence(
                expected_epoch=4,
                expected_fence=9,
                actual_epoch=5,
                actual_fence=10,
            )
        )

    def test_changed_fence_is_stale_even_if_epoch_matches(self):
        self.assertTrue(
            stale_fence(
                expected_epoch=4,
                expected_fence=9,
                actual_epoch=4,
                actual_fence=10,
            )
        )

    def test_uncertain_partial_cannot_blindly_retry(self):
        self.assertFalse(operation_transition_allowed("UNCERTAIN_PARTIAL", "IN_FLIGHT"))
        self.assertTrue(operation_transition_allowed("UNCERTAIN_PARTIAL", "VERIFIED_COMPLETE"))
        self.assertTrue(operation_transition_allowed("UNCERTAIN_PARTIAL", "VERIFIED_NOT_DONE"))

    def test_verified_operation_is_terminal(self):
        self.assertFalse(operation_transition_allowed("VERIFIED_COMPLETE", "IN_FLIGHT"))
        self.assertFalse(operation_transition_allowed("VERIFIED_NOT_DONE", "IN_FLIGHT"))

    def test_prepared_operation_may_start(self):
        self.assertTrue(operation_transition_allowed("PREPARED", "IN_FLIGHT"))

    def test_schema_is_additive_and_contains_core_runtime_tables(self):
        schema = Path(__file__).with_name("job_runtime_schema.sql").read_text(encoding="utf-8")
        for table in (
            "jaytec_jobs",
            "jaytec_job_steps",
            "jaytec_operations",
            "jaytec_job_events",
            "jaytec_guardian_findings",
            "jaytec_task_packets",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema)
        self.assertNotIn("DROP TABLE", schema.upper())
        self.assertNotIn("TRUNCATE", schema.upper())

    def test_schema_has_unique_operation_idempotency(self):
        schema = Path(__file__).with_name("job_runtime_schema.sql").read_text(encoding="utf-8")
        self.assertIn("idempotency_key TEXT NOT NULL UNIQUE", schema)

    def test_schema_has_monotonic_fence_fields(self):
        schema = Path(__file__).with_name("job_runtime_schema.sql").read_text(encoding="utf-8")
        self.assertIn("ownership_epoch BIGINT NOT NULL DEFAULT 1", schema)
        self.assertIn("fence_token BIGINT NOT NULL DEFAULT 1", schema)
        self.assertIn("version BIGINT NOT NULL DEFAULT 0", schema)

    def test_schema_separates_retry_readiness_from_lease(self):
        schema = Path(__file__).with_name("job_runtime_schema.sql").read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ", schema)
        self.assertIn("jaytec_jobs_ready_idx", schema)

    def test_task_packet_queue_is_pollable_and_idempotent(self):
        schema = Path(__file__).with_name("job_runtime_schema.sql").read_text(encoding="utf-8")
        self.assertIn("packet_hash TEXT NOT NULL", schema)
        self.assertIn("idempotency_key TEXT NOT NULL UNIQUE", schema)
        self.assertIn("result JSONB", schema)
        self.assertIn("completed_at TIMESTAMPTZ", schema)


if __name__ == "__main__":
    unittest.main()
