from __future__ import annotations

from typing import Any, Mapping

from durable_tasks import TRANSIENT_OVERALL_STATUSES, should_cache_orchestration_result


def transient_specialist_statuses(result: Mapping[str, Any]) -> set[str]:
    """Return transient statuses at packet or specialist level.

    A packet can be PARTIAL_SUCCESS while one specialist timed out or was rate
    limited. Such a result is not terminal for the durable reliability path and
    must never poison idempotent replay.
    """
    statuses: set[str] = set()
    overall = str(result.get("overall_status") or "")
    if overall in TRANSIENT_OVERALL_STATUSES:
        statuses.add(overall)
    for key in ("codex_result", "gemini_result"):
        child = result.get(key)
        if isinstance(child, Mapping):
            status = str(child.get("status") or "")
            if status in TRANSIENT_OVERALL_STATUSES:
                statuses.add(status)
    return statuses


def should_cache_reliable_result(result: Mapping[str, Any]) -> bool:
    return should_cache_orchestration_result(result) and not transient_specialist_statuses(result)


class TransientAwareRegistry:
    """Delegate registry that refuses to replay/cache any transient result.

    This protects both whole-packet TIMEOUT/RATE_LIMITED results and historical
    PARTIAL_SUCCESS rows that still contain a transient specialist result.
    """

    def __init__(self, delegate: Any):
        self.delegate = delegate

    def lookup(self, key, packet_hash, *, now=None):
        value = self.delegate.lookup(key, packet_hash, now=now)
        if value is not None and not should_cache_reliable_result(value):
            return None
        return value

    def store(self, key, packet_hash, result, *, now=None):
        if should_cache_reliable_result(result):
            return self.delegate.store(key, packet_hash, result, now=now)
        return None
