import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace

from durable_tasks_runtime import ReliableDurableTaskQueue, ReliableDurableTaskWorker
from reliability_registry import should_cache_reliable_result, transient_specialist_statuses


class _FakeCursor:
    def __init__(self, row):
        self.row = row
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.sql += str(sql)

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, *args, **kwargs):
        return self._cursor


class _SchemaQueue(ReliableDurableTaskQueue):
    def __init__(self, row):
        self.fake_cursor = _FakeCursor(row)

    def _connect(self):
        return _FakeConnection(self.fake_cursor)


class _AttemptBudgetQueue:
    def __init__(self):
        self.finished = None

    def claim_next(self, **kwargs):
        return {
            "job_id": "job-1",
            "attempt_count": 3,
            "max_attempts": 2,
            "packet": {"task_id": "t"},
        }

    def token_from_claim(self, claimed):
        return SimpleNamespace(job_id=claimed["job_id"])

    def finish(self, token, *, result, succeeded):
        self.finished = (token.job_id, dict(result), succeeded)
        return {"job_id": token.job_id}


class _PartialTransientQueue:
    def __init__(self, *, attempt_count=1, max_attempts=3):
        self.attempt_count = attempt_count
        self.max_attempts = max_attempts
        self.requeued = None
        self.finished = None
        self.incident = None

    def claim_next(self, **kwargs):
        return {
            "job_id": "job-partial",
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "packet": {"task_id": "t"},
        }

    def token_from_claim(self, claimed):
        return SimpleNamespace(job_id=claimed["job_id"])

    def requeue(self, token, *, error, delay_seconds):
        self.requeued = (token.job_id, dict(error), delay_seconds)
        return {"job_id": token.job_id}

    def finish(self, token, *, result, succeeded):
        self.finished = (token.job_id, dict(result), succeeded)
        return {"job_id": token.job_id}

    def record_incident(self, event_type, *, job_id, detail):
        self.incident = (event_type, job_id, dict(detail))
        return {}


class _ImmediateWorker(ReliableDurableTaskWorker):
    def _execute_with_heartbeat(self, token, packet):
        return dict(self.execute_packet("{}"))


