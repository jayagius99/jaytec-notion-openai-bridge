"""STAGING ONLY: unified JAYTEC execute_task_packet MCP surface.

This entrypoint is intentionally independent from production server.py so the
staging service can boot and validate packets without production provider keys.
Specialist dispatch fails closed until the corresponding server-side credential
is configured. Do not promote until BRIDGE_CONTRACT_V1.md activation gates pass.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from openai import OpenAI

from circuit_breaker import CircuitBreaker
from orchestration import ExecutionRegistry, execute_task_packet_core, parse_packet_json

PORT = int(os.environ.get("PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.3-codex").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "google/gemini-3.1-pro-preview").strip()
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "90"))
CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("CIRCUIT_FAILURE_THRESHOLD", "3"))
CIRCUIT_RESET_SECONDS = int(os.environ.get("CIRCUIT_RESET_SECONDS", "60"))

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
REGISTRY = ExecutionRegistry()
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

CODEX_CONTRACT = """Return ONLY one JSON object. Preserve task_id and subtask_id.
Required keys: status, model, findings, evidence, confidence, conclusion,
unresolved_items, files_or_artifacts, architecture_changes_required,
knowledge_writeback_proposal, side_effects_attempted, requested_operations.
status must be one of SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION,
POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED.
model must be exactly gpt-5.3-codex. Never include credentials or secrets."""

GEMINI_RESEARCH_MODE_V1_1 = """JAYTEC_GEMINI_RESEARCH_MODE v1.1.0
ROLE: RESEARCH SPECIALIST. Treat each request as stateless.
Preserve TASK_ID and SUBTASK_ID exactly. Investigate the supplied objective,
separate verified facts/evidence from inference, report confidence and unresolved
questions, and hand findings back to Notion. Do not perform engineering writes.
Never expose credentials. Return ONLY one JSON object using the requested result
contract. model must be exactly google/gemini-3.1-pro-preview."""


def _json_object(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker returned invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("worker JSON root must be an object")
    return value


def _codex_dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    if OPENAI_CLIENT is None:
        raise RuntimeError("OPENAI_API_KEY is not configured on the staging bridge")
    prompt = (
        "ROLE: GPT-5.3 CODEX ENGINEERING\n" + CODEX_CONTRACT
        + "\nTASK_PACKET_JSON:\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )
    response = OPENAI_CLIENT.responses.create(
        model=CODEX_MODEL,
        input=prompt,
        reasoning={"effort": "high"},
    )
    result = _json_object(response.output_text or "")
    result.setdefault("model", CODEX_MODEL)
    return result


def _gemini_dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    if OPENROUTER_CLIENT is None:
        raise RuntimeError("OPENROUTER_API_KEY is not configured on the staging bridge")
    prompt = (
        GEMINI_RESEARCH_MODE_V1_1
        + "\nTASK_ID: " + str(packet.get("task_id", ""))
        + "\nSUBTASK_ID: " + str(packet.get("subtask_id", ""))
        + "\nTASK_PACKET_JSON:\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        + "\nRESULT_CONTRACT: status, model, findings, evidence, confidence, conclusion, "
          "unresolved_items, files_or_artifacts, architecture_changes_required, "
          "knowledge_writeback_proposal, side_effects_attempted, requested_operations."
    )
    response = OPENROUTER_CLIENT.chat.completions.create(
        model=GEMINI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        timeout=GEMINI_TIMEOUT_S,
        stream=False,
        extra_body={
            "provider": {
                "sort": "price",
                "allow_fallbacks": True,
            }
        },
    )
    if not response.choices:
        raise RuntimeError("Gemini returned no choices")
    result = _json_object(response.choices[0].message.content or "")
    result["model"] = getattr(response, "model", None) or result.get("model") or GEMINI_MODEL
    return result


CODEX_DISPATCH = CODEX_CIRCUIT.guard(_codex_dispatch)
GEMINI_DISPATCH = GEMINI_CIRCUIT.guard(_gemini_dispatch)


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
            "idempotency_store": "process_memory_staging_only",
            "production_ready": False,
        },
        sort_keys=True,
    )


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
    result = execute_task_packet_core(
        packet,
        {"codex": CODEX_DISPATCH, "gemini": GEMINI_DISPATCH},
        REGISTRY,
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
