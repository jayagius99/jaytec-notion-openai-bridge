from __future__ import annotations

from typing import Any

from durable_tasks import should_cache_orchestration_result


class TransientAwareRegistry:
    """Delegate registry that refuses to replay/cache transient whole-packet failures.

    This also protects upgrades from transient TIMEOUT/RATE_LIMITED results that
    may have been written by the older synchronous runtime before hardening.
    """

    def __init__(self, delegate: Any):
        self.delegate = delegate

    def lookup(self, key, packet_hash, *, now=None):
        value = self.delegate.lookup(key, packet_hash, now=now)
        if value is not None and not should_cache_orchestration_result(value):
            return None
        return value

    def store(self, key, packet_hash, result, *, now=None):
        if should_cache_orchestration_result(result):
            return self.delegate.store(key, packet_hash, result, now=now)
        return None
