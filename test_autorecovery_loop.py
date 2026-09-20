import threading
import unittest

from autorecovery_loop import (
    AutoRecoveryLoop,
    AutoRecoveryLoopError,
    SUPERVISOR_INTERVAL_MINUTES,
    SUPERVISOR_INTERVAL_SECONDS,
)
from autorecovery_supervisor import (
    RecoveryDecision,
    RecoveryRoute,
    StopReason,
    SupervisorAction,
)


class FakeSupervisor:
    def __init__(self, decisions=None, fail_task=None):
        self.decisions = decisions or {}
        self.fail_task = fail_task
        self.calls = []

    def tick(self, task_id):
        self.calls.append(task_id)
        if task_id == self.fail_task:
            raise RuntimeError("boom")
        return self.decisions.get(
            task_id,
            RecoveryDecision(
                SupervisorAction.NOOP_HEALTHY,
                StopReason.RUNNING,
                "HEALTHY_WORKER_HEARTBEAT",
            ),
        )


class LoopTests(unittest.TestCase):
    def test_interval_is_exactly_fifteen_minutes(self):
        self.assertEqual(SUPERVISOR_INTERVAL_MINUTES, 15)
        self.assertEqual(SUPERVISOR_INTERVAL_SECONDS, 900)
        with self.assertRaisesRegex(
            AutoRecoveryLoopError,
            "INTERVAL_MUST_MATCH_15_MINUTE_CADENCE",
        ):
            AutoRecoveryLoop(
                supervisor=FakeSupervisor(),
                task_source=lambda: [],
                interval_seconds=899,
            )

    def test_cycle_deduplicates_task_ids(self):
        sup = FakeSupervisor()
        loop = AutoRecoveryLoop(
            supervisor=sup,
            task_source=lambda: ["task-a", "task-a", "", "task-b"],
        )
        results = loop.run_cycle()
        self.assertEqual(sup.calls, ["task-a", "task-b"])
        self.assertEqual([r.task_id for r in results], ["task-a", "task-b"])

    def test_task_failure_does_not_stop_other_assignments(self):
        sup = FakeSupervisor(fail_task="task-a")
        loop = AutoRecoveryLoop(
            supervisor=sup,
            task_source=lambda: ["task-a", "task-b"],
        )
        results = loop.run_cycle()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].action, SupervisorAction.NOTIFY_JAY.value)
        self.assertEqual(results[1].action, SupervisorAction.NOOP_HEALTHY.value)

    def test_recovery_result_is_reported_without_second_invocation(self):
        sup = FakeSupervisor(
            decisions={
                "task-a": RecoveryDecision(
                    SupervisorAction.RECOVERY_STARTED,
                    StopReason.RUNNING,
                    "RECOVERY_WORKER_HEALTHY",
                    recovery_route=RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT,
                )
            }
        )
        events = []
        loop = AutoRecoveryLoop(
            supervisor=sup,
            task_source=lambda: ["task-a"],
            event_sink=events.append,
        )
        results = loop.run_cycle()
        self.assertEqual(sup.calls, ["task-a"])
        self.assertEqual(results[0].action, SupervisorAction.RECOVERY_STARTED.value)
        self.assertEqual(
            results[0].recovery_route,
            RecoveryRoute.FRESH_WORKER_SAME_CHECKPOINT.value,
        )
        self.assertEqual(events[-1]["event"], "JAYTEC_AUTORECOVERY_CYCLE_RESULT")

    def test_overlapping_local_cycle_is_skipped(self):
        sup = FakeSupervisor()
        events = []
        loop = AutoRecoveryLoop(
            supervisor=sup,
            task_source=lambda: ["task-a"],
            event_sink=events.append,
        )
        acquired = loop._cycle_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            self.assertEqual(loop.run_cycle(), ())
        finally:
            loop._cycle_lock.release()
        self.assertEqual(
            events[-1]["reason"],
            "LOCAL_CYCLE_ALREADY_RUNNING",
        )

    def test_one_hundred_cycles_hold_exact_fifteen_minute_start_cadence(self):
        class FakeClock:
            def __init__(self):
                self.value = 0.0

            def monotonic(self):
                return self.value

        class AdvancingSupervisor(FakeSupervisor):
            def __init__(self, clock):
                super().__init__()
                self.clock = clock

            def tick(self, task_id):
                self.clock.value += 37.0
                return super().tick(task_id)

        class CountingStop:
            def __init__(self, clock, waits):
                self.clock = clock
                self.waits = waits

            def is_set(self):
                return False

            def wait(self, seconds):
                self.waits.append(seconds)
                self.clock.value += seconds
                return len(self.waits) >= 100

        clock = FakeClock()
        waits = []
        sup = AdvancingSupervisor(clock)
        loop = AutoRecoveryLoop(
            supervisor=sup,
            task_source=lambda: ["task-a"],
        )
        loop.run_forever(
            stop_event=CountingStop(clock, waits),
            monotonic_fn=clock.monotonic,
        )

        self.assertEqual(len(sup.calls), 100)
        self.assertEqual(len(waits), 100)
        self.assertTrue(all(wait == 863.0 for wait in waits))
        self.assertEqual(clock.value, 100 * SUPERVISOR_INTERVAL_SECONDS)

    def test_run_forever_runs_immediately_and_can_stop_after_first_cycle(self):
        sup = FakeSupervisor()
        stop = threading.Event()

        class StoppingSource:
            def __call__(self):
                stop.set()
                return ["task-a"]

        loop = AutoRecoveryLoop(
            supervisor=sup,
            task_source=StoppingSource(),
        )
        loop.run_forever(stop_event=stop)
        self.assertEqual(sup.calls, ["task-a"])


if __name__ == "__main__":
    unittest.main()
