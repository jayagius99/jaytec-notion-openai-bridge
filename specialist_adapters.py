"""Shared specialist adapter contracts + dispatch construction.

This module exists to prevent drift between staging_server.py and server.py.
It contains the exact staging-tested CODEX_CONTRACT and GEMINI_RESEARCH_MODE_V1_1
strings and the dispatch semantics used by both entrypoints.

Security / safety:
- Enforces exact model identity BEFORE provider calls.
- Prompts require strict JSON-only output and no side effects.
- Gemini transport failures fail closed; malformed output is never guessed into
  a successful result.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from worker_json import WorkerJsonError, json_object, json_object_with_diagnostics

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

Return ONLY one valid JSON object (no markdown fences and no surrounding prose).
Preserve TASK_ID and SUBTASK_ID. Keep strings properly JSON escaped. If the
answer is long, keep the required transport fields compact and put the detailed
analysis in conclusion rather than expanding many duplicated fields.

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

GEMINI_FORMAT_RETRY = """Your previous transport attempt did not produce a complete parseable JSON object.
Re-answer the ORIGINAL TASK_PACKET_JSON from scratch. Do not quote or repair the prior response.
Return one compact valid JSON object only, using exactly the required JAYTEC Gemini research fields.
Never include markdown fences, comments, trailing prose, NaN/Infinity, or unescaped newlines inside JSON strings."""


def require_exact_model(name: str, expected: str, *, context: str) -> None:
    if name != expected:
        raise RuntimeError(f"{context}: model mismatch (expected {expected}, got {name})")


def _provider_model(response: Any) -> str | None:
    value = getattr(response, "model", None)
    return value if isinstance(value, str) and value.strip() else None


def _finish_reason(choice: Any) -> str | None:
    value = getattr(choice, "finish_reason", None)
    return value if isinstance(value, str) and value.strip() else None


def _safe_transport_diagnostics(*, content: str, finish_reason: str | None, provider_model: str | None, extracted_object: bool) -> dict[str, Any]:
    encoded = (content or "").encode("utf-8", errors="replace")
    return {
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_bytes": len(encoded),
        "finish_reason": finish_reason,
        "provider_model": provider_model,
        "extracted_balanced_object": bool(extracted_object),
    }


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
        response = openai_client.responses.create(
            model=codex_model,
            input=prompt,
            reasoning={"effort": "high"},
        )
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

    def _prompt(packet: Mapping[str, Any], *, retry_format: bool = False) -> str:
        prefix = GEMINI_RESEARCH_MODE_V1_1
        if retry_format:
            prefix += "\n" + GEMINI_FORMAT_RETRY
        return (
            prefix
            + "\nTASK_ID: "
            + str(packet.get("task_id", ""))
            + "\nSUBTASK_ID: "
            + str(packet.get("subtask_id", ""))
            + "\nTASK_PACKET_JSON:\n"
            + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        )

    def _single_call(packet: Mapping[str, Any], *, retry_format: bool) -> Mapping[str, Any]:
        response = openrouter_client.chat.completions.create(
            model=gemini_model,
            messages=[{"role": "user", "content": _prompt(packet, retry_format=retry_format)}],
            temperature=0,
            timeout=gemini_timeout_s,
            stream=False,
            response_format={"type": "json_object"},
            extra_body={
                "provider": {
                    "sort": "price",
                    # Provider failover is allowed only within the exact locked
                    # model. The caller cannot provide a fallback model.
                    "allow_fallbacks": True,
                }
            },
        )
        if not response.choices:
            raise RuntimeError("gemini_no_choices")

        returned_provider_model = _provider_model(response)
        if returned_provider_model is not None:
            require_exact_model(returned_provider_model, gemini_model, context="gemini_provider_response")

        choice = response.choices[0]
        finish_reason = _finish_reason(choice)
        content = choice.message.content or ""
        if finish_reason in {"length", "content_filter"}:
            digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
            raise WorkerJsonError(
                "TRUNCATED_OR_BLOCKED_RESPONSE",
                f"finish_reason={finish_reason};bytes={len(content.encode('utf-8', errors='replace'))};sha256={digest}",
            )

        result, diagnostics = json_object_with_diagnostics(content)
        result.setdefault("model", gemini_model)
        result["bridge_diagnostics"] = _safe_transport_diagnostics(
            content=content,
            finish_reason=finish_reason,
            provider_model=returned_provider_model,
            extracted_object=diagnostics.extracted_object,
        )
        return result

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return _single_call(packet, retry_format=False)
        except WorkerJsonError:
            # One bounded re-answer is allowed only when the packet itself
            # authorises retries. We never feed malformed output back to the
            # model and never synthesize missing JSON fields locally.
            retries = packet.get("max_retries", 0)
            if not isinstance(retries, int) or isinstance(retries, bool) or retries < 1:
                raise
            return _single_call(packet, retry_format=True)

    return circuit.guard(_dispatch)
