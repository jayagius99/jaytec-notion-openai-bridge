from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Mapping, Optional

from five_seat_adapters import AdapterRegistry
from five_seat_runtime import FiveSeatStaleLease, PostgresFiveSeatScheduler
from five_seat_signals import (
    PostgresFabricSignal,
    REVIEW_AVAILABLE_CHANNEL,
    WORK_AVAILABLE_CHANNEL,
)


class FabricWorkerError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _handoff_id(job_id: str, fence_token: int, result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_json(dict(result)).encode("utf-8")).hexdigest()
    return f"handoff-{job_id}-{fence_token}-{digest[:16]}"


def _handoff_payload(claim: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_packet_hash": claim.get("task_packet_hash"),
        "starting_checkpoint": claim.get("checkpoint_ref"),
        "operations": list(result.get("operations") or []),
        "artifacts": list(result.get("artifacts") or []),
        "tests": list(result.get("tests") or []),
        "evidence": list(result.get("evidence") or []),
        "provider_identity": result.get("provider_identity"),
        "unresolved_items": list(result.get("unresolved_items") or []),
        "partial_side_effect_status": str(
            result.get("partial_side_effect_status") or "NONE"
        ),
        "proposed_next_action": result.get("proposed_next_action"),
        "worker_completion_classification": str(
            result.get("worker_completion_classification") or "CANDIDATE_COMPLETE"
        ),
        "result": dict(result),
    }


class FiveSeatWorker:
    """Generic worker loop over the durable five-seat scheduler.

    Work is chained immediately. LISTEN/NOTIFY only accelerates idle wakeups;
    a bounded timeout always re-checks Postgres so lost signals cannot stall.
    """

    def __init__(
        self,
        scheduler: PostgresFiveSeatScheduler,
        registry: AdapterRegistry,
        signal: PostgresFabricSignal,
        *,
        owner: str,
        execution_room_id: str,
        lease_seconds: int = 300,
        idle_fallback_seconds: float = 15.0,
    ):
        self.scheduler = scheduler
        self.registry = registry
        self.signal = signal
        self.owner = owner
        self.execution_room_id = execution_room_id
        self.lease_seconds = lease_seconds
        self.idle_fallback_seconds = max(0.1, min(float(idle_fallback_seconds), 60.0))
        self._stop = threading.Event()

    def run_once(self) -> bool:
        claim = self.scheduler.claim_next(
            owner=self.owner,
            execution_room_id=self.execution_room_id,
            lease_seconds=self.lease_seconds,
            supported_worker_kinds=self.registry.supported_worker_kinds,
            capabilities=self.registry.capabilities,
        )
        if claim is None:
            return False

        token = self.scheduler.token_from_claim(claim)
        worker_kind = str(claim.get("worker_kind") or "").upper()
        required_capabilities = list(claim.get("required_capabilities") or [])
        if not self.registry.can_run(worker_kind, required_capabilities):
            raise FabricWorkerError("claimed_incompatible_adapter:" + worker_kind)
        adapter = self.registry.get(worker_kind)

        stop_heartbeat = threading.Event()
        lost_lease = threading.Event()
        interval = max(2.0, min(30.0, float(self.lease_seconds) / 3.0))

        def keepalive() -> None:
            current = token
            while not stop_heartbeat.wait(interval):
                try:
                    current = self.scheduler.heartbeat(
                        current,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    lost_lease.set()
                    return

        heartbeat = threading.Thread(
            target=keepalive,
            name=f"five-seat-heartbeat-{token.seat_id}-{token.job_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = adapter.execute(dict(claim.get("payload") or {}))
            if not isinstance(result, Mapping):
                raise FabricWorkerError("adapter_result_not_mapping")
            result = dict(result)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1.0)

        if lost_lease.is_set():
            # Stale worker authority is dead. Never hand off from a lost fence.
            return True

        handoff_id = _handoff_id(
            token.job_id,
            token.job_fence_token,
            result,
        )
        self.scheduler.release_for_review(
            token,
            handoff_ref=handoff_id,
            handoff_payload=_handoff_payload(claim, result),
        )
        self.signal.notify(
            REVIEW_AVAILABLE_CHANNEL,
            reason="worker_handoff_ready",
            job_id=token.job_id,
        )
        return True

    def run_until_idle(self, *, max_jobs: int = 100) -> int:
        """Chain ready work without an intentional pause."""
        completed = 0
        while completed < max(1, int(max_jobs)) and not self._stop.is_set():
            if not self.run_once():
                break
            completed += 1
        return completed

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if self.run_once():
                # Immediate refill: no sleep after a completed handoff.
                continue
            self.signal.wait(
                WORK_AVAILABLE_CHANNEL,
                timeout_seconds=self.idle_fallback_seconds,
            )

    def stop(self) -> None:
        self._stop.set()
