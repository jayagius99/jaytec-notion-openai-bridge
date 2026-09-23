from __future__ import annotations

import copy
import json
import os
import socket
import threading
from typing import Any, Mapping, Optional

from openai import OpenAI

import server as legacy_server
from circuit_breaker import CircuitBreaker, CircuitOpenError
from durable_tasks import contains_secret_material
from durable_tasks_runtime import ReliableDurableTaskQueue, ReliableDurableTaskWorker
from five_seat_authority import PostgresFabricAuthority
from five_seat_queue import PostgresFabricQueue
from five_seat_reporting import FiveSeatReporter
from five_seat_service import (
    FiveSeatFabricService,
    submit_low_risk_task_packet,
)
from guardian_runtime import ReliabilityGuardian
from idempotency_postgres import PostgresExecutionRegistry
from orchestration import (
    ExecutionRegistry,
    PacketValidationError,
    ProviderUnavailableError,
    RateLimitError,
    parse_packet_json,
    validate_packet,
)
from reliability_registry import TransientAwareRegistry
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    EXPECTED_SOL_MODEL,
    build_codex_dispatch,
    build_gemini_dispatch,
    build_sol_reserve_dispatch,
    fetch_vercel_gateway_credit_balance,
)
from workload_read_model import WorkloadReadModel


RUNTIME_ID = "JAYTEC_RELIABILITY_RUNTIME_V1"
LEGACY_SYNC_PROVIDER_TIMEOUT_S = float(os.environ.get("LEGACY_SYNC_PROVIDER_TIMEOUT_S", "8"))
JAYTEC_READ_SYNC_TIMEOUT_S = float(os.environ.get("JAYTEC_READ_SYNC_TIMEOUT_S", "45"))
DURABLE_CODEX_TIMEOUT_S = float(os.environ.get("DURABLE_CODEX_TIMEOUT_S", "90"))
DURABLE_GEMINI_TIMEOUT_S = float(os.environ.get("DURABLE_GEMINI_TIMEOUT_S", "120"))
DURABLE_WORKER_ENABLED = os.environ.get(
    "DURABLE_WORKER_ENABLED",
    "1" if legacy_server.RUNTIME_MODE == "production" else "0",
).strip().lower() in {"1", "true", "yes", "on"}
GUARDIAN_LOOP_ENABLED = os.environ.get(
    "GUARDIAN_LOOP_ENABLED",
    "1" if legacy_server.RUNTIME_MODE == "production" else "0",
).strip().lower() in {"1", "true", "yes", "on"}
DURABLE_WORKER_POLL_S = float(os.environ.get("DURABLE_WORKER_POLL_S", "1"))
DURABLE_WORKER_LEASE_S = int(os.environ.get("DURABLE_WORKER_LEASE_S", "300"))
GUARDIAN_INTERVAL_S = float(os.environ.get("GUARDIAN_INTERVAL_S", "60"))
MAX_PARALLEL_DURABLE_JOBS = int(os.environ.get("MAX_PARALLEL_DURABLE_JOBS", "4"))
FIVE_SEAT_LEASE_S = int(os.environ.get("FIVE_SEAT_LEASE_S", "300"))
FIVE_SEAT_GUARDIAN_INTERVAL_S = float(os.environ.get("FIVE_SEAT_GUARDIAN_INTERVAL_S", "30"))
FIVE_SEAT_REPORT_INTERVAL_S = float(os.environ.get("FIVE_SEAT_REPORT_INTERVAL_S", "3600"))


def _five_seat_enabled() -> bool:
    return legacy_server.five_seat_fabric_enabled()


_ORIGINAL_EXECUTE = legacy_server._execute_task_packet_json
_ORIGINAL_BUILD_CODEX = legacy_server.build_codex_dispatch
_ORIGINAL_BUILD_GEMINI = legacy_server.build_gemini_dispatch


