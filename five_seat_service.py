from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from typing import Any, Callable, Mapping

import psycopg2

from durable_tasks import SUCCESS_OVERALL_STATUSES
from five_seat_adapters import AdapterRegistry, RetryableAdapterError
from five_seat_guardian import FiveSeatGuardian
from five_seat_reporting import FiveSeatReporter
from five_seat_runtime import PostgresFiveSeatScheduler
from five_seat_signals import PostgresFabricSignal
from five_seat_watch import PostgresWatchController, WatchStaleLeader
from five_seat_worker import FiveSeatWorker
from reliability_registry import transient_specialist_statuses


TASK_PACKET_WORKER_KIND = "TASK_PACKET"
TASK_PACKET_CAPABILITY = "jaytec.task_packet.execute"
FABRIC_SERVICE_ID = "JAYTEC_FIVE_SEAT_PRODUCTION_HOST_V1"
WATCH_OWNER_PREFIX = "five-seat-watch"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _provider_identity(result: Mapping[str, Any]) -> str:
    observed: list[str] = []
    for key in ("codex_result", "gemini_result", "sol_result"):
        child = result.get(key)
        if isinstance(child, Mapping):
            model = str(child.get("model") or "").strip()
            if model and model not in observed:
                observed.append(model)
    model = str(result.get("model") or "").strip()
    if model and model not in observed:
        observed.append(model)
    return ",".join(observed) if observed else "JAYTEC_EXISTING_SPECIALIST_EXECUTOR"


