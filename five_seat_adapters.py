from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping


class AdapterRegistryError(RuntimeError):
    pass


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
