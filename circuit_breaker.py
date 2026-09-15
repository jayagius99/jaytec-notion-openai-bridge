from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional


class CircuitOpenError(RuntimeError):
    pass


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    opened_at: Optional[datetime] = None


class CircuitBreaker:
    """Small fail-closed circuit breaker for one specialist transport.

    It opens only on transport/dispatcher exceptions. Model conclusions and
    policy-level failures are handled by the orchestration contract and do not
    count as transport failures here.
    """

    def __init__(self, *, failure_threshold: int = 3, reset_after_seconds: int = 60):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_after_seconds < 1:
            raise ValueError("reset_after_seconds must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_after_seconds = reset_after_seconds
        self._state = CircuitState()
        self._lock = threading.Lock()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def allow(self, *, now: Optional[datetime] = None) -> bool:
        current = (now or self._now()).astimezone(timezone.utc)
        with self._lock:
            opened = self._state.opened_at
            if opened is None:
                return True
            if (current - opened).total_seconds() >= self.reset_after_seconds:
                self._state = CircuitState()
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState()

    def record_failure(self, *, now: Optional[datetime] = None) -> None:
        current = (now or self._now()).astimezone(timezone.utc)
        with self._lock:
            self._state.consecutive_failures += 1
            if self._state.consecutive_failures >= self.failure_threshold:
                self._state.opened_at = current

    def snapshot(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "failure_threshold": self.failure_threshold,
                "reset_after_seconds": self.reset_after_seconds,
                "consecutive_failures": self._state.consecutive_failures,
                "opened_at": self._state.opened_at.isoformat() if self._state.opened_at else None,
                "open": self._state.opened_at is not None,
            }

    def guard(self, dispatcher: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        def guarded(packet: Mapping[str, Any]) -> Mapping[str, Any]:
            if not self.allow():
                raise CircuitOpenError("specialist circuit is open")
            try:
                result = dispatcher(packet)
            except Exception:
                self.record_failure()
                raise
            self.record_success()
            return result
        return guarded