def _retryable_single_attempt_dispatch(underlying, *, model: str):
    """Convert one provider attempt into a JAYTEC result instead of inner retries.

    The orchestration core retries only exceptions. Returning a valid transient
    worker result here makes both legacy compatibility calls and durable jobs
    single-attempt at this layer. Durable retry/backoff is then owned only by
    the persistent queue, preventing multiplicative retry budgets.
    """

    def single_attempt(packet):
        try:
            return underlying(packet)
        except TimeoutError:
            return {
                "status": "TIMEOUT",
                "model": model,
                "findings": [],
                "evidence": [],
                "unresolved_items": ["worker_timeout"],
            }
        except RateLimitError as exc:
            retry_after = getattr(exc, "retry_after", None)
            suffix = f":retry_after={retry_after}" if retry_after is not None else ""
            return {
                "status": "RATE_LIMITED",
                "model": model,
                "findings": [],
                "evidence": [],
                "unresolved_items": [f"retryable_rate_limit{suffix}"],
            }
        except ProviderUnavailableError as exc:
            retry_after = getattr(exc, "retry_after", None)
            suffix = f":retry_after={retry_after}" if retry_after is not None else ""
            return {
                "status": "RATE_LIMITED",
                "model": model,
                "findings": [],
                "evidence": [],
                "unresolved_items": [f"retryable_provider_unavailable{suffix}"],
            }
        except CircuitOpenError:
            return {
                "status": "RATE_LIMITED",
                "model": model,
                "findings": [],
                "evidence": [],
                "unresolved_items": ["retryable_provider_circuit_open"],
            }

    return single_attempt


def _transient_safe_execute_task_packet_json(
    packet_json: str,
    *,
    registry: ExecutionRegistry,
    idempotency_store: str,
    codex_dispatch: Any,
    gemini_dispatch: Any,
    sol_dispatch: Any = None,
) -> str:
    return _ORIGINAL_EXECUTE(
        packet_json,
        registry=TransientAwareRegistry(registry),
        idempotency_store=idempotency_store,
        codex_dispatch=codex_dispatch,
        gemini_dispatch=gemini_dispatch,
        sol_dispatch=sol_dispatch,
    )


def _bounded_legacy_codex_dispatch(
    *,
    openai_client,
    codex_model,
    circuit,
    provider_mode=None,
    max_output_tokens=None,
    max_packet_retries=None,
):
    config = dict(legacy_server.engineering_dispatch_kwargs())
    if provider_mode is not None:
        config["provider_mode"] = provider_mode
    if max_output_tokens is not None:
        config["max_output_tokens"] = max_output_tokens
    if max_packet_retries is not None:
        config["max_packet_retries"] = max_packet_retries
    underlying = _ORIGINAL_BUILD_CODEX(
        openai_client=openai_client,
        codex_model=codex_model,
        circuit=circuit,
        codex_timeout_s=max(1.0, LEGACY_SYNC_PROVIDER_TIMEOUT_S),
        **config,
    )
    return _retryable_single_attempt_dispatch(underlying, model=EXPECTED_CODEX_MODEL)


def _bounded_legacy_gemini_dispatch(*, openrouter_client, gemini_model, gemini_timeout_s, circuit):
    underlying = _ORIGINAL_BUILD_GEMINI(
        openrouter_client=openrouter_client,
        gemini_model=gemini_model,
        gemini_timeout_s=max(1.0, min(float(gemini_timeout_s), LEGACY_SYNC_PROVIDER_TIMEOUT_S)),
        circuit=circuit,
    )
    read_underlying = _ORIGINAL_BUILD_GEMINI(
        openrouter_client=openrouter_client,
        gemini_model=gemini_model,
        gemini_timeout_s=max(1.0, min(float(gemini_timeout_s), JAYTEC_READ_SYNC_TIMEOUT_S)),
        circuit=circuit,
    )

    def no_format_retry(packet):
        bounded = copy.deepcopy(dict(packet))
        bounded["max_retries"] = 0
        # Ordinary compatibility calls remain tightly bounded. JAYTEC:READ is
        # a dedicated direct retrieval command and gets a larger, still-bounded
        # window so web_fetch + Gemini can complete without routing elsewhere.
        if bounded.get("workflow_id") == "JAYTEC_READ":
            return read_underlying(bounded)
        return underlying(bounded)

    return _retryable_single_attempt_dispatch(no_format_retry, model=EXPECTED_GEMINI_MODEL)


