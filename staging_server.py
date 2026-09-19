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

import base64
import gzip
import hashlib
import json
import os
from typing import Any, Mapping

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

from circuit_breaker import CircuitBreaker
from jaytec_read import build_jaytec_read_packet, enforce_read_report
from orchestration import ExecutionRegistry, PacketValidationError, execute_task_packet_core, parse_packet_json
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    build_codex_dispatch,
    build_engineering_dispatch,
    build_gemini_dispatch,
    ENGINEERING_PROVIDER_ACTIVE,
    resolve_engineering_model,
    resolve_engineering_provider_mode,
)

PORT = int(os.environ.get("PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ENGINEERING_MODEL = resolve_engineering_model()
CODEX_MODEL = ENGINEERING_MODEL
ENGINEERING_PROVIDER_MODE = resolve_engineering_provider_mode()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "90"))
CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("CIRCUIT_FAILURE_THRESHOLD", "3"))
CIRCUIT_RESET_SECONDS = int(os.environ.get("CIRCUIT_RESET_SECONDS", "60"))
DATABASE_URL = (
    os.environ.get("JAYTEC_STAGING_DATABASE_URL", "").strip()
    or os.environ.get("DATABASE_URL", "").strip()
)

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

OPENAI_CLIENT = (
    OpenAI(api_key=OPENAI_API_KEY)
    if ENGINEERING_PROVIDER_MODE == ENGINEERING_PROVIDER_ACTIVE and OPENAI_API_KEY
    else None
)
OPENROUTER_CLIENT = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL) if OPENROUTER_API_KEY else None

# Build dispatchers ONCE to avoid runtime drift and repeated guards.
ENGINEERING_DISPATCH = (
    build_engineering_dispatch(openai_client=OPENAI_CLIENT, engineering_model=ENGINEERING_MODEL, circuit=CODEX_CIRCUIT)
    if OPENAI_CLIENT
    else CODEX_CIRCUIT.guard(
        lambda _packet: (_ for _ in ()).throw(RuntimeError("ENGINEERING_PROVIDER_DOOR_LOCKED_RESERVE"))
    )
)
CODEX_DISPATCH = ENGINEERING_DISPATCH  # TaskPacket v1 wire alias

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
            "engineering_model": ENGINEERING_MODEL,
            "engineering_provider_mode": ENGINEERING_PROVIDER_MODE,
            "codex_model": CODEX_MODEL,  # legacy compatibility field
            "codex_adapter_configured": bool(OPENAI_CLIENT),
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


def _run_jaytec_read_bootstrap_probe() -> None:
    """STAGING-ONLY one-shot JAYTEC:READ diagnostic.

    Activated only when JAYTEC_READ_BOOTSTRAP_URL is set in the staging
    environment. It performs no writes and logs only the validated READ result.
    """
    target = os.environ.get("JAYTEC_READ_BOOTSTRAP_URL", "").strip()
    if not target:
        return
    packet = build_jaytec_read_packet(
        target,
        request_suffix="This is a one-shot staging recovery read requested by Jay. Return source-specific evidence only.",
        validation_suffix=["Return the exact shared-chat content needed to resume the paused JAYTEC security work."],
    )
    result = execute_task_packet_core(
        packet,
        {"gemini": GEMINI_DISPATCH},
        REGISTRY,
    )
    validated = enforce_read_report(result, target)
    print(
        "JAYTEC_READ_BOOTSTRAP_RESULT="
        + json.dumps(validated, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _run_god_project_review_probe() -> None:
    """STAGING-ONLY independent review of the pinned GOD Project v2.0 packet."""
    encoded = os.environ.get("JAYTEC_GOD_PROJECT_REVIEW_PACKET_GZ_B64", "").strip()
    expected_sha = os.environ.get("JAYTEC_GOD_PROJECT_REVIEW_PACKET_SHA256", "").strip()
    enabled = os.environ.get("JAYTEC_GOD_PROJECT_REVIEW_ENABLED", "").strip() == "1"
    if not enabled:
        return

    result = {
        "status": "FAILED_CLOSED",
        "model": "deepseek/deepseek-v4-flash-0731:free",
        "ready_to_activate": False,
        "findings": [],
        "blockers": [],
        "required_changes": [],
        "confidence": None,
        "packet_sha256": None,
    }

    try:
        if not encoded or not expected_sha:
            raise RuntimeError("review_packet_missing")
        packet = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
        packet_sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()
        result["packet_sha256"] = packet_sha
        if packet_sha != expected_sha:
            raise RuntimeError("review_packet_hash_mismatch")
        if OPENROUTER_CLIENT is None:
            raise RuntimeError("openrouter_client_unavailable")

        review_model = "deepseek/deepseek-v4-flash-0731:free"
        prompt = """JAYTEC GOD PROJECT v2.0 — INDEPENDENT HOSTILE REVIEW

You are JAYTEC's independent review specialist. You are subordinate and have no execution authority.

Review the exact launch packet below. Challenge it aggressively for:
1. authority hierarchy contradictions;
2. any wording that could let ChatGPT/Project/specialists impersonate ROOT_OWNER;
3. conflict with current V2 machine state;
4. stale or unverifiable assumptions;
5. duplicate/competing authority sources;
6. missing migration/rollback/completion gates;
7. fake or impossible background/autonomy claims;
8. unsafe root-control activation;
9. weak evidence/completion language;
10. setup instructions likely to cause the new ChatGPT Project to mis-operate.

Return ONLY JSON with exactly:
{
  "status": "PASS" | "PASS_WITH_CHANGES" | "FAIL",
  "ready_to_activate": boolean,
  "findings": [string],
  "blockers": [string],
  "required_changes": [string],
  "confidence": string
}

Do not make changes. Do not use external tools. Do not broaden scope.

EXACT REVIEW PACKET:
""" + packet

        response = OPENROUTER_CLIENT.chat.completions.create(
            model=review_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=3000,
            timeout=90,
            stream=False,
            response_format={"type": "json_object"},
            extra_body={"provider": {"allow_fallbacks": False, "require_parameters": True}},
        )
        if not response.choices:
            raise RuntimeError("reviewer_no_choices")
        provider_model = getattr(response, "model", None)
        if provider_model and provider_model != review_model:
            raise RuntimeError("reviewer_model_mismatch")
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("reviewer_output_not_object")
        status = parsed.get("status")
        if status not in {"PASS", "PASS_WITH_CHANGES", "FAIL"}:
            raise RuntimeError("reviewer_status_invalid")
        for key in ("findings", "blockers", "required_changes"):
            if not isinstance(parsed.get(key), list) or not all(isinstance(x, str) for x in parsed[key]):
                raise RuntimeError(f"reviewer_{key}_invalid")
        if not isinstance(parsed.get("ready_to_activate"), bool):
            raise RuntimeError("reviewer_ready_flag_invalid")
        confidence = parsed.get("confidence")
        if not isinstance(confidence, str):
            raise RuntimeError("reviewer_confidence_invalid")
        result.update(parsed)
        result["model"] = review_model
        result["packet_sha256"] = packet_sha
    except Exception as exc:
        result["blockers"] = [f"review_probe_error:{type(exc).__name__}:{str(exc)}"]

    print(
        "JAYTEC_GOD_PROJECT_REVIEW_RESULT="
        + json.dumps(result, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    _run_jaytec_read_bootstrap_probe()\n    _run_god_project_review_probe()
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        host_origin_protection=False,
    )
