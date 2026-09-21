from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from jaytec_read import (
    JAYTEC_READ_FETCH_ENGINES,
    JAYTEC_READ_PROMPT,
    build_openrouter_web_fetch_tool,
    enforce_read_report,
    is_jaytec_read_packet,
    source_url_from_packet,
)
from worker_json import WorkerJsonError, json_object_with_diagnostics


EXPECTED_DEEPSEEK_REVIEWER_MODEL = "deepseek/deepseek-v4-flash-0731:free"
DEEPSEEK_REVIEW_MAX_OUTPUT_TOKENS = 4096

DEEPSEEK_REVIEWER_CONTRACT = """ROLE: JAYTEC INDEPENDENT ADVERSARIAL SECURITY REVIEWER — DEEPSEEK V4 FLASH 0731.

You are subordinate to Jay/ChatGPT/JAYTEC and have REVIEW-ONLY authority.
You MUST NOT perform writes, deployments, merges, external contacts, credential operations, or tool actions.

Return ONLY one JSON object with exactly the JAYTEC specialist result fields:
- status
- model
- findings
- evidence
- confidence
- conclusion
- unresolved_items
- files_or_artifacts
- architecture_changes_required
- knowledge_writeback_proposal
- side_effects_attempted
- requested_operations

Rules:
- model MUST be exactly deepseek/deepseek-v4-flash-0731:free
- side_effects_attempted MUST be []
- requested_operations MUST be []
- findings/evidence/unresolved_items MUST be arrays of strings
- conclusion MUST follow the exact shape requested by the task
- attack assumptions, bypasses, replay/concurrency, privilege confusion, stale state,
  credential compromise, deployment isolation, rollback, failure recovery, and
  unsafe completion claims.
- Do not soften a blocker to make the system appear ready.
- Do not invent evidence.
- Never include credentials or secrets.
"""

DEEPSEEK_FORMAT_RETRY = """Your previous transport result was empty, malformed, truncated, or not a complete JSON object.
Re-answer the ORIGINAL REVIEW_PACKET_JSON from scratch.
Return one compact valid JSON object only.
Do not include markdown fences, commentary, chain-of-thought, or text outside the JSON object.
"""


def _reviewer_result_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "SUCCESS",
                    "PARTIAL_SUCCESS",
                    "NEEDS_VALIDATION",
                    "POLICY_BLOCKED",
                    "FAILED_CLOSED",
                    "INVALID_PACKET",
                    "TIMEOUT",
                    "RATE_LIMITED",
                ],
            },
            "model": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": ["string", "null"]},
            "conclusion": {},
            "unresolved_items": {"type": "array", "items": {"type": "string"}},
            "files_or_artifacts": {"type": "array"},
            "architecture_changes_required": {"type": "array"},
            "knowledge_writeback_proposal": {"type": "array"},
            "side_effects_attempted": {"type": "array"},
            "requested_operations": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "status",
            "model",
            "findings",
            "evidence",
            "confidence",
            "conclusion",
            "unresolved_items",
            "files_or_artifacts",
            "architecture_changes_required",
            "knowledge_writeback_proposal",
            "side_effects_attempted",
            "requested_operations",
        ],
    }


def _provider_model(response: Any) -> str | None:
    value = getattr(response, "model", None)
    return value if isinstance(value, str) and value.strip() else None


def _finish_reason(choice: Any) -> str | None:
    value = getattr(choice, "finish_reason", None)
    return value if isinstance(value, str) and value.strip() else None


def _provider_route_unavailable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 404:
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def _safe_diagnostics(
    *,
    content: str,
    finish_reason: str | None,
    provider_model: str | None,
    attempt: int,
    extracted_object: bool,
    provider_fallbacks: bool,
) -> dict[str, Any]:
    encoded = (content or "").encode("utf-8", errors="replace")
    return {
        "reviewer": "deepseek",
        "model": EXPECTED_DEEPSEEK_REVIEWER_MODEL,
        "provider_model": provider_model,
        "attempt": attempt,
        "content_bytes": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "finish_reason": finish_reason,
        "extracted_balanced_object": bool(extracted_object),
        "max_output_tokens": DEEPSEEK_REVIEW_MAX_OUTPUT_TOKENS,
        "model_fallbacks": False,
        "provider_fallbacks": bool(provider_fallbacks),
        "side_effect_capability": False,
    }


