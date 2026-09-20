"""Minimal Notion-facing JAYTEC MCP courier.

The exposed MCP catalog contains exactly one tool: collaborate.
That tool can only:
  * return JAYTEC orchestration status; or
  * forward one exact JAYTEC task packet.

No native JAYTEC worker, guardian, meeting, research, provider, or reliability
tool is exposed to Notion. No background worker or autonomous loop is started.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

import server as legacy_server
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

        self.openrouter_client = (
            OpenAI(
                api_key=legacy_server.OPENROUTER_API_KEY,
                base_url=legacy_server.OPENROUTER_BASE_URL,
            )
            if legacy_server.OPENROUTER_API_KEY
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
        return legacy_server._orchestration_status_json(
            runtime_mode=legacy_server.RUNTIME_MODE,
            codex_model=legacy_server.CODEX_MODEL,
            gemini_model=legacy_server.GEMINI_MODEL,
            codex_circuit=self.codex_circuit.snapshot(),
            gemini_circuit=self.gemini_circuit.snapshot(),
            idempotency_store=self.idempotency_store,
            production_ready=self.production_ready,
        )

    def execute(self, packet_json: str) -> str:
        return legacy_server._execute_task_packet_json(
            packet_json,
            registry=self.registry,
            idempotency_store=self.idempotency_store,
            codex_dispatch=self.codex_dispatch,
            gemini_dispatch=self.gemini_dispatch,
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