class TestReliableRuntimeHardening(unittest.TestCase):
    def test_schema_gate_is_read_only_and_accepts_ready_schema(self):
        queue = _SchemaQueue(
            {
                "next_attempt_at": True,
                "task_packets": True,
                "jobs_ready_idx": True,
                "task_packets_status_idx": True,
            }
        )
        result = queue.verify_schema_ready()
        self.assertTrue(all(result.values()))
        sql = queue.fake_cursor.sql.upper()
        self.assertNotIn("ALTER TABLE", sql)
        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("DROP TABLE", sql)

    def test_schema_gate_fails_closed_when_migration_is_missing(self):
        queue = _SchemaQueue(
            {
                "next_attempt_at": True,
                "task_packets": False,
                "jobs_ready_idx": True,
                "task_packets_status_idx": False,
            }
        )
        with self.assertRaisesRegex(RuntimeError, "reliability_runtime_schema_not_ready"):
            queue.verify_schema_ready()

    def test_reclaimed_packet_cannot_execute_beyond_attempt_budget(self):
        queue = _AttemptBudgetQueue()
        calls = {"n": 0}

        def execute(_packet_json):
            calls["n"] += 1
            return {"overall_status": "SUCCESS"}

        worker = ReliableDurableTaskWorker(
            queue,
            execute,
            owner="worker",
            execution_room_id="room",
            poll_seconds=1,
            lease_seconds=30,
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(calls["n"], 0)
        self.assertIsNotNone(queue.finished)
        self.assertFalse(queue.finished[2])
        self.assertIn(
            "durable_retry_budget_exhausted_before_dispatch",
            queue.finished[1]["unresolved_items"],
        )

    def test_partial_success_with_transient_specialist_is_not_cacheable(self):
        result = {
            "overall_status": "PARTIAL_SUCCESS",
            "codex_result": {"status": "SUCCESS"},
            "gemini_result": {"status": "TIMEOUT"},
        }
        self.assertEqual(transient_specialist_statuses(result), {"TIMEOUT"})
        self.assertFalse(should_cache_reliable_result(result))

    def test_partial_success_with_transient_specialist_is_requeued(self):
        queue = _PartialTransientQueue(attempt_count=1, max_attempts=3)
        result = {
            "overall_status": "PARTIAL_SUCCESS",
            "codex_result": {"status": "SUCCESS"},
            "gemini_result": {"status": "TIMEOUT"},
            "unresolved_items": ["worker_timeout"],
        }
        worker = _ImmediateWorker(
            queue,
            lambda _packet_json: result,
            owner="worker",
            execution_room_id="room",
            poll_seconds=1,
            lease_seconds=30,
        )
        self.assertTrue(worker.run_once())
        self.assertIsNotNone(queue.requeued)
        self.assertIsNone(queue.finished)
        self.assertEqual(queue.incident[0], "SPECIALIST_TIMEOUT")

    def test_partial_transient_fails_closed_when_retry_budget_is_exhausted(self):
        queue = _PartialTransientQueue(attempt_count=3, max_attempts=3)
        result = {
            "overall_status": "PARTIAL_SUCCESS",
            "codex_result": {"status": "SUCCESS"},
            "gemini_result": {"status": "RATE_LIMITED"},
            "unresolved_items": ["retryable_rate_limit"],
        }
        worker = _ImmediateWorker(
            queue,
            lambda _packet_json: result,
            owner="worker",
            execution_room_id="room",
            poll_seconds=1,
            lease_seconds=30,
        )
        self.assertTrue(worker.run_once())
        self.assertIsNone(queue.requeued)
        self.assertIsNotNone(queue.finished)
        self.assertFalse(queue.finished[2])
        self.assertEqual(queue.finished[1]["overall_status"], "FAILED_CLOSED")
        self.assertIn(
            "transient_specialist_retry_budget_exhausted",
            queue.finished[1]["unresolved_items"],
        )

    def test_provider_wrapper_is_single_attempt_and_retryable(self):
        env = os.environ.copy()
        env.update(
            {
                "RUNTIME_MODE": "staging_candidate",
                "DURABLE_WORKER_ENABLED": "0",
                "GUARDIAN_LOOP_ENABLED": "0",
            }
        )
        code = r'''
import json
from orchestration import RateLimitError, ProviderUnavailableError
import reliable_server

counts = {"rate": 0, "provider": 0}

def rate(_packet):
    counts["rate"] += 1
    raise RateLimitError("limited")

def provider(_packet):
    counts["provider"] += 1
    raise ProviderUnavailableError("down")

rate_result = reliable_server._retryable_single_attempt_dispatch(
    rate, model="gpt-5.6-sol"
)({"max_retries": 3})
provider_result = reliable_server._retryable_single_attempt_dispatch(
    provider, model="google/gemini-3.1-pro-preview"
)({"max_retries": 3})
print(json.dumps({"counts": counts, "rate": rate_result, "provider": provider_result}))
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload["counts"], {"rate": 1, "provider": 1})
        self.assertEqual(payload["rate"]["status"], "RATE_LIMITED")
        self.assertEqual(payload["provider"]["status"], "RATE_LIMITED")
        self.assertIn("provider_unavailable", payload["provider"]["unresolved_items"][0])

    def test_single_instance_worker_pool_has_real_parallel_workers(self):
        env = os.environ.copy()
        env.update(
            {
                "RUNTIME_MODE": "staging_candidate",
                "DURABLE_WORKER_ENABLED": "0",
                "GUARDIAN_LOOP_ENABLED": "0",
            }
        )
        code = r'''
import json
import reliable_server

class Queue:
    pass

pool = reliable_server.ReliableDurableWorkerPool(
    Queue(),
    lambda _packet: {},
    instance_id="instance",
    worker_count=4,
    poll_seconds=1,
    lease_seconds=30,
)
print(json.dumps({
    "count": pool.worker_count,
    "owners": [w.owner for w in pool.workers],
    "rooms": [w.execution_room_id for w in pool.workers],
}))
'''
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload["count"], 4)
        self.assertEqual(len(set(payload["owners"])), 4)
        self.assertEqual(len(set(payload["rooms"])), 4)


if __name__ == "__main__":
    unittest.main()
