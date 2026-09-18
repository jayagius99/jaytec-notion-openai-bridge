"""Shared specialist adapter contracts + dispatch construction.

This module exists to prevent drift between staging_server.py and server.py.
It contains the exact staging-tested CODEX_CONTRACT and GEMINI_RESEARCH_MODE_V1_1
strings and the dispatch semantics used by both entrypoints.

Security / safety:
- Enforces exact model identity BEFORE provider calls.
- Prompts require strict JSON-only output and no side effects.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from openai import OpenAI, RateLimitError as OpenAIRateLimitError

from orchestration import RateLimitError as OrchestrationRateLimitError

from circuit_breaker import CircuitBreaker
from worker_json import json_object

EXPECTED_CODEX_MODEL = "gpt-5.3-codex"
EXPECTED_GEMINI_MODEL = "google/gemini-3.1-pro-preview"

CODEX_CONTRACT = """Return ONLY one JSON object. Preserve task_id and subtask_id.

REQUIRED SHAPE (types are strict):
- status: string enum (SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION, POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED)
- model: string exactly gpt-5.3-codex
- findings: JSON array of strings (NOT an object)
- evidence: JSON array of strings (NOT an object)
- confidence: string|null
- conclusion: any JSON (object/string/etc) or null
- unresolved_items: JSON array of strings
- files_or_artifacts: JSON array
- architecture_changes_required: JSON array
- knowledge_writeback_proposal: JSON array
- side_effects_attempted: JSON array (MUST be [])
- requested_operations: JSON array of strings (subset of packet.allowed_operations; use [])

Never include markdown fences or surrounding prose. Never include credentials or secrets."""

GEMINI_RESEARCH_MODE_V1_1 = """JAYTEC_GEMINI_RESEARCH_MODE v1.1.0
ROLE: RESEARCH SPECIALIST. Treat each request as stateless.

Return ONLY one JSON object (no markdown fences). Preserve TASK_ID and SUBTASK_ID.

REQUIRED SHAPE (types are strict):
- status: string enum (SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION, POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED)
- model: string exactly google/gemini-3.1-pro-preview
- findings: JSON array of strings
- evidence: JSON array of strings
- confidence: string|null
- conclusion: any JSON or null
- unresolved_items: JSON array of strings
- files_or_artifacts: JSON array
- architecture_changes_required: JSON array
- knowledge_writeback_proposal: JSON array
- side_effects_attempted: JSON array (MUST be [])
- requested_operations: JSON array of strings (subset of packet.allowed_operations; use [])

Never expose credentials. Do not perform engineering writes."""


def require_exact_model(name: str, expected: str, *, context: str) -> None:
    if name != expected:
        raise RuntimeError(f"{context}: model mismatch (expected {expected}, got {name})")


def _safe_openai_rate_limit_details(exc: Exception) -> dict[str, Any]:
    """Extract only non-secret diagnostics needed to classify OpenAI 429s."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    body = getattr(exc, "body", None)
    error = body.get("error", {}) if isinstance(body, Mapping) else {}
    if not isinstance(error, Mapping):
        error = {}

    def _header(name: str) -> Any:
        try:
            return headers.get(name)
        except Exception:
            return None

    details = {
        "status_code": getattr(exc, "status_code", None),
        "error_type": error.get("type"),
        "error_code": error.get("code"),
        "error_param": error.get("param"),
        "request_id": getattr(exc, "request_id", None) or _header("x-request-id"),
        "retry_after": _header("retry-after"),
        "x_ratelimit_limit_requests": _header("x-ratelimit-limit-requests"),
        "x_ratelimit_remaining_requests": _header("x-ratelimit-remaining-requests"),
        "x_ratelimit_reset_requests": _header("x-ratelimit-reset-requests"),
        "x_ratelimit_limit_tokens": _header("x-ratelimit-limit-tokens"),
        "x_ratelimit_remaining_tokens": _header("x-ratelimit-remaining-tokens"),
        "x_ratelimit_reset_tokens": _header("x-ratelimit-reset-tokens"),
    }
    return {k: v for k, v in details.items() if v is not None}


def build_codex_dispatch(
    *,
    openai_client: OpenAI,
    codex_model: str,
    circuit: CircuitBreaker,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    # Fail closed BEFORE any upstream call.
    require_exact_model(codex_model, EXPECTED_CODEX_MODEL, context="codex")

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        prompt = (
            "ROLE: GPT-5.3 CODEX ENGINEERING\n"
            + CODEX_CONTRACT
            + "\nTASK_PACKET_JSON:\n"
            + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        )
        try:
            response = openai_client.responses.create(
                model=codex_model,
                input=prompt,
                reasoning={"effort": "high"},
            )
        except OpenAIRateLimitError as exc:
            details = _safe_openai_rate_limit_details(exc)
            raise OrchestrationRateLimitError(
                "OpenAI rate limited",
                retry_after=details.get("retry_after"),
                details=details,
            ) from exc
        result = json_object(response.output_text or "")
        result.setdefault("model", codex_model)
        return result

    return circuit.guard(_dispatch)


def build_gemini_dispatch(
    *,
    openrouter_client: OpenAI,
    gemini_model: str,
    gemini_timeout_s: float,
    circuit: CircuitBreaker,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    # Fail closed BEFORE any upstream call.
    require_exact_model(gemini_model, EXPECTED_GEMINI_MODEL, context="gemini")

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        prompt = (
            GEMINI_RESEARCH_MODE_V1_1
            + "\nTASK_ID: "
            + str(packet.get("task_id", ""))
            + "\nSUBTASK_ID: "
            + str(packet.get("subtask_id", ""))
            + "\nTASK_PACKET_JSON:\n"
            + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        )
        response = openrouter_client.chat.completions.create(
            model=gemini_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            timeout=gemini_timeout_s,
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
        result = json_object(response.choices[0].message.content or "")
        result.setdefault("model", gemini_model)
        return result

    return circuit.guard(_dispatch)
