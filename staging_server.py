"""STAGING ONLY: unified JAYTEC execute_task_packet MCP surface.

This entrypoint is intentionally independent from production server.py so the
staging service can boot and validate packets without production provider keys.
Specialist dispatch fails closed until the corresponding server-side credential
is configured. Do not promote until BRIDGE_CONTRACT_V1.md activation gates pass.

New in C4: optional Postgres-backed idempotency via DATABASE_URL.
- If DATABASE_URL is set, staging uses durable idempotency and MUST fail closed
  if DB is unavailable (no silent durable->memory fallback).
- If DATABASE_URL is not set, staging stays on process memory.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

from circuit_breaker import CircuitBreaker
from orchestration import ExecutionRegistry, PacketValidationError, execute_task_packet_core, parse_packet_json
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    build_codex_dispatch,
    build_gemini_dispatch,
)

PORT = int(os.environ.get("PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
CODEX_MODEL = os.environ.get("CODEX_MODEL", EXPECTED_CODEX_MODEL).strip()
ENGINEERING_PROVIDER_MODE = os.environ.get("ENGINEERING_PROVIDER_MODE", "LOCKED_RESERVE").strip().upper()
ENGINEERING_SOL_OUTPUT_TOKEN_CAP = int(os.environ.get("ENGINEERING_SOL_OUTPUT_TOKEN_CAP", "2000"))
ENGINEERING_SOL_MAX_PACKET_RETRIES = int(os.environ.get("ENGINEERING_SOL_MAX_PACKET_RETRIES", "1"))
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "90"))
CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("CIRCUIT_FAILURE_THRESHOLD", "3"))
CIRCUIT_RESET_SECONDS = int(os.environ.get("CIRCUIT_RESET_SECONDS", "60"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not MCP_AUTH_TOKEN:
    raise RuntimeError("MCP_AUTH_TOKEN is required")

auth = StaticTokenVerifier(
    tokens={
        MCP_AUTH_TOKEN: {
            "sub": "jaytec-staging-client",
            "client_id": "jaytec-orchestration-staging",
        }
    }
)
mcp = FastMCP("JAYTEC Orchestration Staging", auth=auth)

# --- Idempotency registry selection (staging only) ---
if DATABASE_URL:
    from idempotency_postgres import PostgresExecutionRegistry

    REGISTRY = PostgresExecutionRegistry(database_url=DATABASE_URL, ttl_seconds=ExecutionRegistry().ttl_seconds)
    REGISTRY.ensure_schema()
    IDEMPOTENCY_STORE = "postgres"
else:
    REGISTRY = ExecutionRegistry()
    IDEMPOTENCY_STORE = "process_memory_staging_only"

CODEX_CIRCUIT = CircuitBreaker(
    failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
    reset_after_seconds=CIRCUIT_RESET_SECONDS,
)
GEMINI_CIRCUIT = CircuitBreaker(
    failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
    reset_after_seconds=CIRCUIT_RESET_SECONDS,
)

OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
OPENROUTER_CLIENT = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL) if OPENROUTER_API_KEY else None

# Build dispatchers ONCE to avoid runtime drift and repeated guards.
CODEX_DISPATCH = (
    build_codex_dispatch(openai_client=OPENAI_CLIENT, codex_model=CODEX_MODEL, circuit=CODEX_CIRCUIT, provider_mode=ENGINEERING_PROVIDER_MODE, max_output_tokens=ENGINEERING_SOL_OUTPUT_TOKEN_CAP, max_packet_retries=ENGINEERING_SOL_MAX_PACKET_RETRIES)
    if OPENAI_CLIENT
    else CODEX_CIRCUIT.guard(lambda _packet: (_ for _ in ()).throw(RuntimeError("OPENAI_API_KEY is not configured on the staging bridge")))
)

GEMINI_DISPATCH = (
    build_gemini_dispatch(
        openrouter_client=OPENROUTER_CLIENT,
        gemini_model=GEMINI_MODEL,
        gemini_timeout_s=GEMINI_TIMEOUT_S,
        circuit=GEMINI_CIRCUIT,
    )
    if OPENROUTER_CLIENT
    else GEMINI_CIRCUIT.guard(lambda _packet: (_ for _ in ()).throw(RuntimeError("OPENROUTER_API_KEY is not configured on the staging bridge")))
)


@mcp.tool
def orchestration_status() -> str:
    return json.dumps(
        {
            "status": "STAGING",
            "operation": "execute_task_packet",
            "codex_model": CODEX_MODEL,
            "codex_adapter_configured": bool(OPENAI_API_KEY),
            "codex_circuit": CODEX_CIRCUIT.snapshot(),
            "gemini_model": GEMINI_MODEL,
            "gemini_adapter_configured": bool(OPENROUTER_API_KEY),
            "gemini_provider_routing": "price",
            "gemini_circuit": GEMINI_CIRCUIT.snapshot(),
            "idempotency_store": IDEMPOTENCY_STORE,
            "production_ready": False,
        },
        sort_keys=True,
    )


@mcp.tool
def idempotency_persistence_probe() -> str:
    if IDEMPOTENCY_STORE != "postgres":
        return json.dumps({"status": "SKIP", "idempotency_store": IDEMPOTENCY_STORE}, sort_keys=True)
    try:
        pruned = REGISTRY.prune()
        return json.dumps({"status": "PASS", "idempotency_store": IDEMPOTENCY_STORE, "pruned": pruned}, sort_keys=True)
    except Exception as exc:
        return json.dumps({"status": "FAIL", "idempotency_store": IDEMPOTENCY_STORE, "error": type(exc).__name__}, sort_keys=True)


@mcp.tool
def execute_task_packet(packet_json: str) -> str:
    packet, parse_errors = parse_packet_json(packet_json)
    if packet is None:
        return json.dumps(
            {
                "execution_id": "invalid",
                "task_id": "",
                "subtask_id": "",
                "overall_status": "INVALID_PACKET",
                "unresolved_items": list(parse_errors),
                "return_schema_version": "1.0",
            },
            sort_keys=True,
        )

    def _lookup(key: str, digest: str, now=None):
        try:
            return REGISTRY.lookup(key, digest, now=now)
        except ValueError as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            raise

    def _store(key: str, digest: str, result, now=None):
        try:
            return REGISTRY.store(key, digest, result, now=now)
        except ValueError as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            raise

    if IDEMPOTENCY_STORE == "postgres":
        class _Wrapper(ExecutionRegistry):
            def lookup(self, key, packet_hash, *, now=None):
                return _lookup(key, packet_hash, now=now)

            def store(self, key, packet_hash, result, *, now=None):
                return _store(key, packet_hash, result, now=now)

        registry = _Wrapper()
    else:
        registry = REGISTRY

    result = execute_task_packet_core(
        packet,
        {"codex": CODEX_DISPATCH, "gemini": GEMINI_DISPATCH},
        registry,
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        host_origin_protection=False,
    )
