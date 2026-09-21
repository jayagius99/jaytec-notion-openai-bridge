"""Minimal Notion-facing JAYTEC MCP courier.

The exposed MCP catalog contains exactly one tool: collaborate.
That tool can only:
  * return JAYTEC orchestration status; or
  * forward one exact JAYTEC task packet.

No native JAYTEC worker, guardian, meeting, research, provider, or reliability
tool is exposed to Notion. No background worker or autonomous loop is started.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI
from starlette.middleware import Middleware
import uvicorn

import server as legacy_server
from meeting_bus import MeetingBusMiddleware
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
                gemini_timeout_s=legacy_server.GEMINI_TIMEOUT_S,
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
        return json.dumps(payload, sort_keys=True)

    def execute(self, packet_json: str) -> str:
        return legacy_server._execute_task_packet_json(
            packet_json,
            registry=self.registry,
            idempotency_store=self.idempotency_store,
            codex_dispatch=self.codex_dispatch,
            gemini_dispatch=self.gemini_dispatch,
            sol_dispatch=self.sol_dispatch,
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
        middleware=[Middleware(MeetingBusMiddleware)],
        stateless_http=True,
        host_origin_protection=False,
    )




def _run_durable_sol_live_proof_once() -> None:
    """Run at most one owner-authorized live Sol proof for a durable proof ID.

    A Postgres claim is committed before inference. Any concurrent or later
    startup with the same proof ID skips without calling a model.
    """
    if os.environ.get("SOL_DURABLE_PROOF_ON_STARTUP", "0").strip() != "1":
        return

    proof_id = os.environ.get(
        "SOL_DURABLE_PROOF_ID",
        "JAYTEC_SOL_DURABLE_LIVE_PROOF_20260922_V1",
    ).strip()
    if not proof_id or len(proof_id) > 200:
        print('JAYTEC_SOL_DURABLE_PROOF={"ok":false,"error":"invalid_proof_id"}', flush=True)
        return
    if not legacy_server.DATABASE_URL:
        print('JAYTEC_SOL_DURABLE_PROOF={"ok":false,"error":"database_required"}', flush=True)
        return

    import psycopg2

    claimed = False
    with psycopg2.connect(legacy_server.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS jaytec_one_shot_proof_claims (
                    proof_id TEXT PRIMARY KEY,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO jaytec_one_shot_proof_claims (proof_id)
                VALUES (%s)
                ON CONFLICT (proof_id) DO NOTHING
                RETURNING proof_id
                """,
                (proof_id,),
            )
            claimed = cur.fetchone() is not None

    if not claimed:
        print(
            "JAYTEC_SOL_DURABLE_PROOF="
            + json.dumps({"ok": False, "skipped": "already_claimed", "proof_id": proof_id}, sort_keys=True),
            flush=True,
        )
        return

    runtime = JaytecCourierRuntime()
    now = datetime.now(timezone.utc)
    packet = {
        "packet_version": "1.0",
        "task_id": "JAYTEC-SOL-LIVE-PROOF-001",
        "subtask_id": "JAYTEC-SOL-LIVE-PROOF-001-A",
        "parent_task_id": "JAYTEC-SOL-LIVE-PROOF-001",
        "request": "Return a minimal readiness acknowledgement for this bounded live proof.",
        "intent": "verify live GPT-5.6 Sol reserve routing",
        "workflow_id": "JAYTEC_OWNER_SOL_LIVE_PROOF",
        "risk_level": "low",
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "owner_explicit_sol_request": True,
            "proof_scope": "minimal reachability only",
        },
        "context_digests": {},
        "known_facts": [],
        "constraints": [
            "no side effects",
            "zero retries",
            "no paid fallback",
            "exact Sol model only",
        ],
        "specialist_plan": ["sol"],
        "allowed_operations": ["analyze", "validate"],
        "expected_output": "minimal structured readiness confirmation",
        "validation_requirements": [
            "exact model openai/gpt-5.6-sol",
            "no side effects",
            "no fallback models",
        ],
        "side_effect_policy": "none",
        "idempotency_key": proof_id,
        "deadline": (now + timedelta(minutes=3)).isoformat(),
        "max_fanout": 1,
        "max_retries": 0,
        "return_schema_version": "1.0",
    }

    try:
        envelope = json.loads(runtime.execute(json.dumps(packet, sort_keys=True)))
        sol = envelope.get("sol_result") or {}
        diag = sol.get("bridge_diagnostics") or {}
        safe = {
            "ok": envelope.get("overall_status") == "SUCCESS"
            and sol.get("model") == legacy_server.EXPECTED_SOL_MODEL,
            "overall_status": envelope.get("overall_status"),
            "model": sol.get("model"),
            "gateway": diag.get("gateway"),
            "provider_only": diag.get("provider_only"),
            "model_lock": diag.get("model_lock"),
            "fallback_models": diag.get("fallback_models"),
            "zero_spend_attested": diag.get("zero_spend_attested"),
            "credit_balance_before_usd": diag.get("credit_balance_before_usd"),
            "credit_balance_after_usd": diag.get("credit_balance_after_usd"),
            "credit_used_usd": diag.get("credit_used_usd"),
            "idempotent_replay": (envelope.get("usage_summary") or {}).get("idempotent_replay"),
            "proof_id": proof_id,
        }
        print("JAYTEC_SOL_DURABLE_PROOF=" + json.dumps(safe, sort_keys=True), flush=True)
    except Exception as exc:
        print(
            "JAYTEC_SOL_DURABLE_PROOF="
            + json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:160],
                    "proof_id": proof_id,
                },
                sort_keys=True,
            ),
            flush=True,
        )



def main() -> None:
    _run_durable_sol_live_proof_once()
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
