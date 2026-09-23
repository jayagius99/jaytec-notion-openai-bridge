from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping


class AdapterRegistryError(RuntimeError):
    pass


class AdapterExecutionError(RuntimeError):
    """Base adapter failure with explicit retry/side-effect semantics."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        side_effects_possible: bool,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.side_effects_possible = bool(side_effects_possible)
        self.retry_after_seconds = (
            None
            if retry_after_seconds is None
            else max(1, min(int(retry_after_seconds), 3600))
        )


class RetryableAdapterError(AdapterExecutionError):
    """Retryable only when the adapter guarantees no side effect occurred."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(
            message,
            retryable=True,
            side_effects_possible=False,
            retry_after_seconds=retry_after_seconds,
        )


class PermanentAdapterError(AdapterExecutionError):
    """Non-retryable failure with verified no-side-effect semantics."""

    def __init__(self, message: str):
        super().__init__(
            message,
            retryable=False,
            side_effects_possible=False,
        )


class UncertainSideEffectError(AdapterExecutionError):
    """Failure where an external effect may have happened. Never auto-retry."""

    def __init__(self, message: str):
        super().__init__(
            message,
            retryable=False,
            side_effects_possible=True,
        )


@dataclass(frozen=True)
class WorkerAdapter:
    worker_kind: str
    capabilities: frozenset[str]
    execute: Callable[[Mapping[str, Any]], Mapping[str, Any]]


class AdapterRegistry:
    def __init__(self):
        self._adapters: Dict[str, WorkerAdapter] = {}

    def register(
        self,
        worker_kind: str,
        *,
        capabilities: Iterable[str],
        execute: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        kind = str(worker_kind or "").strip().upper()
        if not kind:
            raise ValueError("worker_kind is required")
        if kind in self._adapters:
            raise AdapterRegistryError("duplicate_adapter:" + kind)
        caps = frozenset(str(item).strip() for item in capabilities if str(item).strip())
        self._adapters[kind] = WorkerAdapter(kind, caps, execute)

    def get(self, worker_kind: str) -> WorkerAdapter:
        kind = str(worker_kind or "").strip().upper()
        adapter = self._adapters.get(kind)
        if adapter is None:
            raise AdapterRegistryError("adapter_unavailable:" + kind)
        return adapter

    @property
    def supported_worker_kinds(self) -> frozenset[str]:
        return frozenset(self._adapters)

    @property
    def capabilities(self) -> frozenset[str]:
        result: set[str] = set()
        for adapter in self._adapters.values():
            result.update(adapter.capabilities)
        return frozenset(result)

    def can_run(self, worker_kind: str, required_capabilities: Iterable[str]) -> bool:
        try:
            adapter = self.get(worker_kind)
        except AdapterRegistryError:
            return False
        required = {str(item).strip() for item in required_capabilities if str(item).strip()}
        return required.issubset(adapter.capabilities)
