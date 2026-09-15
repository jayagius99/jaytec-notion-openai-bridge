"""STAGING ONLY: unified JAYTEC execute_task_packet MCP surface.

Do not use as production entrypoint until the activation gates in
BRIDGE_CONTRACT_V1.md have passed.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping

from openai import OpenAI

from orchestration import ExecutionRegistry, execute_task_packet_core, parse_packet_json
from server import CODEX_MODEL, PORT, _call_openai, mcp

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "google/gemini-3.1-pro-preview").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "90"))

# Staging process-local registry. Production requires a durable shared store.
REGISTRY = ExecutionRegistry()
OPENROUTER_CLIENT = (
    OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
    if OPENROUTER_API_KEY
    else None
)

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
    if not CODEX_MODEL:
        raise RuntimeError("CODEX_MODEL is not configured")
    prompt = (
        "ROLE: GPT-5.3 CODEX ENGINEERING\n"
        + CODEX_CONTRACT
        + "\nTASK_PACKET_JSON:\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )
    result = _json_object(_call_openai(CODEX_MODEL, prompt))
    result.setdefault("model", CODEX_MODEL)
    return result


def _gemini_dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    if OPENROUTER_CLIENT is None:
        raise RuntimeError("OPENROUTER_API_KEY is not configured on the staging bridge")
    prompt = (
        GEMINI_RESEARCH_MODE_V1_1
        + "\nTASK_ID: " + str(packet.get("task_id", ""))
        + "\nSUBTASK_ID: " + str(packet.get("subtask_id", ""))
        + "\nTASK_PACKET_JSON:\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        + "\nRESULT_CONTRACT: status, model, findings, evidence, confidence, conclusion, "
          "unresolved_items, files_or_artifacts, architecture_changes_required, "
          "knowledge_writeback_proposal, side_effects_attempted, requested_operations."
    )
    response = OPENROUTER_CLIENT.chat.completions.create(
        model=GEMINI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        timeout=GEMINI_TIMEOUT_S,
    )
    if not response.choices:
        raise RuntimeError("Gemini returned no choices")
    content = response.choices[0].message.content or ""
    result = _json_object(content)
    returned_model = getattr(response, "model", None)
    result["model"] = returned_model or result.get("model") or GEMINI_MODEL
    return result


@mcp.tool
def orchestration_status() -> str:
    return json.dumps(
        {
            "status": "STAGING",
            "operation": "execute_task_packet",
            "codex_model": CODEX_MODEL or None,
            "gemini_model": GEMINI_MODEL,
            "gemini_adapter_configured": bool(OPENROUTER_API_KEY),
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
        {"codex": _codex_dispatch, "gemini": _gemini_dispatch},
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
