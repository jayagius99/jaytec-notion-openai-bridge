import threading
import unittest

from autorecovery_loop import (
    AutoRecoveryLoop,
    AutoRecoveryLoopError,
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
    def test_interval_is_exactly_five_minutes(self):
        self.assertEqual(SUPERVISOR_INTERVAL_SECONDS, 300)
        with self.assertRaisesRegex(
            AutoRecoveryLoopError,
            "INTERVAL_MUST_BE_300_SECONDS",
        ):
            AutoRecoveryLoop(
                supervisor=FakeSupervisor(),
                task_source=lambda: [],
                interval_seconds=299,
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
