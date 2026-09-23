from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Mapping

from five_seat_adapters import (
    AdapterExecutionError,
    AdapterRegistry,
    PermanentAdapterError,
    RetryableAdapterError,
    UncertainSideEffectError,
)
from five_seat_remedies import PostgresFabricRemedies, retry_delay_seconds
from five_seat_runtime import FiveSeatStaleLease, PostgresFiveSeatScheduler
from five_seat_signals import PostgresFabricSignal, WORK_AVAILABLE_CHANNEL


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
        ).upper(),
        "proposed_next_action": result.get("proposed_next_action"),
        "worker_completion_classification": str(
            result.get("worker_completion_classification") or "CANDIDATE_COMPLETE"
        ),
        "result": dict(result),
    }


def _failure_result(
    *,
    error_class: str,
    message: str,
    partial_side_effect_status: str,
    classification: str,
) -> dict[str, Any]:
    return {
        "operations": [],
        "artifacts": [],
        "tests": [],
        "evidence": [
            {
                "type": "worker_failure",
                "error_class": str(error_class),
                "message": str(message)[:1000],
            }
        ],
        "provider_identity": None,
        "unresolved_items": [f"worker_failure:{error_class}"],
        "partial_side_effect_status": partial_side_effect_status,
        "proposed_next_action": "WATCH_REVIEW_FAILURE",
        "worker_completion_classification": classification,
    }


