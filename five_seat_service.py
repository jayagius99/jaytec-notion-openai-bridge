from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import threading
import time
from typing import Any, Callable, Mapping

import psycopg2
import psycopg2.extras

from canonical_writer import PostgresCanonicalWriterQueue
from dan_worker_relay import (
    DanWorkerRelay,
    DanWorkerRelayConfig,
    DanWorkerRelayError,
)
from durable_tasks import SUCCESS_OVERALL_STATUSES, contains_secret_material
from five_seat_adapters import AdapterRegistry, RetryableAdapterError
from five_seat_guardian import FiveSeatGuardian
from five_seat_github_broker import (
    GITHUB_WORK_CAPABILITY,
    GITHUB_WORKER_KIND,
    GitHubBranchPrBroker,
)
from five_seat_queue import PostgresFabricQueue
from five_seat_reporting import FiveSeatReporter
from five_seat_runtime import PostgresFiveSeatScheduler
from five_seat_signals import PostgresFabricSignal
from five_seat_watch import (
    PostgresWatchController,
    WatchLeaderUnavailable,
    WatchLeaderToken,
    WatchStaleLeader,
)
from five_seat_worker import FiveSeatWorker
from orchestration import PacketValidationError, parse_packet_json, validate_packet
from reliability_registry import transient_specialist_statuses


TASK_PACKET_WORKER_KIND = "TASK_PACKET"
TASK_PACKET_CAPABILITY = "jaytec.task_packet.execute"
FABRIC_SERVICE_ID = "JAYTEC_FIVE_SEAT_PRODUCTION_HOST_V1"
WATCH_OWNER_PREFIX = "five-seat-watch"
DAN_RECOVERY_SAFE_PARTIAL = frozenset(
    {"NONE", "VERIFIED_COMPLETE", "VERIFIED_NOT_DONE"}
)


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _dan_relay_from_env() -> DanWorkerRelay | None:
    if not _env_enabled("JAYTEC_DAN_WORKER_ENABLED"):
        return None
    token = (
        os.environ.get("JAYTEC_DAN_GITHUB_TOKEN", "").strip()
        or os.environ.get("JAYTEC_GITHUB_BROKER_TOKEN", "").strip()
    )
    repository = os.environ.get(
        "JAYTEC_DAN_RELAY_REPOSITORY",
        "jayagius99/jaytec-work-engine-v2-g1",
    ).strip()
    issue_number = int(os.environ.get("JAYTEC_DAN_RELAY_ISSUE", "128"))
    timeout_seconds = float(
        os.environ.get("JAYTEC_DAN_RECOVERY_TIMEOUT_SECONDS", "180")
    )
    return DanWorkerRelay(
        DanWorkerRelayConfig.build(
            token,
            repository,
            issue_number,
            timeout_seconds=timeout_seconds,
        )
    )


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


def submit_low_risk_task_packet(
    queue: PostgresFabricQueue,
    *,
    packet_json: str,
    source_shared_state_version: int,
    priority: int = 100,
) -> dict[str, Any]:
    """Single source of truth for the initial FIVE_SEAT_LOW_RISK_V1 ingress."""
    packet, parse_errors = parse_packet_json(packet_json)
    if packet is None:
        raise PacketValidationError(";".join(parse_errors))
    validation = validate_packet(packet)
    if not validation.ok:
        raise PacketValidationError(";".join(validation.errors))
    if contains_secret_material(packet):
        raise PacketValidationError("packet_contains_secret_material")

    plan = [str(item).strip().lower() for item in packet.get("specialist_plan", [])]
    if not plan or any(item not in {"codex", "gemini"} for item in plan):
        raise PacketValidationError(
            "FIVE_SEAT_LOW_RISK_V1 permits only codex/gemini free-primary roles"
        )
    if str(packet.get("side_effect_policy") or "").strip().lower() != "none":
        raise PacketValidationError(
            "FIVE_SEAT_LOW_RISK_V1 requires side_effect_policy=none"
        )

    bounded = copy.deepcopy(packet)
    bounded["max_retries"] = 0
    objective = str(
        bounded.get("intent")
        or bounded.get("request")
        or "JAYTEC specialist packet"
    )[:4000]
    task_id = str(bounded.get("task_id") or "").strip()
    subtask_id = str(bounded.get("subtask_id") or "").strip() or None
    idempotency_key = str(bounded.get("idempotency_key") or "").strip()
    if not task_id or not idempotency_key:
        raise PacketValidationError("task_id/idempotency_key required")

    return queue.submit(
        task_id=task_id,
        subtask_id=subtask_id,
        objective=objective,
        worker_kind=TASK_PACKET_WORKER_KIND,
        idempotency_key="five-seat:" + idempotency_key,
        source_shared_state_version=int(source_shared_state_version),
        authority_class="READ_ONLY",
        concurrency_class="A",
        priority=int(priority),
        max_attempts=max(1, min(3, int(bounded.get("max_retries") or 0) + 1)),
        max_reworks=2,
        required_capabilities={TASK_PACKET_CAPABILITY},
        read_scope={f"specialist:{name}" for name in plan},
        mutation_scope=set(),
        resource_scope={"specialists": plan},
        dependencies=set(),
        collision_key="task-packet:" + task_id,
        cost_policy={
            "mode": "ZERO_SPEND",
            "allow_paid": False,
            "max_cost_usd": 0,
            "provider_mode": "FREE_ONLY",
        },
        evidence_standard={
            "watch_review_required": True,
            "whole_packet_accept_required": True,
        },
        stop_conditions={
            "provider_fallback": "FAIL_CLOSED",
            "side_effect_request": "FAIL_CLOSED",
        },
        result_destination={
            "type": "WATCH_ATTESTED_TASK_PACKET",
            "task_id": task_id,
        },
        payload={
            "packet_json": json.dumps(bounded, ensure_ascii=False, sort_keys=True),
        },
    )


