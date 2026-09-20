"""Fifteen-minute runner for JAYTEC WATCH + AUTORECOVERY.

The loop is deliberately generic: it needs an already-constructed supervisor
whose verifier, invoker, health probe and notifier have passed runtime gates.
This module does not create those authorities itself.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from autorecovery_supervisor import (
    AutoRecoverySupervisor,
    RecoveryDecision,
    SupervisorAction,
)

SUPERVISOR_INTERVAL_MINUTES = 15
SUPERVISOR_INTERVAL_SECONDS = SUPERVISOR_INTERVAL_MINUTES * 60
MAX_TASKS_PER_CYCLE = 128


class AutoRecoveryLoopError(RuntimeError):
    pass


@dataclass(frozen=True)
class CycleResult:
    task_id: str
    action: str
    stop_reason: str
    reason: str
    recovery_route: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_task_ids(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        task_id = str(raw or "").strip()
        if not task_id:
            continue
        if task_id in seen:
            continue
        seen.add(task_id)
        result.append(task_id)
        if len(result) > MAX_TASKS_PER_CYCLE:
            raise AutoRecoveryLoopError("TOO_MANY_TASKS")
    return tuple(result)


class AutoRecoveryLoop:
    """Runs one bounded supervisor decision per canonical task every 900s.

    Cross-process duplicate recovery remains safe because the supervisor must
    acquire the durable recovery lease/fencing token before invoking a worker.
    """

    def __init__(
        self,
        *,
        supervisor: AutoRecoverySupervisor,
        task_source: Callable[[], Iterable[str]],
        interval_seconds: int = SUPERVISOR_INTERVAL_SECONDS,
        event_sink: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ):
        if interval_seconds != SUPERVISOR_INTERVAL_SECONDS:
            raise AutoRecoveryLoopError(
                "INTERVAL_MUST_MATCH_15_MINUTE_CADENCE"
            )
        self.supervisor = supervisor
        self.task_source = task_source
        self.interval_seconds = interval_seconds
        self.event_sink = event_sink
        self._cycle_lock = threading.Lock()

    def _emit(self, payload: Mapping[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(dict(payload))

    def run_cycle(self) -> tuple[CycleResult, ...]:
        if not self._cycle_lock.acquire(blocking=False):
            self._emit(
                {
                    "event": "JAYTEC_AUTORECOVERY_CYCLE_SKIPPED",
                    "reason": "LOCAL_CYCLE_ALREADY_RUNNING",
                }
            )
            return ()
        try:
            try:
                task_ids = _normalize_task_ids(self.task_source())
            except Exception as exc:
                self._emit(
                    {
                        "event": "JAYTEC_AUTORECOVERY_TASK_SOURCE_FAILED",
                        "reason": type(exc).__name__,
                    }
                )
                return ()

            results: list[CycleResult] = []
            for task_id in task_ids:
                try:
                    self.supervisor.refresh_worker_health(task_id)
                    decision = self.supervisor.tick(task_id)
                    result = CycleResult(
                        task_id=task_id,
                        action=decision.action.value,
                        stop_reason=decision.effective_stop_reason.value,
                        reason=decision.reason,
                        recovery_route=(
                            decision.recovery_route.value
                            if decision.recovery_route
                            else None
                        ),
                    )
                except Exception as exc:
                    # A task-specific failure must not stop supervision of the
                    # remaining assignments. The task fails closed and is
                    # surfaced for diagnosis.
                    result = CycleResult(
                        task_id=task_id,
                        action=SupervisorAction.NOTIFY_JAY.value,
                        stop_reason="UNKNOWN",
                        reason="SUPERVISOR_TICK_EXCEPTION:" + type(exc).__name__,
                        recovery_route=None,
                    )
                results.append(result)
                self._emit(
                    {
                        "event": "JAYTEC_AUTORECOVERY_CYCLE_RESULT",
                        **result.to_dict(),
                    }
                )
            return tuple(results)
        finally:
            self._cycle_lock.release()

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        """Run immediately, then start each next cycle ~900s after prior start."""

        while not stop_event.is_set():
            started = monotonic_fn()
            self.run_cycle()
            elapsed = max(0.0, monotonic_fn() - started)
            wait_for = max(0.0, self.interval_seconds - elapsed)
            if stop_event.wait(wait_for):
                return


__all__ = [
    "AutoRecoveryLoop",
    "AutoRecoveryLoopError",
    "CycleResult",
    "MAX_TASKS_PER_CYCLE",
    "SUPERVISOR_INTERVAL_MINUTES",
    "SUPERVISOR_INTERVAL_SECONDS",
]
