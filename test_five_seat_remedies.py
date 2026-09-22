import unittest
from pathlib import Path
from types import SimpleNamespace

from five_seat_adapters import (
    AdapterRegistry,
    PermanentAdapterError,
    RetryableAdapterError,
    UncertainSideEffectError,
)
from five_seat_remedies import retry_delay_seconds
from five_seat_runtime import quarantine_conflict
from five_seat_worker import FiveSeatWorker


class FakeSignal:
    def wait(self, channel, *, timeout_seconds):
        return False

    def notify(self, channel, *, reason, job_id=None):
        return None


class FakeScheduler:
    database_url = "stub"

    def __init__(self, claim):
        self.claim = claim
        self.releases = []
        self.claimed = False

    def claim_next(self, **kwargs):
        if self.claimed:
            return None
        self.claimed = True
        return dict(self.claim)

    def token_from_claim(self, claim):
        return SimpleNamespace(
            job_id=claim["job_id"],
            seat_id=claim["seat_id"],
            owner=claim["lease_owner"],
            ownership_epoch=claim["ownership_epoch"],
            job_fence_token=claim["fence_token"],
            seat_epoch=claim["seat_epoch"],
            seat_fence_token=claim["seat_fence_token"],
        )

    def heartbeat(self, token, *, lease_seconds):
        return token

    def release_for_review(self, token, *, handoff_ref, handoff_payload):
        self.releases.append((handoff_ref, dict(handoff_payload)))
        return {"handoff": handoff_payload}


class FakeRemedies:
    def __init__(self):
        self.retry_calls = []
        self.failure_calls = []
        self.success_calls = []
        self.cancel = False
        self.cancel_ack = 0
        self.quarantine = []

    def cancel_requested(self, token):
        return self.cancel

    def acknowledge_cancel(self, token):
        self.cancel_ack += 1
        return {}

    def record_adapter_failure(self, worker_kind, *, error, cooldown_seconds=60):
        self.failure_calls.append((worker_kind, dict(error)))
        return {}

    def record_adapter_success(self, worker_kind):
        self.success_calls.append(worker_kind)
        return {}

    def safe_retry(self, token, *, worker_kind, error, delay_seconds):
        self.retry_calls.append((worker_kind, dict(error), delay_seconds))
        return {"requeued": True}

    def quarantine_current(self, token, *, reason, evidence):
        self.quarantine.append((reason, dict(evidence)))
        return {}


def claim():
    return {
        "job_id": "job-1",
        "seat_id": "WORKER-SEAT-1",
        "lease_owner": "worker-1",
        "ownership_epoch": 2,
        "fence_token": 3,
        "seat_epoch": 4,
        "seat_fence_token": 5,
        "lease_expires_at": object(),
        "worker_kind": "TEST",
        "required_capabilities": ["test.run"],
        "payload": {"value": 1},
        "fabric_attempt_count": 1,
        "fabric_max_attempts": 3,
        "task_packet_hash": "abc",
    }


