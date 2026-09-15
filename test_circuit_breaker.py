import unittest
from datetime import datetime, timedelta, timezone

from circuit_breaker import CircuitBreaker, CircuitOpenError


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold_and_blocks(self):
        breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=60)
        calls = {"n": 0}

        def failing(_):
            calls["n"] += 1
            raise RuntimeError("boom")

        guarded = breaker.guard(failing)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                guarded({})
        self.assertTrue(breaker.snapshot()["open"])
        with self.assertRaises(CircuitOpenError):
            guarded({})
        self.assertEqual(2, calls["n"])

    def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=60)
        breaker.record_failure()
        breaker.record_success()
        snap = breaker.snapshot()
        self.assertEqual(0, snap["consecutive_failures"])
        self.assertFalse(snap["open"])

    def test_half_open_after_reset_window(self):
        breaker = CircuitBreaker(failure_threshold=1, reset_after_seconds=10)
        t0 = datetime(2026, 9, 15, tzinfo=timezone.utc)
        breaker.record_failure(now=t0)
        self.assertFalse(breaker.allow(now=t0 + timedelta(seconds=9)))
        self.assertTrue(breaker.allow(now=t0 + timedelta(seconds=10)))
        self.assertFalse(breaker.snapshot()["open"])


if __name__ == "__main__":
    unittest.main()
