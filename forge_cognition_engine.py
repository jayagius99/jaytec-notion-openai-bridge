"""Event-driven Forge cognition engine.

The engine is intentionally provider-neutral. Genesis wiring may bind an
approved Forge reasoning worker, while deterministic REFLEX work can bypass an
LLM entirely. The engine never activates Forge and never grants authority.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from forge_cognition import (
    CycleAction,
    ForgeCognitionError,
    ForgeMindStore,
    ForgeMode,
    ReasoningTier,
    RESULT_VERSION,
    choose_cycle,
)


class CognitionInvoker(Protocol):
    def invoke(self, packet: Mapping[str, Any], *, tier: ReasoningTier) -> Mapping[str, Any]:
        ...


class ReflexExecutor(Protocol):
    def execute(self, packet: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class CognitionStepResult:
    status: str
    action: str
    tier: str
    elapsed_ms: int
    committed_event_id: Optional[int] = None
    detail: str = ""


class ForgeCognitionEngine:
    def __init__(
        self,
        *,
        store: ForgeMindStore,
        invoker: CognitionInvoker,
        reflex_executor: Optional[ReflexExecutor] = None,
    ):
        self.store = store
        self.invoker = invoker
        self.reflex_executor = reflex_executor

    def step(self, forge_id: str, worker_id: str) -> CognitionStepResult:
        before=self.store.load_state(forge_id)
        action,goal,tier,reason=choose_cycle(before)
        if action is CycleAction.HOLD:
            return CognitionStepResult(
                status="HOLD",
                action=action.value,
                tier=tier.value,
                elapsed_ms=0,
                detail=reason,
            )
        if before.mode is not ForgeMode.RUNNING:
            raise ForgeCognitionError("FORGE_NOT_RUNNING")

        started=time.perf_counter()
        packet=self.store.prepare_cycle(forge_id,worker_id)
        token=int(packet["fencing_token"])
        expected_version=int(packet["state_version"])
        try:
            result=None
            if tier is ReasoningTier.REFLEX and self.reflex_executor is not None:
                result=self.reflex_executor.execute(packet)
            if result is None:
                result=self.invoker.invoke(packet,tier=tier)
            if not isinstance(result,Mapping):
                raise ForgeCognitionError("COGNITION_INVOKER_RESULT_INVALID")
            normalized=dict(result)
            normalized.setdefault("schema_version",RESULT_VERSION)
            committed=self.store.commit_cycle_result(
                forge_id,
                worker_id=worker_id,
                fencing_token=token,
                expected_state_version=expected_version,
                result=normalized,
            )
            elapsed=int((time.perf_counter()-started)*1000)
            return CognitionStepResult(
                status="COMMITTED",
                action=action.value,
                tier=tier.value,
                elapsed_ms=elapsed,
                committed_event_id=int(committed["event_id"]),
                detail=reason,
            )
        except Exception:
            # If commit already succeeded the lease is gone, so a stale release
            # fails safely and the original exception is preserved.
            try:
                self.store.release_cycle(
                    forge_id,
                    worker_id=worker_id,
                    fencing_token=token,
                )
            except Exception:
                pass
            raise