def build_deepseek_security_review_dispatch(
    *,
    openrouter_client: OpenAI,
    model: str,
    timeout_s: float,
    circuit: CircuitBreaker,
    max_output_tokens: int = DEEPSEEK_REVIEW_MAX_OUTPUT_TOKENS,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    if model != EXPECTED_DEEPSEEK_REVIEWER_MODEL:
        raise RuntimeError(
            "deepseek_reviewer_model_mismatch:"
            f"expected={EXPECTED_DEEPSEEK_REVIEWER_MODEL};got={model}"
        )
    if timeout_s <= 0:
        raise ValueError("deepseek_reviewer_timeout_must_be_positive")
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < 512
        or max_output_tokens > 8192
    ):
        raise ValueError("deepseek_reviewer_max_output_tokens_invalid")

    def _prompt(
        packet: Mapping[str, Any],
        *,
        retry_format: bool,
        fetch_engine: str | None = None,
    ) -> str:
        prefix = DEEPSEEK_REVIEWER_CONTRACT
        if is_jaytec_read_packet(packet):
            prefix += "\n" + JAYTEC_READ_PROMPT
            prefix += "\nSOURCE_URL: " + source_url_from_packet(packet)
            if fetch_engine:
                prefix += "\nFETCH_ENGINE: " + fetch_engine
        if retry_format:
            prefix += "\n" + DEEPSEEK_FORMAT_RETRY
        return (
            prefix
            + "\nREVIEW_PACKET_JSON:\n"
            + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        )

    def _single(
        packet: Mapping[str, Any],
        *,
        retry_format: bool,
        attempt: int,
        fetch_engine: str | None = None,
    ) -> Mapping[str, Any]:
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": _prompt(
                        packet,
                        retry_format=retry_format,
                        fetch_engine=fetch_engine,
                    ),
                }
            ],
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "timeout": timeout_s,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "jaytec_deepseek_security_review",
                    "strict": True,
                    "schema": _reviewer_result_schema(),
                },
            },
            "extra_body": {
                "provider": {
                    "allow_fallbacks": False,
                    "require_parameters": True,
                },
            },
        }

        if is_jaytec_read_packet(packet):
            read_source_url = source_url_from_packet(packet)
            engine = fetch_engine or JAYTEC_READ_FETCH_ENGINES[0]
            request_kwargs["tools"] = [
                build_openrouter_web_fetch_tool(read_source_url, engine=engine)
            ]
            request_kwargs["tool_choice"] = "required"

        response = openrouter_client.chat.completions.create(**request_kwargs)
        if not response.choices:
            raise WorkerJsonError("EMPTY_RESPONSE", "reviewer returned no choices")

        returned_model = _provider_model(response)
        if returned_model is None:
            raise RuntimeError(
                "deepseek_reviewer_provider_model_unobservable"
            )
        if returned_model != model:
            raise RuntimeError(
                "deepseek_reviewer_provider_model_mismatch:"
                f"expected={model};got={returned_model}"
            )

        choice = response.choices[0]
        finish_reason = _finish_reason(choice)
        content = choice.message.content or ""
        if finish_reason in {"length", "content_filter"}:
            encoded = content.encode("utf-8", errors="replace")
            raise WorkerJsonError(
                "TRUNCATED_OR_BLOCKED_RESPONSE",
                "finish_reason="
                + str(finish_reason)
                + ";bytes="
                + str(len(encoded))
                + ";sha256="
                + hashlib.sha256(encoded).hexdigest(),
            )

        result, diagnostics = json_object_with_diagnostics(content)
        result.setdefault("model", model)
        if result.get("model") != model:
            raise RuntimeError("deepseek_reviewer_result_model_mismatch")
        if result.get("side_effects_attempted") not in ([], None):
            raise RuntimeError("deepseek_reviewer_side_effect_violation")
        if result.get("requested_operations") not in ([], None):
            raise RuntimeError("deepseek_reviewer_requested_operations_nonempty")

        result["bridge_diagnostics"] = _safe_diagnostics(
            content=content,
            finish_reason=finish_reason,
            provider_model=returned_model,
            attempt=attempt,
            extracted_object=diagnostics.extracted_object,
            provider_fallbacks=False,
        )
        if is_jaytec_read_packet(packet):
            read_source_url = source_url_from_packet(packet)
            engine = fetch_engine or JAYTEC_READ_FETCH_ENGINES[0]
            result = enforce_read_report(result, read_source_url)
            read_diag = dict(result.get("bridge_diagnostics") or {})
            read_diag["web_retrieval"] = {
                "enabled": True,
                "engine": engine,
                "source_url_sha256": hashlib.sha256(read_source_url.encode("utf-8")).hexdigest(),
                "notion_fallback": False,
            }
            result["bridge_diagnostics"] = read_diag
        return result

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        max_retries = packet.get("max_retries", 1)
        if (
            not isinstance(max_retries, int)
            or isinstance(max_retries, bool)
            or max_retries < 0
            or max_retries > 1
        ):
            raise RuntimeError("deepseek_reviewer_retry_budget_invalid")

        def _single_with_format_retry(fetch_engine: str | None = None) -> Mapping[str, Any]:
            try:
                return _single(
                    packet,
                    retry_format=False,
                    attempt=1,
                    fetch_engine=fetch_engine,
                )
            except Exception as exc:
                # One transport-format retry is permitted, but JAYTEC never enables
                # OpenRouter provider fallback here. A provider-route failure is a
                # real dependency failure, not permission to silently switch routes.
                retryable = isinstance(exc, WorkerJsonError)
                if max_retries < 1 or not retryable:
                    raise
                return _single(
                    packet,
                    retry_format=True,
                    attempt=2,
                    fetch_engine=fetch_engine,
                )

        if not is_jaytec_read_packet(packet):
            return _single_with_format_retry()

        attempts: list[dict[str, Any]] = []
        last_result: Mapping[str, Any] | None = None
        for engine in JAYTEC_READ_FETCH_ENGINES:
            result = _single_with_format_retry(engine)
            last_result = result
            conclusion = result.get("conclusion")
            report = conclusion.get("READ_REPORT") if isinstance(conclusion, Mapping) else None
            verified = isinstance(report, Mapping) and report.get("VERIFIED") is True
            attempts.append(
                {
                    "engine": engine,
                    "status": result.get("status"),
                    "verified": verified,
                }
            )
            if result.get("status") == "SUCCESS" and verified:
                out = dict(result)
                diagnostics = dict(out.get("bridge_diagnostics") or {})
                diagnostics["web_retrieval_attempts"] = attempts
                out["bridge_diagnostics"] = diagnostics
                return out
            if result.get("status") in {"POLICY_BLOCKED", "INVALID_PACKET"}:
                break

        if last_result is None:
            raise RuntimeError("jaytec_read_no_fetch_attempts")
        out = dict(last_result)
        diagnostics = dict(out.get("bridge_diagnostics") or {})
        diagnostics["web_retrieval_attempts"] = attempts
        diagnostics["notion_fallback"] = False
        out["bridge_diagnostics"] = diagnostics
        return out

    return circuit.guard(_dispatch)
