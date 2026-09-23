import unittest
from datetime import datetime, timedelta, timezone

from five_seat_scheduler_diagnostic import _diagnose_rows


def target():
    return {
        "job_id": "fabric-9e729002f85e75f7de97ee01",
        "task_id": "FS08-PRODUCTION-ADMISSION-002",
        "assignment_type": "FIVE_SEAT_FABRIC",
        "status": "QUEUED",
        "fabric_state": "QUEUED",
        "seat_id": None,
        "lease_ready": True,
        "next_attempt_ready": True,
        "source_shared_state_version": 55,
        "fabric_attempt_count": 0,
        "fabric_max_attempts": 1,
        "concurrency_class": "A",
        "mutation_scope": [],
        "read_scope": ["specialist:codex"],
        "resource_scope": {"specialists": ["codex"]},
        "dependencies": [],
        "collision_key": "task-packet:FS08-PRODUCTION-ADMISSION-002",
    }


class SchedulerDiagnosticTests(unittest.TestCase):
    def test_closed_circuit_clean_target_has_no_mechanical_blocker(self):
        result = _diagnose_rows(
            target=target(),
            circuit={
                "worker_kind": "TASK_PACKET",
                "state": "CLOSED",
                "consecutive_failures": 0,
                "failure_threshold": 3,
            },
            quarantined=[],
            active=[],
            seats=[],
            authority={"current_shared_state_version": 55},
        )
        self.assertEqual(result["mechanical_blockers"], [])
        self.assertTrue(result["circuit_claim_eligible"])
        self.assertTrue(result["concurrency"]["allowed"])

    def test_overlapping_quarantine_blocks_only_matching_surface(self):
        blocked = {
            "job_id": "old-probe",
            "task_id": "FS08-PRODUCTION-ADMISSION-001",
            "assignment_type": "FIVE_SEAT_FABRIC",
            "status": "BLOCKED",
            "fabric_state": "QUARANTINED",
            "concurrency_class": "A",
            "mutation_scope": [],
            "read_scope": ["specialist:codex"],
            "resource_scope": {"specialists": ["codex"]},
            "dependencies": [],
            "collision_key": "task-packet:FS08-PRODUCTION-ADMISSION-001",
        }
        result = _diagnose_rows(
            target=target(),
            circuit={"worker_kind": "TASK_PACKET", "state": "CLOSED"},
            quarantined=[blocked],
            active=[],
            seats=[],
            authority={"current_shared_state_version": 55},
        )
        self.assertIn("OVERLAPPING_QUARANTINE", result["mechanical_blockers"])
        self.assertEqual(
            result["overlapping_quarantine"][0]["job_id"],
            "old-probe",
        )

    def test_half_open_circuit_is_mechanical_blocker(self):
        result = _diagnose_rows(
            target=target(),
            circuit={
                "worker_kind": "TASK_PACKET",
                "state": "HALF_OPEN",
                "probe_job_id": "stale-probe",
            },
            quarantined=[],
            active=[],
            seats=[],
            authority={"current_shared_state_version": 55},
        )
        self.assertIn("WORKER_KIND_CIRCUIT_BLOCK", result["mechanical_blockers"])
        self.assertFalse(result["circuit_claim_eligible"])

    def test_elapsed_open_circuit_is_eligible_for_bounded_half_open(self):
        result = _diagnose_rows(
            target=target(),
            circuit={
                "worker_kind": "TASK_PACKET",
                "state": "OPEN",
                "open_until": datetime.now(timezone.utc) - timedelta(seconds=5),
            },
            quarantined=[],
            active=[],
            seats=[],
            authority={"current_shared_state_version": 55},
        )
        self.assertNotIn(
            "WORKER_KIND_CIRCUIT_BLOCK",
            result["mechanical_blockers"],
        )
        self.assertTrue(result["circuit_open_window_elapsed"])


if __name__ == "__main__":
    unittest.main()
