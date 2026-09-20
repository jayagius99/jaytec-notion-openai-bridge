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
        pre_action,_,pre_tier,pre_reason=choose_cycle(before)
        if pre_action is CycleAction.HOLD:
            return CognitionStepResult(
                status="HOLD",
                action=pre_action.value,
                tier=pre_tier.value,
                elapsed_ms=0,
                detail=pre_reason,
            )
        if before.mode is not ForgeMode.RUNNING:
            raise ForgeCognitionError("FORGE_NOT_RUNNING")

        started=time.perf_counter()
        packet=self.store.prepare_cycle(forge_id,worker_id)
        token=int(packet["fencing_token"])
        expected_version=int(packet["state_version"])
        action=CycleAction(str(packet["selected_action"]))
        tier=ReasoningTier(str(packet["reasoning_tier"]))
        reason=str(packet.get("selection_reason") or "")
        try:
            result=None
            used_model_call=False
            if tier is ReasoningTier.REFLEX and self.reflex_executor is not None:
                result=self.reflex_executor.execute(packet)
            if result is None:
                used_model_call=True
                result=self.invoker.invoke(packet,tier=tier)
            if not isinstance(result,Mapping):
                raise ForgeCognitionError("COGNITION_INVOKER_RESULT_INVALID")
            normalized=dict(result)
            normalized.setdefault("schema_version",RESULT_VERSION)
            telemetry=dict(normalized.get("telemetry") or {})
            telemetry.setdefault("reasoning_tier",tier.value)
            telemetry.setdefault("selected_action",action.value)
            telemetry.setdefault("model_call_used",used_model_call)
            telemetry.setdefault("engine_precommit_elapsed_ms",int((time.perf_counter()-started)*1000))
            normalized["telemetry"]=telemetry
            committed=self.store.commit_cycle_result(
                forge_id,
                worker_id=worker_id,
                fencing_token=token,
                expected_state_version=expected_version,
                result=normalized,
                consumed_signal_ids=tuple(int(x) for x in packet.get("signal_ids", [])),
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


    def run_burst(
        self,
        forge_id: str,
        worker_id: str,
        *,
        max_cycles: int = 16,
        max_wall_seconds: float = 30.0,
    ) -> list[CognitionStepResult]:
        """Process consecutive runnable cognition cycles without cadence delay.

        The burst is deliberately bounded so a malformed goal loop cannot
        monopolize the worker. HOLD/wait modes stop the burst immediately.
        """
        if isinstance(max_cycles,bool) or not isinstance(max_cycles,int) or not 1 <= max_cycles <= 64:
            raise ForgeCognitionError("MAX_CYCLES_INVALID")
        if isinstance(max_wall_seconds,bool) or not isinstance(max_wall_seconds,(int,float)) or not 1.0 <= float(max_wall_seconds) <= 300.0:
            raise ForgeCognitionError("MAX_WALL_SECONDS_INVALID")
        started=time.perf_counter()
        out:list[CognitionStepResult]=[]
        for _ in range(max_cycles):
            if time.perf_counter()-started >= float(max_wall_seconds):
                break
            state=self.store.load_state(forge_id)
            if state.mode is not ForgeMode.RUNNING:
                break
            result=self.step(forge_id,worker_id)
            out.append(result)
            if result.status != "COMMITTED":
                break
        return out

    def wait_and_step(
        self,
        forge_id: str,
        worker_id: str,
        *,
        timeout_seconds: float = 300.0,
    ) -> CognitionStepResult:
        """Wait on PostgreSQL wake notification, then process immediately."""
        state=self.store.load_state(forge_id)
        if state.mode is ForgeMode.RUNNING:
            return self.step(forge_id,worker_id)
        if state.mode is not ForgeMode.WAITING_FOR_DEPENDENCY:
            return CognitionStepResult(
                status="HOLD",
                action=CycleAction.HOLD.value,
                tier=ReasoningTier.REFLEX.value,
                elapsed_ms=0,
                detail="MODE_HOLD:"+state.mode.value,
            )
        notice=self.store.wait_for_wake(forge_id,timeout_seconds=timeout_seconds)
        if notice is None:
            return CognitionStepResult(
                status="IDLE_TIMEOUT",
                action=CycleAction.HOLD.value,
                tier=ReasoningTier.REFLEX.value,
                elapsed_ms=0,
                detail="NO_WAKE_SIGNAL",
            )
        return self.step(forge_id,worker_id)