class FiveSeatWorker:
    """Generic continuous worker over the durable five-seat scheduler.

    Automatic retries are allowed only for an explicit RetryableAdapterError,
    which guarantees no side effect occurred. All ambiguous failures are
    handed off as UNCERTAIN_PARTIAL and quarantined by the scheduler.
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
        remedies: PostgresFabricRemedies | None = None,
    ):
        self.scheduler = scheduler
        self.registry = registry
        self.signal = signal
        self.remedies = remedies or PostgresFabricRemedies(scheduler.database_url)
        self.owner = owner
        self.execution_room_id = execution_room_id
        self.lease_seconds = lease_seconds
        self.idle_fallback_seconds = max(0.1, min(float(idle_fallback_seconds), 60.0))
        self._stop = threading.Event()

    def _handoff(
        self,
        token,
        claim: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
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
            # Admission/registry mismatch is an internal safety fault. Do not
            # leave the task running or attempt another adapter.
            self.remedies.quarantine_current(
                token,
                reason="CLAIMED_INCOMPATIBLE_ADAPTER",
                evidence={
                    "worker_kind": worker_kind,
                    "required_capabilities": required_capabilities,
                },
            )
            return True
        adapter = self.registry.get(worker_kind)

        if self.remedies.cancel_requested(token):
            self.remedies.acknowledge_cancel(token)
            return True

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

        result: dict[str, Any] | None = None
        failure: Exception | None = None
        try:
            adapter_payload = dict(claim.get("payload") or {})
            blockers = claim.get("blockers") or []
            if isinstance(blockers, str):
                try:
                    blockers = json.loads(blockers)
                except json.JSONDecodeError:
                    blockers = []
            dan_context = None
            if isinstance(blockers, list):
                for item in reversed(blockers):
                    if isinstance(item, Mapping) and item.get("source") == "DAN_RECOVERY":
                        dan_context = dict(item)
                        break
            if dan_context is not None:
                adapter_payload["_dan_recovery_context"] = dan_context
                packet_json = adapter_payload.get("packet_json")
                if isinstance(packet_json, str) and packet_json.strip():
                    try:
                        packet = json.loads(packet_json)
                    except json.JSONDecodeError:
                        packet = None
                    if isinstance(packet, dict):
                        required_context = packet.get("required_context")
                        required_context = dict(required_context) if isinstance(required_context, Mapping) else {}
                        required_context["dan_recovery"] = dan_context
                        packet["required_context"] = required_context
                        original_key = str(packet.get("idempotency_key") or "")
                        receipt = str(dan_context.get("response_digest") or dan_context.get("request_digest") or "")
                        packet["idempotency_key"] = "dan-recovery-" + hashlib.sha256(
                            (original_key + "|" + receipt).encode("utf-8")
                        ).hexdigest()[:40]
                        adapter_payload["packet_json"] = json.dumps(packet, ensure_ascii=False, sort_keys=True)
            # Runtime-owned context is overwritten unconditionally so queued
            # payload cannot forge its fence/scope/seat authority.
            adapter_payload["_fabric_context"] = {
                "job_id": claim.get("job_id"),
                "job_fence_token": token.job_fence_token,
                "seat_id": token.seat_id,
                "seat_fence_token": token.seat_fence_token,
                "mutation_scope": list(claim.get("mutation_scope") or []),
                "read_scope": list(claim.get("read_scope") or []),
                "resource_scope": dict(claim.get("resource_scope") or {}),
                "authority_class": claim.get("authority_class"),
            }
            raw = adapter.execute(adapter_payload)
            if not isinstance(raw, Mapping):
                raise UncertainSideEffectError("adapter_result_not_mapping")
            result = dict(raw)
        except Exception as exc:
            failure = exc
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1.0)

        if lost_lease.is_set():
            # A replacement/reconciler owns the lineage now. Never write a
            # result, retry, or handoff from the stale token.
            return True

        if failure is not None:
            error = {
                "error_class": type(failure).__name__,
                "message": str(failure)[:1000],
            }
            self.remedies.record_adapter_failure(worker_kind, error=error)

            if isinstance(failure, RetryableAdapterError):
                delay = (
                    failure.retry_after_seconds
                    if failure.retry_after_seconds is not None
                    else retry_delay_seconds(int(claim.get("fabric_attempt_count") or 1))
                )
                retry = self.remedies.safe_retry(
                    token,
                    worker_kind=worker_kind,
                    error=error,
                    delay_seconds=delay,
                )
                if retry.get("requeued") is True:
                    return True
                if retry.get("reason") == "CANCEL_REQUESTED":
                    self.remedies.acknowledge_cancel(token)
                    return True
                partial = (
                    "UNCERTAIN_PARTIAL"
                    if retry.get("reason") == "UNRESOLVED_SIDE_EFFECT"
                    else "NONE"
                )
                result = _failure_result(
                    error_class=type(failure).__name__,
                    message=str(failure),
                    partial_side_effect_status=partial,
                    classification="RETRY_NOT_SAFE_OR_BUDGET_EXHAUSTED",
                )
            elif isinstance(failure, PermanentAdapterError):
                result = _failure_result(
                    error_class=type(failure).__name__,
                    message=str(failure),
                    partial_side_effect_status="NONE",
                    classification="FAILED_SAFE_CANDIDATE",
                )
            else:
                # UncertainSideEffectError and every unclassified exception are
                # deliberately treated as potentially side-effecting.
                result = _failure_result(
                    error_class=type(failure).__name__,
                    message=str(failure),
                    partial_side_effect_status="UNCERTAIN_PARTIAL",
                    classification="QUARANTINE_CANDIDATE",
                )

            try:
                self._handoff(token, claim, result)
            except FiveSeatStaleLease:
                return True
            return True

        assert result is not None
        partial = str(result.get("partial_side_effect_status") or "NONE").upper()
        if partial not in {
            "NONE",
            "VERIFIED_COMPLETE",
            "VERIFIED_NOT_DONE",
            "UNCERTAIN_PARTIAL",
        }:
            partial = "UNCERTAIN_PARTIAL"
            result["partial_side_effect_status"] = partial
            unresolved = list(result.get("unresolved_items") or [])
            unresolved.append("invalid_partial_side_effect_status")
            result["unresolved_items"] = unresolved

        if self.remedies.cancel_requested(token):
            # Preserve what actually happened; cancellation arriving during
            # execution cannot erase evidence. release_for_review sees the
            # CANCEL_REQUESTED state and quarantines it for reconciliation.
            unresolved = list(result.get("unresolved_items") or [])
            unresolved.append("cancel_requested_during_execution")
            result["unresolved_items"] = unresolved
            result["worker_completion_classification"] = (
                "CANCEL_REQUESTED_AFTER_EXECUTION"
            )

        if partial == "UNCERTAIN_PARTIAL":
            self.remedies.record_adapter_failure(
                worker_kind,
                error={
                    "error_class": "UNCERTAIN_PARTIAL_RESULT",
                    "message": "adapter returned uncertain partial side-effect state",
                },
            )
        else:
            self.remedies.record_adapter_success(worker_kind)

        try:
            self._handoff(token, claim, result)
        except FiveSeatStaleLease:
            return True
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
            try:
                worked = self.run_once()
            except Exception:
                # Do not kill the persistent worker pool on one internal fault,
                # and do not hot-loop. Durable state/leases remain the authority
                # for Guardian/recovery.
                self._stop.wait(self.idle_fallback_seconds)
                continue
            if worked:
                # Immediate refill: no sleep after a completed handoff.
                continue
            self.signal.wait(
                WORK_AVAILABLE_CHANNEL,
                timeout_seconds=self.idle_fallback_seconds,
            )

    def stop(self) -> None:
        self._stop.set()