# Compatibility path remains available, but it is tightly bounded and transient
# failures do not poison idempotent replay. Durable submit/poll is preferred.
legacy_server._execute_task_packet_json = _transient_safe_execute_task_packet_json
legacy_server.build_codex_dispatch = _bounded_legacy_codex_dispatch
legacy_server.build_gemini_dispatch = _bounded_legacy_gemini_dispatch


class ReliableDurableWorkerPool:
    """Run several independently fenced durable workers on one service instance."""

    def __init__(
        self,
        queue: ReliableDurableTaskQueue,
        execute_packet,
        *,
        instance_id: str,
        worker_count: int,
        poll_seconds: float,
        lease_seconds: int,
    ):
        count = max(1, int(worker_count))
        self.workers = [
            ReliableDurableTaskWorker(
                queue,
                execute_packet,
                owner=f"render:{instance_id}:worker:{index}",
                execution_room_id=f"durable-worker:{instance_id}:{index}",
                poll_seconds=poll_seconds,
                lease_seconds=lease_seconds,
            )
            for index in range(count)
        ]

    def start(self) -> None:
        for worker in self.workers:
            worker.start()

    def stop(self) -> None:
        for worker in self.workers:
            worker.stop()

    def run_once(self) -> bool:
        # Diagnostic kick only. Normal production execution uses all pool
        # threads. Try each worker until one safely claims work.
        for worker in self.workers:
            if worker.run_once():
                return True
        return False

    @property
    def alive(self) -> bool:
        return bool(self.workers) and all(worker.alive for worker in self.workers)

    @property
    def alive_count(self) -> int:
        return sum(1 for worker in self.workers if worker.alive)

    @property
    def worker_count(self) -> int:
        return len(self.workers)


