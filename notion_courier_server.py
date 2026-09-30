"""Minimal Notion-facing JAYTEC MCP courier.

The exposed MCP catalog contains exactly one tool: collaborate.
That tool can only:
  * return JAYTEC orchestration status; or
  * forward one exact JAYTEC task packet.

No native JAYTEC worker, guardian, meeting, research, provider, or reliability
tool is exposed to Notion. No background worker or autonomous loop is started.
"""
from __future__ import annotations

import atexit
import asyncio
import json
import os
import threading
import time
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI
from starlette.middleware import Middleware
import uvicorn

import server as legacy_server
from meeting_bus import MeetingBusMiddleware
from dan_relay_http import DanRelayMiddleware
from dan_cognition_http import DanCognitionMiddleware
from five_seat_authority import PostgresFabricAuthority
from five_seat_queue import PostgresFabricQueue
from five_seat_reporting import FiveSeatReporter
from five_seat_service import FiveSeatFabricService, submit_low_risk_task_packet
from five_seat_production_admission import (
    run_probe as run_production_admission_probe,
    wait_for_local_watch,
)
from circuit_breaker import CircuitBreaker
from orchestration import ExecutionRegistry
from notion_courier_policy import (
    CourierPolicyError,
    completion_payload,
    parse_courier_command,
    rejection_payload,
)


class CourierRuntime(Protocol):
    def status(self) -> str: ...
    def execute(self, packet_json: str) -> str: ...