def _bounded_dan_recovery_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    receipt = value.get("qwen_receipt")
    if not isinstance(receipt, Mapping):
        return None
    candidate = receipt.get("candidate")
    if not isinstance(candidate, Mapping):
        return None
    return {
        "identity": str(value.get("identity") or "")[:80],
        "attempt_id": str(value.get("attempt_id") or "")[:160],
        "source_state_version": value.get("source_state_version"),
        "ownership_fence": str(value.get("ownership_fence") or "")[:240],
        "model_id": str(receipt.get("model_id") or "")[:500],
        "response_sha256": str(receipt.get("response_sha256") or "")[:128],
        "result": str(candidate.get("result") or "")[:6000],
        "evidence": [str(item)[:1200] for item in list(candidate.get("evidence") or [])[:10]],
        "limitations": str(candidate.get("limitations") or "")[:3000],
        "recommended_next_action": str(
            candidate.get("recommended_next_action") or ""
        )[:3000],
        "acceptance": "WATCH_RECOVERY_CONTEXT_ONLY",
    }


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

        fabric_context = payload.get("_fabric_context")
        if isinstance(fabric_context, Mapping):
            watch_evidence = fabric_context.get("last_watch_evidence")
            if isinstance(watch_evidence, Mapping):
                dan_context = _bounded_dan_recovery_context(
                    watch_evidence.get("dan_worker_recovery")
                )
                if dan_context is not None:
                    packet_obj = json.loads(packet_json)
                    if not isinstance(packet_obj, dict):
                        raise RuntimeError("TASK_PACKET_JSON_NOT_OBJECT")
                    required_context = packet_obj.get("required_context")
                    if not isinstance(required_context, dict):
                        required_context = {}
                    else:
                        required_context = dict(required_context)
                    required_context["dan_worker_recovery"] = dan_context
                    packet_obj["required_context"] = required_context
                    packet_json = json.dumps(
                        packet_obj,
                        ensure_ascii=False,
                        sort_keys=True,
                    )

        raw = execute_packet(packet_json)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError("TASK_PACKET_RESULT_JSON_INVALID") from exc
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
        github_broker: GitHubBranchPrBroker | None = None,
        canonical_writer_queue: PostgresCanonicalWriterQueue | None = None,
        dan_relay: DanWorkerRelay | None = None,
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
        self.github_broker = github_broker
        self.canonical_writer_queue = canonical_writer_queue
        self.dan_relay = dan_relay
        self._dan_relay_startup_error: str | None = None
        if self.dan_relay is None and _env_enabled("JAYTEC_DAN_WORKER_ENABLED"):
            try:
                self.dan_relay = _dan_relay_from_env()
            except Exception as exc:
                self._dan_relay_startup_error = (
                    type(exc).__name__ + ":" + str(exc)[:500]
                )
        self.registry = AdapterRegistry()
        self.registry.register(
            TASK_PACKET_WORKER_KIND,
            capabilities={TASK_PACKET_CAPABILITY},
            execute=build_task_packet_adapter(execute_packet),
        )
        if self.github_broker is not None:
            self.registry.register(
                GITHUB_WORKER_KIND,
                capabilities={GITHUB_WORK_CAPABILITY},
                execute=self.github_broker.execute,
            )

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._workers: list[FiveSeatWorker] = []
        self._errors: list[str] = []
        self._watch_token: WatchLeaderToken | None = None
        self._watch_token_lock = threading.Lock()
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
        with self._watch_token_lock:
            token = self._watch_token
            self._watch_token = None
        if token is not None:
            try:
                self.watch.release_leader(token)
                print(
                    "FIVE_SEAT_WATCH_LEADER_RELEASED="
                    + json.dumps(
                        {
                            "owner": token.owner,
                            "leader_epoch": token.leader_epoch,
                            "fence_token": token.fence_token,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            except WatchStaleLeader:
                pass
            except Exception as exc:
                self._errors.append(
                    "watch_release:" + type(exc).__name__ + ":" + str(exc)[:500]
                )
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
                    with self._watch_token_lock:
                        self._watch_token = token
                    last_heartbeat = time.time()
                    print(
                        "FIVE_SEAT_WATCH_LEADER_ACQUIRED="
                        + json.dumps(
                            {
                                "owner": token.owner,
                                "leader_epoch": token.leader_epoch,
                                "fence_token": token.fence_token,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                elif time.time() - last_heartbeat >= max(10.0, self.lease_seconds / 3):
                    token = self.watch.heartbeat(
                        token,
                        lease_seconds=self.lease_seconds,
                    )
                    with self._watch_token_lock:
                        self._watch_token = token
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

                    external_verification: dict[str, Any] = {}
                    worker_kind = str(handoff.get("worker_kind") or "").upper()
                    if partial not in {
                        "NONE",
                        "VERIFIED_COMPLETE",
                        "VERIFIED_NOT_DONE",
                    }:
                        decision = "BLOCK"
                        reason = "uncertain side-effect state requires quarantine"
                    elif overall == "SUCCESS" and not unresolved:
                        if worker_kind == GITHUB_WORKER_KIND:
                            if self.github_broker is None:
                                decision = "BLOCK"
                                reason = "github mutation cannot be independently verified"
                            else:
                                try:
                                    external_verification = dict(
                                        self.github_broker.verify_result(result)
                                    )
                                except Exception as exc:
                                    decision = "BLOCK"
                                    reason = (
                                        "github independent readback failed:"
                                        + type(exc).__name__
                                    )
                                else:
                                    decision = "ACCEPT"
                                    reason = (
                                        "github branch/PR/file state independently "
                                        "read back by WATCH"
                                    )
                        else:
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
                        "worker_kind": worker_kind,
                        "whole_packet_status": overall,
                        "partial_side_effect_status": partial,
                        "unresolved_count": len(unresolved),
                        "result_sha256": result.get("result_sha256"),
                        "independent_watch_review": True,
                        **external_verification,
                    }
                    review_result = self.watch.review(
                        token,
                        review_id="watch-" + str(handoff["handoff_id"]),
                        handoff_id=str(handoff["handoff_id"]),
                        decision=decision,
                        reason=reason,
                        evidence=evidence,
                    )
                    destination = handoff.get("result_destination") or {}
                    if isinstance(destination, str):
                        destination = json.loads(destination)
                    if (
                        decision == "ACCEPT"
                        and isinstance(destination, Mapping)
                        and destination.get("type") == "CANONICAL_WRITE_CANDIDATE"
                    ):
                        if self.canonical_writer_queue is None:
                            raise RuntimeError(
                                "canonical_write_candidate_without_writer_queue"
                            )
                        reconciled = self.canonical_writer_queue.reconcile_watch_accepts(
                            limit=20
                        )
                        if reconciled.get("errors"):
                            raise RuntimeError(
                                "canonical_writer_reconcile_failed:"
                                + json.dumps(
                                    reconciled["errors"],
                                    sort_keys=True,
                                    default=str,
                                )[:1000]
                            )
            except WatchLeaderUnavailable:
                token = None
                with self._watch_token_lock:
                    self._watch_token = None
            except WatchStaleLeader:
                token = None
                with self._watch_token_lock:
                    self._watch_token = None
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

    def job_status(self, job_id: str) -> dict[str, Any]:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        with psycopg2.connect(self.database_url) as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT j.job_id,j.task_id,j.subtask_id,j.objective,j.status,
                           j.fabric_state,j.priority,j.source_shared_state_version,
                           j.seat_id,s.worker_id,j.lease_owner,j.lease_expires_at,
                           j.ownership_epoch,j.fence_token,j.checkpoint_ref,
                           j.blockers,j.health,j.created_at,j.updated_at,
                           e.worker_kind,e.authority_class,e.cost_policy,
                           r.review_id,r.decision AS watch_decision,
                           r.reason AS watch_reason,r.created_at AS watch_reviewed_at
                    FROM jaytec_jobs j
                    JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    LEFT JOIN jaytec_worker_seats s
                      ON s.seat_id=j.seat_id AND s.current_job_id=j.job_id
                    LEFT JOIN jaytec_watch_reviews r ON r.job_id=j.job_id
                    WHERE j.job_id=%s
                    ORDER BY r.created_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (job_id,),
                )
                record = cur.fetchone()
        return {"found": bool(record), "job": dict(record) if record else None}

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
            "canonical_writer_queue_enabled": self.canonical_writer_queue is not None,
            "dan_worker_recovery_enabled": self.dan_relay is not None,
            "dan_worker_recovery_error": self._dan_relay_startup_error,
        }