class GuardianBackgroundLoop:
    def __init__(
        self,
        guardian: ReliabilityGuardian,
        queue: ReliableDurableTaskQueue,
        *,
        interval_seconds: float,
    ):
        self.guardian = guardian
        self.queue = queue
        self.interval_seconds = max(5.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self) -> Mapping[str, Any]:
        return self.guardian.run(auto_repair=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                try:
                    self.queue.record_incident(
                        "GUARDIAN_LOOP_EXCEPTION",
                        detail={
                            "error_class": type(exc).__name__,
                            "message": str(exc)[:1000],
                        },
                    )
                except Exception:
                    pass
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="jaytec-guardian-lite-loop",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def create_mcp_app():
    mcp = legacy_server.create_mcp_app()

    queue: Optional[ReliableDurableTaskQueue] = None
    worker: Optional[ReliableDurableWorkerPool] = None
    guardian: Optional[ReliabilityGuardian] = None
    guardian_loop: Optional[GuardianBackgroundLoop] = None
    workload_read_model: Optional[WorkloadReadModel] = None
    durable_codex_circuit: Optional[CircuitBreaker] = None
    durable_gemini_circuit: Optional[CircuitBreaker] = None
    durable_sol_circuit: Optional[CircuitBreaker] = None
    fabric_queue: Optional[PostgresFabricQueue] = None
    fabric_authority: Optional[PostgresFabricAuthority] = None
    fabric_service: Optional[FiveSeatFabricService] = None
    fabric_reporter: Optional[FiveSeatReporter] = None
    fabric_enabled = _five_seat_enabled()

    if legacy_server.DATABASE_URL:
        queue = ReliableDurableTaskQueue(
            legacy_server.DATABASE_URL,
            max_parallel=MAX_PARALLEL_DURABLE_JOBS,
        )
        workload_read_model = WorkloadReadModel(legacy_server.DATABASE_URL)
        # Read-only gate: the tracked Neon migration must already have been
        # explicitly approved/applied. Runtime startup never mutates PR #8 DDL.
        queue.verify_schema_ready()
        guardian = ReliabilityGuardian(legacy_server.DATABASE_URL)

        durable_registry = PostgresExecutionRegistry(
            database_url=legacy_server.DATABASE_URL,
            ttl_seconds=ExecutionRegistry().ttl_seconds,
        )
        durable_registry.ensure_schema()

        durable_openai = OpenAI(api_key=legacy_server.OPENAI_API_KEY) if legacy_server.OPENAI_API_KEY else None
        durable_openrouter = (
            OpenAI(
                api_key=legacy_server.OPENROUTER_API_KEY,
                base_url=legacy_server.OPENROUTER_BASE_URL,
            )
            if legacy_server.OPENROUTER_API_KEY
            else None
        )
        durable_sol_gateway = (
            OpenAI(
                api_key=legacy_server.AI_GATEWAY_API_KEY,
                base_url=legacy_server.SOL_GATEWAY_URL,
            )
            if legacy_server.AI_GATEWAY_API_KEY
            else None
        )
        durable_codex_circuit = CircuitBreaker(
            failure_threshold=legacy_server.CIRCUIT_FAILURE_THRESHOLD,
            reset_after_seconds=legacy_server.CIRCUIT_RESET_SECONDS,
        )
        durable_gemini_circuit = CircuitBreaker(
            failure_threshold=legacy_server.CIRCUIT_FAILURE_THRESHOLD,
            reset_after_seconds=legacy_server.CIRCUIT_RESET_SECONDS,
        )
        durable_sol_circuit = CircuitBreaker(
            failure_threshold=1,
            reset_after_seconds=max(legacy_server.CIRCUIT_RESET_SECONDS, 300),
        )
        if durable_openrouter is not None:
            durable_codex_dispatch = _retryable_single_attempt_dispatch(
                build_codex_dispatch(
                    openai_client=durable_openrouter,
                    codex_model=legacy_server.CODEX_MODEL,
                    circuit=durable_codex_circuit,
                    codex_timeout_s=DURABLE_CODEX_TIMEOUT_S,
                    **legacy_server.engineering_dispatch_kwargs(),
                ),
                model=EXPECTED_CODEX_MODEL,
            )
        else:
            durable_codex_dispatch = durable_codex_circuit.guard(
                lambda _packet: (_ for _ in ()).throw(
                    RuntimeError("OPENROUTER_API_KEY is not configured on this bridge")
                )
            )
        if durable_openrouter is not None:
            durable_gemini_dispatch = _retryable_single_attempt_dispatch(
                build_gemini_dispatch(
                    openrouter_client=durable_openrouter,
                    gemini_model=legacy_server.GEMINI_MODEL,
                    gemini_timeout_s=DURABLE_GEMINI_TIMEOUT_S,
                    circuit=durable_gemini_circuit,
                ),
                model=EXPECTED_GEMINI_MODEL,
            )
        else:
            durable_gemini_dispatch = durable_gemini_circuit.guard(
                lambda _packet: (_ for _ in ()).throw(
                    RuntimeError("OPENROUTER_API_KEY is not configured on this bridge")
                )
            )

        if (
            durable_sol_gateway is not None
            and legacy_server.SOL_RESERVE_ENABLED
            and legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED
        ):
            durable_sol_dispatch = _retryable_single_attempt_dispatch(
                build_sol_reserve_dispatch(
                    gateway_client=durable_sol_gateway,
                    gateway_api_key=legacy_server.AI_GATEWAY_API_KEY,
                    circuit=durable_sol_circuit,
                    credit_balance_fn=lambda: fetch_vercel_gateway_credit_balance(
                        api_key=legacy_server.AI_GATEWAY_API_KEY,
                        base_url=legacy_server.SOL_GATEWAY_URL,
                    ),
                    reserve_enabled=legacy_server.SOL_RESERVE_ENABLED,
                    zero_spend_attested=legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED,
                    sol_timeout_s=legacy_server.SOL_TIMEOUT_S,
                    max_output_tokens=legacy_server.SOL_OUTPUT_TOKEN_CAP,
                    max_input_bytes=legacy_server.SOL_INPUT_BYTE_CAP,
                    min_credit_usd=legacy_server.SOL_MIN_CREDIT_USD,
                    reasoning_effort=legacy_server.SOL_REASONING_EFFORT,
                ),
                model=EXPECTED_SOL_MODEL,
            )
        else:
            durable_sol_dispatch = durable_sol_circuit.guard(
                lambda _packet: (_ for _ in ()).throw(
                    RuntimeError(
                        "Sol reserve unavailable: zero-spend gates are not fully satisfied"
                    )
                )
            )

        def _execute_durable(packet_json: str) -> Mapping[str, Any]:
            text = _transient_safe_execute_task_packet_json(
                packet_json,
                registry=durable_registry,
                idempotency_store="postgres",
                codex_dispatch=durable_codex_dispatch,
                gemini_dispatch=durable_gemini_dispatch,
                sol_dispatch=durable_sol_dispatch,
            )
            value = json.loads(text)
            if not isinstance(value, dict):
                raise RuntimeError("durable packet result must be object")
            return value

        instance_id = os.environ.get("RENDER_INSTANCE_ID", "").strip() or socket.gethostname()
        if fabric_enabled:
            # One execution owner only. The legacy durable background worker and
            # legacy Guardian loop stay stopped while the five-seat fabric owns
            # admission/execution/review/recovery.
            fabric_queue = PostgresFabricQueue(legacy_server.DATABASE_URL)
            fabric_authority = PostgresFabricAuthority(legacy_server.DATABASE_URL)
            fabric_reporter = FiveSeatReporter(legacy_server.DATABASE_URL)
            fabric_service = FiveSeatFabricService(
                legacy_server.DATABASE_URL,
                _execute_durable,
                instance_id=instance_id,
                lease_seconds=FIVE_SEAT_LEASE_S,
                guardian_interval_seconds=FIVE_SEAT_GUARDIAN_INTERVAL_S,
                report_interval_seconds=FIVE_SEAT_REPORT_INTERVAL_S,
            )
            fabric_service.verify_ready()
            fabric_service.start()
        else:
            worker = ReliableDurableWorkerPool(
                queue,
                _execute_durable,
                instance_id=instance_id,
                worker_count=MAX_PARALLEL_DURABLE_JOBS,
                poll_seconds=DURABLE_WORKER_POLL_S,
                lease_seconds=DURABLE_WORKER_LEASE_S,
            )
            guardian_loop = GuardianBackgroundLoop(
                guardian,
                queue,
                interval_seconds=GUARDIAN_INTERVAL_S,
            )
            if DURABLE_WORKER_ENABLED:
                worker.start()
            if GUARDIAN_LOOP_ENABLED:
                guardian_loop.start()

    @mcp.tool
    def submit_task_packet_durable(
        packet_json: str,
        source_shared_state_version: int = 0,
        priority: int = 100,
        execution_room_id: str = "",
    ) -> str:
        """Persist a specialist packet and immediately return a pollable job handle."""
        if queue is None:
            return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
        if fabric_enabled:
            return _json({
                "available": True,
                "accepted": False,
                "reason": "FIVE_SEAT_FABRIC_OWNS_EXECUTION",
                "preferred_submit_tool": "submit_task_packet_five_seat",
            })
        try:
            snapshot = queue.submit(
                packet_json,
                source_shared_state_version=source_shared_state_version,
                priority=priority,
                source_execution_room_id=execution_room_id or None,
            )
            return _json({
                "available": True,
                "accepted": True,
                "preferred_poll_tool": "task_packet_status",
                "snapshot": snapshot,
            })
        except (PacketValidationError, ValueError, RuntimeError) as exc:
            return _json({
                "available": True,
                "accepted": False,
                "error_class": type(exc).__name__,
                "error": str(exc),
            })

    @mcp.tool
    def submit_task_packet_five_seat(
        packet_json: str,
        source_shared_state_version: int,
        priority: int = 100,
    ) -> str:
        """Submit one zero-spend specialist packet to the WATCH-controlled five-seat fabric."""
        if not fabric_enabled:
            return _json({
                "available": False,
                "accepted": False,
                "reason": "FIVE_SEAT_FABRIC_DISABLED",
            })
        if fabric_queue is None or fabric_authority is None:
            return _json({
                "available": False,
                "accepted": False,
                "reason": "FIVE_SEAT_FABRIC_NOT_READY",
            })
        try:
            snapshot = submit_low_risk_task_packet(
                fabric_queue,
                packet_json=packet_json,
                source_shared_state_version=int(source_shared_state_version),
                priority=int(priority),
            )
            return _json({
                "available": True,
                "accepted": True,
                "preferred_poll_tool": "five_seat_job_status",
                "snapshot": snapshot,
            })
        except (PacketValidationError, ValueError, RuntimeError) as exc:
            return _json({
                "available": True,
                "accepted": False,
                "error_class": type(exc).__name__,
                "error": str(exc)[:1000],
            })

    @mcp.tool
    def five_seat_job_status(job_id: str) -> str:
        """Read one five-seat job and its latest WATCH attestation."""
        if not fabric_enabled or fabric_service is None:
            return _json({"available": False, "reason": "FIVE_SEAT_FABRIC_DISABLED"})
        try:
            return _json({"available": True, **fabric_service.job_status(job_id)})
        except Exception as exc:
            return _json({
                "available": True,
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
            })

    @mcp.tool
    def five_seat_status() -> str:
        """Return current five-seat host/seat state without mutating it."""
        if not fabric_enabled or fabric_service is None:
            return _json({"available": False, "reason": "FIVE_SEAT_FABRIC_DISABLED"})
        return _json({"available": True, **fabric_service.status()})

    @mcp.tool
    def watch_last_60_minutes() -> str:
        """Return the WATCH delta report required by the owner operating contract."""
        if not fabric_enabled or fabric_reporter is None:
            return _json({"available": False, "reason": "FIVE_SEAT_FABRIC_DISABLED"})
        try:
            return _json({"available": True, **fabric_reporter.last_60_minutes(window_minutes=60)})
        except Exception as exc:
            return _json({
                "available": True,
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
            })

    @mcp.tool
    def task_packet_status(job_id: str = "", idempotency_key: str = "") -> str:
        """Poll a durable specialist packet without rerunning it."""
        if queue is None:
            return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
        try:
            return _json({
                "available": True,
                "snapshot": queue.status(
                    job_id=job_id or None,
                    idempotency_key=idempotency_key or None,
                ),
            })
        except Exception as exc:
            return _json({
                "available": True,
                "error_class": type(exc).__name__,
                "error": str(exc),
            })

    @mcp.tool
    def reliability_status() -> str:
        """Return durable worker/Guardian health and current operational counts."""
        if queue is None:
            return _json({
                "runtime_id": RUNTIME_ID,
                "database_available": False,
                "durable_worker_enabled": False,
                "guardian_loop_enabled": False,
            })
        return _json({
            "runtime_id": RUNTIME_ID,
            "database_available": True,
            "preferred_specialist_execution": (
                "submit_task_packet_five_seat -> five_seat_job_status"
                if fabric_enabled
                else "submit_task_packet_durable -> task_packet_status"
            ),
            "legacy_sync_execute_task_packet": "compatibility_only_bounded_single-attempt",
            "legacy_sync_provider_timeout_seconds": LEGACY_SYNC_PROVIDER_TIMEOUT_S,
            "five_seat_fabric_enabled": fabric_enabled,
            "five_seat_service": fabric_service.status() if fabric_service else None,
            "legacy_durable_worker_suppressed_by_fabric": bool(fabric_enabled),
            "durable_worker_enabled": bool(DURABLE_WORKER_ENABLED and not fabric_enabled),
            "durable_worker_alive": bool(worker and worker.alive),
            "durable_worker_count": worker.worker_count if worker else 0,
            "durable_worker_alive_count": worker.alive_count if worker else 0,
            "durable_worker_has_lease_heartbeat": bool(worker),
            "guardian_loop_enabled": bool(GUARDIAN_LOOP_ENABLED and not fabric_enabled),
            "guardian_loop_alive": bool(guardian_loop and guardian_loop.alive),
            "guardian_runtime": "ReliabilityGuardian",
            "durable_codex_circuit": durable_codex_circuit.snapshot() if durable_codex_circuit else None,
            "durable_gemini_circuit": durable_gemini_circuit.snapshot() if durable_gemini_circuit else None,
            "durable_sol_circuit": durable_sol_circuit.snapshot() if durable_sol_circuit else None,
            "sol_reserve_enabled": bool(
                legacy_server.AI_GATEWAY_API_KEY
                and legacy_server.SOL_RESERVE_ENABLED
                and legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED
            ),
            "stats": queue.stats(),
        })

    @mcp.tool
    def workload_snapshot(limit: int = 50) -> str:
        """Return a bounded, read-only workload projection without packet/result bodies."""
        if workload_read_model is None:
            return _json({"available": False, "source": "JAYTEC_DURABLE_RUNTIME", "reason": "DATABASE_URL_NOT_CONFIGURED"})
        try:
            return _json({"available": True, **workload_read_model.snapshot(limit=limit)})
        except Exception as exc:
            return _json({"available": True, "source": "JAYTEC_DURABLE_RUNTIME", "error_class": type(exc).__name__, "error": str(exc)[:500]})

    @mcp.tool
    def run_guardian_lite(auto_repair: bool = True) -> str:
        """Run one bounded Guardian audit/containment pass immediately."""
        if guardian is None:
            return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
        try:
            return _json({"available": True, "result": guardian.run(auto_repair=auto_repair)})
        except Exception as exc:
            return _json({
                "available": True,
                "error_class": type(exc).__name__,
                "error": str(exc),
            })

    @mcp.tool
    def record_reliability_incident(
        event_type: str,
        detail_json: str = "{}",
        job_id: str = "",
    ) -> str:
        """Persist a caller-observed timeout/freeze for self-diagnosis and recovery."""
        if queue is None:
            return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
        try:
            detail = json.loads(detail_json or "{}")
            if not isinstance(detail, dict):
                raise ValueError("detail_json must encode an object")
            event = queue.record_incident(
                event_type,
                job_id=job_id or None,
                detail=detail,
            )
            return _json({"available": True, "event": event})
        except Exception as exc:
            return _json({
                "available": True,
                "error_class": type(exc).__name__,
                "error": str(exc),
            })

    @mcp.tool
    def durable_worker_kick() -> str:
        """Run one worker iteration for diagnostics when background execution is disabled."""
        if fabric_enabled:
            return _json({
                "available": False,
                "reason": "FIVE_SEAT_FABRIC_OWNS_EXECUTION",
            })
        if worker is None:
            return _json({"available": False, "reason": "DATABASE_URL_NOT_CONFIGURED"})
        try:
            return _json({"available": True, "worked": worker.run_once()})
        except Exception as exc:
            return _json({
                "available": True,
                "error_class": type(exc).__name__,
                "error": str(exc),
            })

    mcp._jaytec_reliability = {
        "queue": queue,
        "worker": worker,
        "guardian": guardian,
        "guardian_loop": guardian_loop,
        "workload_read_model": workload_read_model,
        "five_seat_enabled": fabric_enabled,
        "five_seat_queue": fabric_queue,
        "five_seat_authority": fabric_authority,
        "five_seat_service": fabric_service,
        "five_seat_reporter": fabric_reporter,
    }
    return mcp


def main() -> None:
    mcp = create_mcp_app()
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=legacy_server.PORT,
        stateless_http=True,
        host_origin_protection=False,
    )


if __name__ == "__main__":
    main()