def build_task_packet_adapter(
    execute_packet: Callable[[str], Mapping[str, Any]],
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Adapt the existing specialist executor to one bounded five-seat worker kind.

    Provider/model selection stays inside the existing JAYTEC executor. The
    five-seat layer owns only durable admission, seat/fence lifecycle, retry
    classification, handoff and WATCH review.
    """

    def execute(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        packet_json = payload.get("packet_json")
        if not isinstance(packet_json, str) or not packet_json.strip():
            raise RuntimeError("TASK_PACKET_PAYLOAD_MISSING")
        raw = execute_packet(packet_json)
        if not isinstance(raw, Mapping):
            raise RuntimeError("TASK_PACKET_RESULT_NOT_MAPPING")
        result = dict(raw)

        transient = transient_specialist_statuses(result)
        if transient:
            raise RetryableAdapterError(
                "transient_specialist_status:" + ",".join(sorted(transient))
            )

        overall = str(result.get("overall_status") or "FAILED_CLOSED").upper()
        unresolved = [str(item)[:1000] for item in list(result.get("unresolved_items") or [])[:20]]
        if overall not in SUCCESS_OVERALL_STATUSES and not unresolved:
            unresolved.append("whole_packet_status:" + overall)

        digest = hashlib.sha256(_canonical(result)).hexdigest()
        return {
            "operations": [],
            "artifacts": [],
            "tests": [
                {
                    "name": "existing-specialist-executor-returned",
                    "result": "PASS",
                    "overall_status": overall,
                }
            ],
            "evidence": [
                {
                    "type": "existing_specialist_executor_result",
                    "result_sha256": digest,
                    "overall_status": overall,
                }
            ],
            "provider_identity": _provider_identity(result),
            "unresolved_items": unresolved,
            "partial_side_effect_status": "NONE",
            "proposed_next_action": "WATCH_REVIEW",
            "worker_completion_classification": (
                "CANDIDATE_COMPLETE"
                if overall == "SUCCESS" and not unresolved
                else "CANDIDATE_NEEDS_REVIEW"
            ),
            "whole_packet_status": overall,
            "whole_packet_result": result,
            "result_sha256": digest,
        }

    return execute


class FiveSeatFabricService:
    """Continuous five-seat host around the existing JAYTEC specialist executor."""

    def __init__(
        self,
        database_url: str,
        execute_packet: Callable[[str], Mapping[str, Any]],
        *,
        instance_id: str | None = None,
        lease_seconds: int = 300,
        guardian_interval_seconds: float = 30.0,
        report_interval_seconds: float = 3600.0,
    ):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.instance_id = (
            str(instance_id or "").strip() or socket.gethostname()
        )
        self.lease_seconds = max(30, min(int(lease_seconds), 3600))
        self.guardian_interval_seconds = max(5.0, float(guardian_interval_seconds))
        self.report_interval_seconds = max(60.0, float(report_interval_seconds))

        self.scheduler = PostgresFiveSeatScheduler(database_url)
        self.signal = PostgresFabricSignal(database_url)
        self.watch = PostgresWatchController(database_url)
        self.guardian = FiveSeatGuardian(database_url)
        self.reporter = FiveSeatReporter(database_url)
        self.registry = AdapterRegistry()
        self.registry.register(
            TASK_PACKET_WORKER_KIND,
            capabilities={TASK_PACKET_CAPABILITY},
            execute=build_task_packet_adapter(execute_packet),
        )

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._workers: list[FiveSeatWorker] = []
        self._errors: list[str] = []
        self._started = False

    def verify_ready(self) -> dict[str, bool]:
        """Read-only startup gate. Never creates or migrates schema."""
        return self.scheduler.verify_schema_ready()

    def start(self) -> None:
        if self._started:
            return
        self.verify_ready()
        self._stop.clear()

        for index in range(1, 6):
            worker = FiveSeatWorker(
                self.scheduler,
                self.registry,
                self.signal,
                owner=f"fabric:{self.instance_id}:worker:{index}",
                execution_room_id=f"five-seat:{self.instance_id}:{index}",
                lease_seconds=self.lease_seconds,
                idle_fallback_seconds=5.0,
            )
            thread = threading.Thread(
                target=worker.run_forever,
                name=f"five-seat-worker-{index}",
                daemon=True,
            )
            thread.start()
            self._workers.append(worker)
            self._threads.append(thread)

        for target, name in (
            (self._watch_loop, "five-seat-watch"),
            (self._guardian_loop, "five-seat-guardian"),
            (self._report_loop, "five-seat-hourly-report"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        self._started = True

    def stop(self) -> None:
        self._stop.set()
        for worker in self._workers:
            worker.stop()
        self._started = False

    def _watch_loop(self) -> None:
        token = None
        last_heartbeat = 0.0
        owner = f"{WATCH_OWNER_PREFIX}:{self.instance_id}"
        while not self._stop.is_set():
            try:
                if token is None:
                    token = self.watch.claim_leader(
                        owner=owner,
                        lease_seconds=self.lease_seconds,
                    )
                    last_heartbeat = time.time()
                elif time.time() - last_heartbeat >= max(10.0, self.lease_seconds / 3):
                    token = self.watch.heartbeat(
                        token,
                        lease_seconds=self.lease_seconds,
                    )
                    last_heartbeat = time.time()

                for handoff in self.watch.pending_reviews(limit=100):
                    payload = handoff.get("payload") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    result = dict(payload.get("result") or {})
                    partial = str(
                        payload.get("partial_side_effect_status")
                        or result.get("partial_side_effect_status")
                        or "UNCERTAIN_PARTIAL"
                    ).upper()
                    unresolved = list(
                        payload.get("unresolved_items")
                        or result.get("unresolved_items")
                        or []
                    )
                    overall = str(
                        result.get("whole_packet_status")
                        or "FAILED_CLOSED"
                    ).upper()

                    if partial not in {
                        "NONE",
                        "VERIFIED_COMPLETE",
                        "VERIFIED_NOT_DONE",
                    }:
                        decision = "BLOCK"
                        reason = "uncertain side-effect state requires quarantine"
                    elif overall == "SUCCESS" and not unresolved:
                        decision = "ACCEPT"
                        reason = "whole packet success independently verified"
                    elif overall in {"PARTIAL_SUCCESS", "NEEDS_VALIDATION"}:
                        decision = "REWORK"
                        reason = "bounded hardening/validation required"
                    elif overall == "POLICY_BLOCKED":
                        decision = "ESCALATE"
                        reason = "owner/policy decision required"
                    else:
                        decision = "BLOCK"
                        reason = "worker evidence does not satisfy acceptance contract"

                    evidence = {
                        "service_id": FABRIC_SERVICE_ID,
                        "whole_packet_status": overall,
                        "partial_side_effect_status": partial,
                        "unresolved_count": len(unresolved),
                        "result_sha256": result.get("result_sha256"),
                        "independent_watch_review": True,
                    }
                    self.watch.review(
                        token,
                        review_id="watch-" + str(handoff["handoff_id"]),
                        handoff_id=str(handoff["handoff_id"]),
                        decision=decision,
                        reason=reason,
                        evidence=evidence,
                    )
            except WatchStaleLeader:
                token = None
            except Exception as exc:
                self._errors.append(
                    "watch:" + type(exc).__name__ + ":" + str(exc)[:500]
                )
                token = None
            self._stop.wait(0.5)

    def _guardian_loop(self) -> None:
        while not self._stop.wait(self.guardian_interval_seconds):
            try:
                self.guardian.run_once()
            except Exception as exc:
                self._errors.append(
                    "guardian:" + type(exc).__name__ + ":" + str(exc)[:500]
                )

    def _report_loop(self) -> None:
        # Startup does not emit a fake hourly report. First durable delta is
        # produced only after one complete reporting window.
        while not self._stop.wait(self.report_interval_seconds):
            try:
                report = self.reporter.last_60_minutes(window_minutes=60)
                with psycopg2.connect(self.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO jaytec_job_events(
                              job_id,event_type,source,source_version,payload
                            ) VALUES (
                              NULL,'WATCH_LAST_60_MINUTES_REPORT',
                              'FIVE_SEAT_WATCH',NULL,%s::jsonb
                            )
                            """,
                            (json.dumps(report, ensure_ascii=False, default=str),),
                        )
            except Exception as exc:
                self._errors.append(
                    "report:" + type(exc).__name__ + ":" + str(exc)[:500]
                )

    def status(self) -> dict[str, Any]:
        seats = self.scheduler.list_seats()
        return {
            "service_id": FABRIC_SERVICE_ID,
            "started": self._started,
            "worker_threads_configured": len(self._workers),
            "worker_threads_alive": sum(
                1 for thread in self._threads
                if thread.name.startswith("five-seat-worker-")
                and thread.is_alive()
            ),
            "seat_count": len(seats),
            "seats": seats,
            "errors": list(self._errors[-20:]),
        }