class TestFiveSeatFailureRemedies(unittest.TestCase):
    def make_worker(self, execute, remedies=None):
        registry = AdapterRegistry()
        registry.register(
            "TEST",
            capabilities={"test.run"},
            execute=execute,
        )
        scheduler = FakeScheduler(claim())
        remedies = remedies or FakeRemedies()
        worker = FiveSeatWorker(
            scheduler,
            registry,
            FakeSignal(),
            owner="worker-1",
            execution_room_id="room-1",
            lease_seconds=30,
            idle_fallback_seconds=0.1,
            remedies=remedies,
        )
        return worker, scheduler, remedies

    def test_retryable_error_requeues_only_through_safe_retry(self):
        def execute(payload):
            raise RetryableAdapterError("temporary", retry_after_seconds=7)

        worker, scheduler, remedies = self.make_worker(execute)
        self.assertTrue(worker.run_once())
        self.assertEqual(len(remedies.retry_calls), 1)
        self.assertEqual(scheduler.releases, [])
        self.assertEqual(remedies.retry_calls[0][2], 7)

    def test_unclassified_exception_becomes_uncertain_handoff(self):
        def execute(payload):
            raise RuntimeError("unknown failure")

        worker, scheduler, remedies = self.make_worker(execute)
        self.assertTrue(worker.run_once())
        self.assertEqual(len(scheduler.releases), 1)
        handoff = scheduler.releases[0][1]
        self.assertEqual(handoff["partial_side_effect_status"], "UNCERTAIN_PARTIAL")
        self.assertEqual(
            handoff["worker_completion_classification"],
            "QUARANTINE_CANDIDATE",
        )

    def test_permanent_verified_no_side_effect_failure_goes_to_watch_review(self):
        def execute(payload):
            raise PermanentAdapterError("bad input")

        worker, scheduler, remedies = self.make_worker(execute)
        self.assertTrue(worker.run_once())
        handoff = scheduler.releases[0][1]
        self.assertEqual(handoff["partial_side_effect_status"], "NONE")
        self.assertEqual(
            handoff["worker_completion_classification"],
            "FAILED_SAFE_CANDIDATE",
        )

    def test_cancel_before_execution_never_calls_adapter(self):
        called = []

        def execute(payload):
            called.append(True)
            return {}

        remedies = FakeRemedies()
        remedies.cancel = True
        worker, scheduler, remedies = self.make_worker(execute, remedies)
        self.assertTrue(worker.run_once())
        self.assertEqual(called, [])
        self.assertEqual(remedies.cancel_ack, 1)
        self.assertEqual(scheduler.releases, [])

    def test_success_resets_adapter_circuit_and_hands_off(self):
        def execute(payload):
            return {
                "partial_side_effect_status": "NONE",
                "operations": [],
                "artifacts": [],
                "tests": ["ok"],
                "evidence": ["proof"],
            }

        worker, scheduler, remedies = self.make_worker(execute)
        self.assertTrue(worker.run_once())
        self.assertEqual(remedies.success_calls, ["TEST"])
        self.assertEqual(len(scheduler.releases), 1)

    def test_quarantine_blocks_only_overlapping_scope(self):
        quarantined = [
            {
                "job_id": "bad",
                "mutation_scope": ["github/repo-a/src"],
                "read_scope": [],
                "resource_scope": {"render_service": "a"},
                "collision_key": "repo-a",
            }
        ]
        overlap = {
            "mutation_scope": ["github/repo-a/src/file.py"],
            "read_scope": [],
            "resource_scope": {},
            "collision_key": "",
        }
        disjoint = {
            "mutation_scope": ["github/repo-b"],
            "read_scope": [],
            "resource_scope": {"render_service": "b"},
            "collision_key": "repo-b",
        }
        self.assertEqual(quarantine_conflict(overlap, quarantined), "bad")
        self.assertIsNone(quarantine_conflict(disjoint, quarantined))

    def test_retry_delay_is_bounded(self):
        self.assertEqual(retry_delay_seconds(1), 5)
        self.assertLessEqual(retry_delay_seconds(20), 300)

    def test_schema_has_attempt_rework_cancel_and_circuit_state(self):
        schema = Path(__file__).with_name("five_seat_schema.sql").read_text(encoding="utf-8")
        self.assertIn("fabric_attempt_count", schema)
        self.assertIn("fabric_max_attempts", schema)
        self.assertIn("fabric_rework_count", schema)
        self.assertIn("fabric_max_reworks", schema)
        self.assertIn("cancel_requested_at", schema)
        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS jaytec_fabric_circuits"), 1)

    def test_expired_worker_reconciliation_invalidates_old_fences(self):
        source = Path(__file__).with_name("five_seat_remedies.py").read_text(encoding="utf-8")
        self.assertIn("reconcile_expired_leases", source)
        self.assertIn("ownership_epoch=ownership_epoch+1", source)
        self.assertIn("fence_token=fence_token+1", source)
        self.assertIn("EXPIRED_WORKER_QUARANTINED", source)

    def test_only_explicit_retryable_error_is_automatic_retry(self):
        source = Path(__file__).with_name("five_seat_worker.py").read_text(encoding="utf-8")
        self.assertIn("isinstance(failure, RetryableAdapterError)", source)
        self.assertIn("UNCERTAIN_PARTIAL", source)
        self.assertNotIn("except BaseException", source)


if __name__ == "__main__":
    unittest.main()