class JaytecCourierRuntime:
    """Synchronous, request-scoped JAYTEC runtime with no background loops."""

    def __init__(self) -> None:
        legacy_server._require_startup_prereqs()

        if legacy_server.DATABASE_URL:
            from idempotency_postgres import PostgresExecutionRegistry

            self.registry: ExecutionRegistry = PostgresExecutionRegistry(
                database_url=legacy_server.DATABASE_URL,
                ttl_seconds=ExecutionRegistry().ttl_seconds,
            )
            self.registry.ensure_schema()
            self.idempotency_store = "postgres"
        else:
            self.registry = ExecutionRegistry()
            self.idempotency_store = "process_memory"

        self.codex_circuit = CircuitBreaker(
            failure_threshold=legacy_server.CIRCUIT_FAILURE_THRESHOLD,
            reset_after_seconds=legacy_server.CIRCUIT_RESET_SECONDS,
        )
        self.gemini_circuit = CircuitBreaker(
            failure_threshold=legacy_server.CIRCUIT_FAILURE_THRESHOLD,
            reset_after_seconds=legacy_server.CIRCUIT_RESET_SECONDS,
        )
        self.sol_circuit = CircuitBreaker(
            failure_threshold=1,
            reset_after_seconds=max(legacy_server.CIRCUIT_RESET_SECONDS, 300),
        )

        self.openrouter_client = (
            OpenAI(
                api_key=legacy_server.OPENROUTER_API_KEY,
                base_url=legacy_server.OPENROUTER_BASE_URL,
                max_retries=0,
            )
            if legacy_server.OPENROUTER_API_KEY
            else None
        )

        self.sol_gateway_client = (
            OpenAI(
                api_key=legacy_server.AI_GATEWAY_API_KEY,
                base_url=legacy_server.SOL_GATEWAY_URL,
            )
            if legacy_server.AI_GATEWAY_API_KEY
            else None
        )

        if self.openrouter_client is not None:
            self.codex_dispatch = legacy_server.build_codex_dispatch(
                openai_client=self.openrouter_client,
                codex_model=legacy_server.CODEX_MODEL,
                circuit=self.codex_circuit,
                **legacy_server.engineering_dispatch_kwargs(),
            )
            self.gemini_dispatch = legacy_server.build_gemini_dispatch(
                openrouter_client=self.openrouter_client,
                gemini_model=legacy_server.GEMINI_MODEL,
                gemini_timeout_s=legacy_server.DURABLE_GEMINI_TIMEOUT_S,
                circuit=self.gemini_circuit,
            )
        else:
            self.codex_dispatch = self.codex_circuit.guard(
                lambda _packet: (_ for _ in ()).throw(
                    RuntimeError("OPENROUTER_API_KEY is not configured")
                )
            )
            self.gemini_dispatch = self.gemini_circuit.guard(
                lambda _packet: (_ for _ in ()).throw(
                    RuntimeError("OPENROUTER_API_KEY is not configured")
                )
            )

        if (
            self.sol_gateway_client is not None
            and legacy_server.SOL_RESERVE_ENABLED
            and legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED
        ):
            self.sol_dispatch = legacy_server.build_sol_reserve_dispatch(
                gateway_client=self.sol_gateway_client,
                gateway_api_key=legacy_server.AI_GATEWAY_API_KEY,
                circuit=self.sol_circuit,
                credit_balance_fn=lambda: legacy_server.fetch_vercel_gateway_credit_balance(
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
            )
        else:
            self.sol_dispatch = self.sol_circuit.guard(
                lambda _packet: (_ for _ in ()).throw(
                    RuntimeError(
                        "Sol reserve unavailable: zero-spend gates are not fully satisfied"
                    )
                )
            )

        self.production_ready = legacy_server.compute_production_ready(
            runtime_mode=legacy_server.RUNTIME_MODE,
            idempotency_store=self.idempotency_store,
            codex_model=legacy_server.CODEX_MODEL,
            gemini_model=legacy_server.GEMINI_MODEL,
            mcp_auth_token_present=bool(legacy_server.MCP_AUTH_TOKEN),
            openai_api_key_present=bool(legacy_server.OPENAI_API_KEY),
            openrouter_api_key_present=bool(legacy_server.OPENROUTER_API_KEY),
        )

        self.fabric_enabled = legacy_server.five_seat_fabric_enabled()
        self.fabric_queue = None
        self.fabric_authority = None
        self.fabric_reporter = None
        self.fabric_service = None
        self.fabric_instance_id = None
        if self.fabric_enabled:
            if not legacy_server.DATABASE_URL:
                raise RuntimeError(
                    "FIVE_SEAT_FABRIC_ENABLED requires DATABASE_URL"
                )
            self.fabric_queue = PostgresFabricQueue(legacy_server.DATABASE_URL)
            self.fabric_authority = PostgresFabricAuthority(legacy_server.DATABASE_URL)
            self.fabric_reporter = FiveSeatReporter(legacy_server.DATABASE_URL)
            self.fabric_instance_id = (
                os.environ.get("RENDER_INSTANCE_ID", "").strip() or "courier"
            )
            self.fabric_service = FiveSeatFabricService(
                legacy_server.DATABASE_URL,
                self._execute_direct,
                instance_id=self.fabric_instance_id,
                lease_seconds=int(os.environ.get("FIVE_SEAT_LEASE_S", "300")),
                guardian_interval_seconds=float(
                    os.environ.get("FIVE_SEAT_GUARDIAN_INTERVAL_S", "30")
                ),
                report_interval_seconds=float(
                    os.environ.get("FIVE_SEAT_REPORT_INTERVAL_S", "3600")
                ),
            )
            # Read-only schema gate. Startup never applies transformation DDL.
            self.fabric_service.verify_ready()
            self.fabric_service.start()
            atexit.register(self.fabric_service.stop)
            if (
                os.environ.get(
                    "FIVE_SEAT_FABRIC_STARTUP_REPORT", "0"
                ).strip()
                == "1"
            ):
                self._emit_fabric_startup_report()
            self._maybe_start_production_admission_probe()

    def _maybe_start_production_admission_probe(self) -> None:
        if (
            os.environ.get(
                "FIVE_SEAT_PROD_ADMISSION_PROBE", "0"
            ).strip()
            != "1"
        ):
            return
        if (
            not self.fabric_enabled
            or not self.fabric_queue
            or not self.fabric_authority
            or not self.fabric_service
            or not self.fabric_reporter
        ):
            print(
                "FIVE_SEAT_PROD_ADMISSION_PROBE="
                + json.dumps(
                    {
                        "schema_version": "JAYTEC_FS08_PRODUCTION_ADMISSION_PROBE_V1",
                        "passed": False,
                        "error_class": "FABRIC_NOT_READY",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return
        deadline = os.environ.get(
            "FIVE_SEAT_PROD_ADMISSION_DEADLINE", ""
        ).strip()
        if not deadline:
            print(
                "FIVE_SEAT_PROD_ADMISSION_PROBE="
                + json.dumps(
                    {
                        "schema_version": "JAYTEC_FS08_PRODUCTION_ADMISSION_PROBE_V1",
                        "passed": False,
                        "error_class": "PROBE_DEADLINE_MISSING",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return
        thread = threading.Thread(
            target=self._run_production_admission_probe,
            args=(deadline,),
            name="fs08-production-admission-probe",
            daemon=True,
        )
        thread.start()

    def _run_production_admission_probe(self, deadline: str) -> None:
        try:
            expected_watch_leader = (
                "five-seat-watch:" + str(self.fabric_instance_id or "")
            )
            wait_for_local_watch(
                reporter=self.fabric_reporter,
                expected_leader=expected_watch_leader,
                timeout_seconds=float(
                    os.environ.get(
                        "FIVE_SEAT_PROD_ADMISSION_WATCH_WAIT_S",
                        "90",
                    )
                ),
                stable_seconds=float(
                    os.environ.get(
                        "FIVE_SEAT_PROD_ADMISSION_WATCH_STABLE_S",
                        "15",
                    )
                ),
            )
            result = run_production_admission_probe(
                database_url=legacy_server.DATABASE_URL,
                queue=self.fabric_queue,
                authority=self.fabric_authority,
                service=self.fabric_service,
                reporter=self.fabric_reporter,
                deadline=deadline,
            )
        except Exception as exc:
            error_text = str(exc)
            error_code = "UNCLASSIFIED_RUNTIME_ERROR"
            if error_text == "fabric_authority_state_uninitialized":
                error_code = "FABRIC_AUTHORITY_STATE_UNINITIALIZED"
            elif error_text == "admission_probe_job_id_missing":
                error_code = "ADMISSION_PROBE_JOB_ID_MISSING"
            elif error_text.startswith("stale_source_shared_state_version:"):
                error_code = "STALE_SOURCE_SHARED_STATE_VERSION"
            elif error_text == "admission_probe_local_watch_not_ready":
                error_code = "LOCAL_WATCH_NOT_READY"
            result = {
                "schema_version": "JAYTEC_FS08_PRODUCTION_ADMISSION_PROBE_V1",
                "passed": False,
                "error_class": type(exc).__name__,
                "error_code": error_code,
            }
        print(
            "FIVE_SEAT_PROD_ADMISSION_PROBE="
            + json.dumps(result, sort_keys=True, default=str),
            flush=True,
        )

    def _emit_fabric_startup_report(self) -> None:
        """Emit a bounded read-only fabric/WATCH startup snapshot.

        This diagnostic never changes queue, seat, WATCH, provider, or
        authority state. It is intentionally non-fatal: the existing schema
        gate remains the startup fail-closed mechanism while this report gives
        operators concrete activation evidence without exposing credentials.
        """
        if not self.fabric_service or not self.fabric_reporter:
            return

        expected_seats = [
            f"WORKER-SEAT-{index}" for index in range(1, 6)
        ]
        expected_leader = (
            "five-seat-watch:" + str(self.fabric_instance_id or "courier")
        )
        deadline = time.monotonic() + 2.0
        snapshot: dict[str, Any] = {
            "schema_version": "JAYTEC_FIVE_SEAT_STARTUP_REPORT_V1",
            "ready": False,
        }
        try:
            while True:
                service = self.fabric_service.status()
                authority_state = (
                    self.fabric_authority.current_state()
                    if self.fabric_authority is not None
                    else {}
                )
                authority_source_version = int(
                    authority_state.get("current_shared_state_version") or 0
                )
                report = self.fabric_reporter.last_60_minutes(
                    window_minutes=60
                )
                watch = dict(report.get("watch") or {})
                summary = dict(report.get("summary") or {})
                seats = list(report.get("seats") or [])
                seat_ids = [
                    str(item.get("seat_id") or "")
                    for item in seats
                    if isinstance(item, dict)
                ]
                errors = list(service.get("errors") or [])
                snapshot = {
                    "schema_version": "JAYTEC_FIVE_SEAT_STARTUP_REPORT_V1",
                    "service_id": service.get("service_id"),
                    "worker_threads_configured": int(
                        service.get("worker_threads_configured") or 0
                    ),
                    "worker_threads_alive": int(
                        service.get("worker_threads_alive") or 0
                    ),
                    "seat_count": int(service.get("seat_count") or 0),
                    "seat_ids": seat_ids,
                    "seats_free": int(summary.get("seats_free") or 0),
                    "watch_healthy": bool(watch.get("healthy")),
                    "watch_leader_present": bool(watch.get("leader")),
                    "watch_leader_matches_instance": (
                        str(watch.get("leader") or "") == expected_leader
                    ),
                    "authority_source_shared_state_version": authority_source_version,
                    "authority_ready": authority_source_version > 0,
                    "fabric_error_count": len(errors),
                }
                snapshot["ready"] = bool(
                    snapshot["worker_threads_configured"] == 5
                    and snapshot["worker_threads_alive"] == 5
                    and snapshot["seat_count"] == 5
                    and seat_ids == expected_seats
                    and snapshot["watch_healthy"]
                    and snapshot["watch_leader_matches_instance"]
                    and snapshot["authority_ready"]
                    and snapshot["fabric_error_count"] == 0
                )
                if snapshot["ready"] or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        except Exception as exc:
            snapshot = {
                "schema_version": "JAYTEC_FIVE_SEAT_STARTUP_REPORT_V1",
                "ready": False,
                "error_class": type(exc).__name__,
            }
        print(
            "FIVE_SEAT_FABRIC_STARTUP_REPORT="
            + json.dumps(snapshot, sort_keys=True, default=str),
            flush=True,
        )

    def status(self) -> str:
        payload = json.loads(
            legacy_server._orchestration_status_json(
                runtime_mode=legacy_server.RUNTIME_MODE,
                codex_model=legacy_server.CODEX_MODEL,
                gemini_model=legacy_server.GEMINI_MODEL,
                codex_circuit=self.codex_circuit.snapshot(),
                gemini_circuit=self.gemini_circuit.snapshot(),
                idempotency_store=self.idempotency_store,
                production_ready=self.production_ready,
            )
        )
        payload.update(
            {
                "sol_model": legacy_server.EXPECTED_SOL_MODEL,
                "sol_gateway_key_present": bool(legacy_server.AI_GATEWAY_API_KEY),
                "sol_reserve_flag_enabled": bool(legacy_server.SOL_RESERVE_ENABLED),
                "sol_free_credit_attested": bool(legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED),
                "sol_reserve_enabled": bool(
                    self.sol_gateway_client is not None
                    and legacy_server.SOL_RESERVE_ENABLED
                    and legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED
                ),
                "sol_circuit": self.sol_circuit.snapshot(),
            }
        )
        if self.fabric_enabled and self.fabric_service and self.fabric_reporter:
            report = self.fabric_reporter.last_60_minutes(window_minutes=60)
            payload.update(
                {
                    "five_seat_fabric_enabled": True,
                    "preferred_execution": "WATCH_CONTROLLED_FIVE_SEAT",
                    "five_seat_service": self.fabric_service.status(),
                    "watch": report.get("watch"),
                    "watch_summary": report.get("summary"),
                    "jay_action_required": report.get("jay_action_required"),
                    "recent_owner_notifications": report.get("owner_notifications", [])[-20:],
                }
            )
        else:
            payload["five_seat_fabric_enabled"] = False
        return json.dumps(payload, sort_keys=True, default=str)

    def _execute_direct(self, packet_json: str) -> str:
        return legacy_server._execute_task_packet_json(
            packet_json,
            registry=self.registry,
            idempotency_store=self.idempotency_store,
            codex_dispatch=self.codex_dispatch,
            gemini_dispatch=self.gemini_dispatch,
            sol_dispatch=self.sol_dispatch,
        )

    def execute(self, packet_json: str) -> str:
        # Compatibility/test runtimes built without __init__ fail safely to the
        # legacy direct path rather than pretending the five-seat fabric is on.
        if not getattr(self, "fabric_enabled", False):
            return self._execute_direct(packet_json)
        if self.fabric_queue is None or self.fabric_authority is None:
            return json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "reason": "FIVE_SEAT_FABRIC_NOT_READY",
                },
                sort_keys=True,
            )
        try:
            authority = self.fabric_authority.current_state()
            source_version = int(authority.get("current_shared_state_version") or 0)
            if source_version <= 0:
                raise RuntimeError("fabric_authority_state_uninitialized")
            snapshot = submit_low_risk_task_packet(
                self.fabric_queue,
                packet_json=packet_json,
                source_shared_state_version=source_version,
                priority=100,
            )
            return json.dumps(
                {
                    "status": "ACCEPTED",
                    "execution_mode": "WATCH_CONTROLLED_FIVE_SEAT",
                    "job_id": snapshot.get("job_id"),
                    "task_id": snapshot.get("task_id"),
                    "fabric_state": snapshot.get("fabric_state"),
                    "source_shared_state_version": source_version,
                    "result_delivery": "WATCH_ATTESTED_DURABLE_OUTBOX",
                },
                sort_keys=True,
                default=str,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "reason": type(exc).__name__ + ":" + str(exc)[:800],
                },
                sort_keys=True,
            )


def create_mcp_app(runtime: CourierRuntime | None = None) -> FastMCP:
    """Create a one-tool authenticated courier server."""
    if not legacy_server.MCP_AUTH_TOKEN:
        raise RuntimeError("MCP_AUTH_TOKEN is required")

    auth = StaticTokenVerifier(
        tokens={
            legacy_server.MCP_AUTH_TOKEN: {
                "sub": "notion-pass-through",
                "client_id": "jaytec-notion-pass-through",
            }
        }
    )
    mcp = FastMCP("JAYTEC Notion Courier", auth=auth)
    courier_runtime = runtime or JaytecCourierRuntime()

    @mcp.tool
    def collaborate(
        task: str,
        notion_analysis: str = "",
        context: str = "",
    ) -> str:
        """Transport one exact JAYTEC command. Return its result verbatim, then stop. Never reason, expand, retry, summarize, route, open chats, or follow up."""
        try:
            command = parse_courier_command(
                task,
                notion_analysis=notion_analysis,
                context=context,
            )
        except CourierPolicyError as exc:
            return rejection_payload(str(exc))

        if command.operation == "status":
            return completion_payload("status", courier_runtime.status())
        if command.operation == "execute_task_packet" and command.packet_json:
            return completion_payload(
                "execute_task_packet",
                courier_runtime.execute(command.packet_json),
            )
        return rejection_payload("UNREACHABLE_OPERATION")

    return mcp


async def _assert_one_tool_catalog(mcp: FastMCP) -> None:
    tools = await mcp.list_tools()
    names = [tool.name for tool in tools]
    if names != ["collaborate"]:
        raise RuntimeError(
            "NOTION_COURIER_CATALOG_UNSAFE:" + ",".join(names)
        )


def _assert_policy_fail_closed() -> None:
    attacks = (
        "think for yourself",
        "open a new chat",
        "continue the previous chat",
        "research this",
        "route this to another agent",
        "retry until it works",
        "use Notion AI",
        "follow up autonomously",
        "JAYTEC_ORCHESTRATION_STATUS then continue",
    )
    for attack in attacks:
        try:
            parse_courier_command(attack)
        except CourierPolicyError:
            continue
        raise RuntimeError(
            "NOTION_COURIER_POLICY_UNSAFE:" + attack[:80]
        )


def assert_courier_startup_invariants(mcp: FastMCP) -> None:
    _assert_policy_fail_closed()
    asyncio.run(_assert_one_tool_catalog(mcp))



def create_http_app(mcp: FastMCP | None = None):
    """Expose the locked Notion courier plus the separate authenticated meeting bus.

    MeetingBusMiddleware is a raw HTTP surface only; it does not add MCP tools
    to Notion and therefore cannot widen the one-tool courier catalog.
    """
    server = mcp or create_mcp_app()
    return server.http_app(
        middleware=[
            Middleware(DanCognitionMiddleware),
            Middleware(DanRelayMiddleware),
            Middleware(MeetingBusMiddleware),
        ],
        stateless_http=True,
        host_origin_protection=False,
    )




def main() -> None:
    safe_preflight = {
        "key_present": bool(legacy_server.AI_GATEWAY_API_KEY),
        "reserve_enabled": bool(legacy_server.SOL_RESERVE_ENABLED),
        "free_credit_attested": bool(legacy_server.SOL_FREE_CREDIT_ONLY_ATTESTED),
    }
    print("JAYTEC_SOL_PREFLIGHT=" + json.dumps(safe_preflight, sort_keys=True), flush=True)

    # Read-only diagnostic: checks Gateway credit balance only. It cannot invoke
    # any model, consume inference tokens, purchase credits, or change routing.
    if os.environ.get("SOL_CREDIT_BALANCE_PROBE_ON_STARTUP", "0").strip() == "1":
        try:
            balance = legacy_server.fetch_vercel_gateway_credit_balance(
                api_key=legacy_server.AI_GATEWAY_API_KEY,
                base_url=legacy_server.SOL_GATEWAY_URL,
            )
            print(
                "JAYTEC_SOL_CREDIT_BALANCE="
                + json.dumps({"ok": True, "balance_usd": str(balance)}, sort_keys=True),
                flush=True,
            )
        except Exception as exc:
            print(
                "JAYTEC_SOL_CREDIT_BALANCE="
                + json.dumps(
                    {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:160]},
                    sort_keys=True,
                ),
                flush=True,
            )

    mcp = create_mcp_app()
    assert_courier_startup_invariants(mcp)
    uvicorn.run(
        create_http_app(mcp),
        host="0.0.0.0",
        port=legacy_server.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
