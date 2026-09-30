"""Shared specialist adapter contracts + dispatch construction.

This module exists to prevent drift between staging_server.py and server.py.
It contains the exact staging-tested CODEX_CONTRACT and GEMINI_RESEARCH_MODE_V1_1
strings and the dispatch semantics used by both entrypoints.

Security / safety:
- Enforces exact model identity BEFORE provider calls.
- Prompts require strict JSON-only output and no side effects.
- Provider timeouts/rate limits/unavailability are normalized to JAYTEC runtime
  exception types so bounded retry/backoff is deterministic.
- Gemini malformed output is never guessed into a successful result.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker, CircuitIgnoredError
from orchestration import ProviderUnavailableError, RateLimitError
from jaytec_read import (
    JAYTEC_READ_FETCH_ENGINES,
    JAYTEC_READ_PROMPT,
    build_openrouter_web_fetch_tool,
    enforce_read_report,
    is_jaytec_read_packet,
    source_url_from_packet,
)
from worker_json import WorkerJsonError, json_object, json_object_with_diagnostics

EXPECTED_CODEX_MODEL = "nex-agi/nex-n2.5-mini:free"
EXPECTED_REVIEWER_MODEL = "qwen/qwen3.8-27b:free"
EXPECTED_GEMINI_MODEL = EXPECTED_REVIEWER_MODEL  # legacy TaskPacket wire role compatibility
EXPECTED_SOL_MODEL = "openai/gpt-5.6-sol"
SOL_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"

SPECIALIST_AUTHORITY_CONTRACT = """JAYTEC SPECIALIST AUTHORITY CONTRACT
- Jay is owner/root authority.
- ChatGPT/OpenAI Lead is the sole JAYTEC coordinator/controller for specialist work.
- You are a subordinate specialist worker/reviewer only.
- Work only on the exact task/questions ChatGPT sends you.
- Do not self-initiate JAYTEC work, broaden scope, create follow-on tasks, approve your own recommendations, or decide that a JAYTEC change should be applied.
- Do not mutate JAYTEC, production state, credentials, providers, authority rules, deployments, repositories, or durable system state.
- Return advice/code/review/evidence to ChatGPT. ChatGPT decides whether anything is applied.
- A specialist response never grants itself or another specialist execution authority.
"""


CODEX_CONTRACT = SPECIALIST_AUTHORITY_CONTRACT + """\nReturn ONLY one JSON object. Preserve task_id and subtask_id.

REQUIRED SHAPE (types are strict):
- status: string enum (SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION, POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED)
- model: string exactly nex-agi/nex-n2.5-mini:free
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

SOL_RESERVE_CONTRACT = SPECIALIST_AUTHORITY_CONTRACT + """\nJAYTEC_SOL_RESERVE_MODE v1.0.0
ROLE: PREMIUM HARD-DECISION / ENGINEERING RESERVE.
Activation is allowed only for an explicit owner request routed by ChatGPT.
This is a zero-out-of-pocket reserve. Never request purchases, top-ups, paid fallback,
alternate paid models/providers, or any side effect. If the free-credit route is
unavailable, return/propagate failure closed.

Return ONLY one valid JSON object (no markdown fences and no surrounding prose).
Preserve TASK_ID and SUBTASK_ID.
REQUIRED SHAPE:
- status: string enum (SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION, POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED)
- model: string exactly openai/gpt-5.6-sol
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
"""

GEMINI_RESEARCH_MODE_V1_1 = SPECIALIST_AUTHORITY_CONTRACT + """\nJAYTEC_INDEPENDENT_REVIEW_MODE v1.0.0
ROLE: INDEPENDENT RESEARCH / ARCHITECTURE / ADVERSARIAL REVIEW SPECIALIST. Treat each request as stateless.

Return ONLY one valid JSON object (no markdown fences and no surrounding prose).
Preserve TASK_ID and SUBTASK_ID. Keep strings properly JSON escaped. If the
answer is long, keep the required transport fields compact and put the detailed
analysis in conclusion rather than expanding many duplicated fields.

REQUIRED SHAPE (types are strict):
- status: string enum (SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION, POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED)
- model: string exactly qwen/qwen3.8-27b:free
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
Return one compact valid JSON object only, using exactly the required JAYTEC independent reviewer fields.
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


def _status_code(exc: Exception) -> int | None:
    for name in ("status_code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after(exc: Exception) -> Any:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
            if value is not None:
                return value
        except Exception:
            pass
    return getattr(exc, "retry_after", None)


def _normalize_provider_exception(exc: Exception, *, context: str) -> None:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    status = _status_code(exc)
    if (
        "timeout" in name
        or "timedout" in name
        or "timeout" in text
        or "timed out" in text
    ):
        raise TimeoutError(f"{context}_timeout") from exc
    if status == 429 or "ratelimit" in name or "rate limit" in text or "rate_limit" in text:
        raise RateLimitError(f"{context}_rate_limited", retry_after=_retry_after(exc)) from exc
    if (
        status is not None and status >= 500
    ) or any(token in name or token in text for token in ("serviceunavailable", "service unavailable", "connectionerror", "connection error", "temporarily unavailable")):
        raise ProviderUnavailableError(
            f"{context}_provider_unavailable", retry_after=_retry_after(exc)
        ) from exc
    raise exc


def _specialist_result_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "SUCCESS", "PARTIAL_SUCCESS", "NEEDS_VALIDATION",
                    "POLICY_BLOCKED", "FAILED_CLOSED", "INVALID_PACKET",
                    "TIMEOUT", "RATE_LIMITED"
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
            "status", "model", "findings", "evidence", "confidence", "conclusion",
            "unresolved_items", "files_or_artifacts", "architecture_changes_required",
            "knowledge_writeback_proposal", "side_effects_attempted",
            "requested_operations",
        ],
    }


def build_codex_dispatch(
    *,
    openai_client: OpenAI,
    codex_model: str,
    circuit: CircuitBreaker,
    codex_timeout_s: float = 45.0,
    provider_mode: str = "OPENROUTER_FREE_PRIMARY",
    allowed_workflow_prefixes: tuple[str, ...] = (
        "JAYTEC_V2_",
        "JAYTEC_ENGINEERING_",
        "JAYTEC_WATCH_RECOVERY_",
    ),
    max_output_tokens: int = 2000,
    max_packet_retries: int = 1,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Build the primary zero-cost engineering specialist.

    The historical codex wire key is retained for TaskPacket compatibility,
    but the specialist role is provider-neutral and resolves to the exact
    OpenRouter free Nemotron model. The passed client must be an OpenRouter-
    configured OpenAI-compatible client.
    """
    require_exact_model(codex_model, EXPECTED_CODEX_MODEL, context="engineering")
    if codex_timeout_s <= 0:
        raise ValueError("codex_timeout_s must be positive")
    if provider_mode != "OPENROUTER_FREE_PRIMARY":
        raise ValueError("unsupported engineering provider mode")
    if type(max_output_tokens) is not int or max_output_tokens < 256 or max_output_tokens > 4000:
        raise ValueError("invalid engineering max_output_tokens")
    if type(max_packet_retries) is not int or max_packet_retries < 0 or max_packet_retries > 1:
        raise ValueError("invalid engineering max_packet_retries")

    def _prompt(packet: Mapping[str, Any], *, retry_format: bool = False) -> str:
        prefix = (
            "ROLE: JAYTEC PRIMARY ENGINEERING SPECIALIST (NEX N2.5 MINI FREE)\n"
            + CODEX_CONTRACT
        )
        if retry_format:
            prefix += (
                "\nYour previous response was not a complete parseable JSON object. "
                "Re-answer the original task from scratch and return one compact JSON object only."
            )
        return prefix + "\nTASK_PACKET_JSON:\n" + json.dumps(
            packet, ensure_ascii=False, sort_keys=True
        )

    def _single_call(packet: Mapping[str, Any], *, retry_format: bool) -> Mapping[str, Any]:
        tool_name = "submit_engineering_result"
        request_kwargs: dict[str, Any] = {
            "model": codex_model,
            "messages": [{"role": "user", "content": _prompt(packet, retry_format=retry_format)}],
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "timeout": codex_timeout_s,
            "stream": False,
            "tools": [{
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": "Return the bounded JAYTEC engineering result to ChatGPT. This tool has no side effects.",
                    "parameters": _specialist_result_schema(),
                },
            }],
            "tool_choice": {
                "type": "function",
                "function": {"name": tool_name},
            },
            "extra_body": {
                "provider": {
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }
            },
        }
        try:
            response = openai_client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            _normalize_provider_exception(exc, context="engineering_provider")
        if not response.choices:
            raise RuntimeError("engineering_no_choices")

        returned_provider_model = _provider_model(response)
        if returned_provider_model is not None:
            require_exact_model(
                returned_provider_model,
                codex_model,
                context="engineering_provider_response",
            )
        choice = response.choices[0]
        message = choice.message
        tool_calls = getattr(message, "tool_calls", None) or []
        if len(tool_calls) != 1:
            raise WorkerJsonError(
                "ENGINEERING_TOOL_CALL_REQUIRED",
                f"expected_one_tool_call;got={len(tool_calls)}",
            )
        call = tool_calls[0]
        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        if name != tool_name:
            raise WorkerJsonError(
                "ENGINEERING_TOOL_NAME_MISMATCH",
                f"expected={tool_name};got={name}",
            )
        arguments = getattr(function, "arguments", None)
        if not isinstance(arguments, str):
            raise WorkerJsonError("ENGINEERING_TOOL_ARGS_MISSING", "arguments_not_string")
        try:
            result = json.loads(arguments)
        except Exception as exc:
            raise WorkerJsonError(
                "ENGINEERING_TOOL_ARGS_INVALID_JSON",
                hashlib.sha256(arguments.encode("utf-8", errors="replace")).hexdigest(),
            ) from exc
        if not isinstance(result, dict):
            raise WorkerJsonError("ENGINEERING_TOOL_ARGS_NOT_OBJECT", type(result).__name__)
        result.setdefault("model", codex_model)
        result["bridge_diagnostics"] = {
            "finish_reason": _finish_reason(choice),
            "provider_model": returned_provider_model,
            "structured_transport": "forced_tool_call",
            "tool_name": tool_name,
            "tool_args_sha256": hashlib.sha256(
                arguments.encode("utf-8", errors="replace")
            ).hexdigest(),
        }
        return result

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow_id = str(packet.get("workflow_id", ""))
        if not any(workflow_id.startswith(prefix) for prefix in allowed_workflow_prefixes):
            raise CircuitIgnoredError("engineering_workflow_not_authorized")
        authority = packet.get("required_context") or {}
        if not isinstance(authority, Mapping):
            raise CircuitIgnoredError("engineering_authority_context_required")
        if authority.get("authority_controller") != "CHATGPT_OPENAI_LEAD":
            raise CircuitIgnoredError("engineering_chatgpt_authority_required")
        if authority.get("specialist_authority") != "SUBORDINATE":
            raise CircuitIgnoredError("engineering_specialist_must_be_subordinate")
        requested_retries = packet.get("max_retries", 0)
        if type(requested_retries) is not int or requested_retries < 0:
            raise CircuitIgnoredError("engineering_invalid_retry_budget")
        if requested_retries > max_packet_retries:
            raise CircuitIgnoredError("engineering_retry_budget_exceeded")

        try:
            return _single_call(packet, retry_format=False)
        except WorkerJsonError:
            if requested_retries < 1:
                raise
            return _single_call(packet, retry_format=True)

    return circuit.guard(_dispatch)


def fetch_vercel_gateway_credit_balance(
    *,
    api_key: str,
    base_url: str = SOL_GATEWAY_BASE_URL,
    timeout_s: float = 10.0,
) -> Decimal:
    """Read the current AI Gateway credit balance without making an inference call."""
    if not api_key:
        raise RuntimeError("sol_gateway_api_key_missing")
    url = base_url.rstrip("/") + "/credits"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        _normalize_provider_exception(exc, context="sol_credit_preflight")
        raise RuntimeError("sol_credit_preflight_failed") from exc
    try:
        balance = Decimal(str(payload["balance"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("sol_credit_balance_invalid") from exc
    if balance < 0:
        raise RuntimeError("sol_credit_balance_negative")
    return balance


def _sol_provider_model_matches(value: str | None) -> bool:
    # Vercel may preserve provider/model or return the upstream model id.
    return value in {EXPECTED_SOL_MODEL, "gpt-5.6-sol"}


def build_sol_reserve_dispatch(
    *,
    gateway_client: OpenAI,
    gateway_api_key: str,
    circuit: CircuitBreaker,
    credit_balance_fn: Callable[[], Decimal],
    reserve_enabled: bool,
    zero_spend_attested: bool,
    sol_timeout_s: float = 60.0,
    max_output_tokens: int = 1800,
    max_input_bytes: int = 64_000,
    min_credit_usd: Decimal = Decimal("0.25"),
    reasoning_effort: str = "medium",
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Build the owner-explicit GPT-5.6 Sol reserve through Vercel AI Gateway.

    This adapter is deliberately separate from the free Nemotron engineering lane.
    It fails closed unless the owner has explicitly enabled the reserve and attested
    that the connected Gateway account is free-credit-only / zero-out-of-pocket.
    """
    if not gateway_api_key:
        raise RuntimeError("sol_gateway_api_key_missing")
    if not reserve_enabled:
        raise RuntimeError("sol_reserve_disabled")
    if not zero_spend_attested:
        raise RuntimeError("sol_zero_spend_attestation_required")
    if sol_timeout_s <= 0:
        raise ValueError("sol_timeout_s must be positive")
    if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 2400:
        raise ValueError("invalid_sol_max_output_tokens")
    if type(max_input_bytes) is not int or not 4096 <= max_input_bytes <= 96_000:
        raise ValueError("invalid_sol_max_input_bytes")
    if reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError("invalid_sol_reasoning_effort")
    if min_credit_usd <= 0:
        raise ValueError("invalid_sol_min_credit_usd")

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow_id = str(packet.get("workflow_id", ""))
        if not workflow_id.startswith("JAYTEC_OWNER_SOL_"):
            raise RuntimeError("sol_owner_workflow_required")
        authority = packet.get("required_context") or {}
        if not isinstance(authority, Mapping):
            raise RuntimeError("sol_authority_context_required")
        if authority.get("authority_controller") != "CHATGPT_OPENAI_LEAD":
            raise RuntimeError("sol_chatgpt_authority_required")
        if authority.get("specialist_authority") != "SUBORDINATE":
            raise RuntimeError("sol_specialist_must_be_subordinate")
        if authority.get("owner_explicit_sol_request") is not True:
            raise RuntimeError("sol_owner_explicit_request_required")
        if packet.get("max_retries", 0) != 0:
            raise RuntimeError("sol_retries_forbidden")
        if packet.get("side_effect_policy") != "none":
            raise RuntimeError("sol_side_effects_forbidden")

        packet_json = json.dumps(packet, ensure_ascii=False, sort_keys=True)
        if len(packet_json.encode("utf-8")) > max_input_bytes:
            raise RuntimeError("sol_input_cap_exceeded")

        balance_before = credit_balance_fn()
        if balance_before < min_credit_usd:
            raise RuntimeError("sol_free_credit_reserve_too_low")

        prompt = (
            "ROLE: JAYTEC GPT-5.6 SOL RESERVE SPECIALIST\n"
            + SOL_RESERVE_CONTRACT
            + "\nTASK_PACKET_JSON:\n"
            + packet_json
        )
        request_kwargs: dict[str, Any] = {
            "model": EXPECTED_SOL_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0,
            "max_completion_tokens": max_output_tokens,
            "reasoning_effort": reasoning_effort,
            "timeout": sol_timeout_s,
            "response_format": {"type": "json_object"},
            "extra_body": {
                "providerOptions": {
                    "gateway": {
                        "only": ["openai"],
                        "disallowPromptTraining": True,
                    }
                }
            },
        }
        try:
            response = gateway_client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            _normalize_provider_exception(exc, context="sol_gateway")
        if not response.choices:
            raise RuntimeError("sol_no_choices")

        returned_provider_model = _provider_model(response)
        if returned_provider_model is not None and not _sol_provider_model_matches(returned_provider_model):
            raise RuntimeError(
                f"sol_provider_response_model_mismatch:returned={returned_provider_model}"
            )

        choice = response.choices[0]
        finish_reason = _finish_reason(choice)
        content = choice.message.content or ""
        if finish_reason in {"length", "content_filter"}:
            raise WorkerJsonError("SOL_TRUNCATED_OR_BLOCKED_RESPONSE", str(finish_reason))
        result, diagnostics = json_object_with_diagnostics(content)
        result["model"] = EXPECTED_SOL_MODEL

        balance_after = credit_balance_fn()
        if balance_after < 0:
            raise RuntimeError("sol_credit_balance_negative_after_call")
        result["bridge_diagnostics"] = {
            **_safe_transport_diagnostics(
                content=content,
                finish_reason=finish_reason,
                provider_model=returned_provider_model,
                extracted_object=diagnostics.extracted_object,
            ),
            "gateway": "vercel_ai_gateway",
            "provider_only": ["openai"],
            "model_lock": EXPECTED_SOL_MODEL,
            "fallback_models": [],
            "zero_spend_attested": True,
            "credit_balance_before_usd": str(balance_before),
            "credit_balance_after_usd": str(balance_after),
            "credit_used_usd": str(max(Decimal("0"), balance_before - balance_after)),
            "max_output_tokens": max_output_tokens,
            "max_input_bytes": max_input_bytes,
            "reasoning_effort": reasoning_effort,
        }
        return result

    return circuit.guard(_dispatch)


def build_gemini_dispatch(
    *,
    openrouter_client: OpenAI,
    gemini_model: str,
    gemini_timeout_s: float,
    circuit: CircuitBreaker,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    require_exact_model(gemini_model, EXPECTED_GEMINI_MODEL, context="reviewer")
    if gemini_timeout_s <= 0:
        raise ValueError("gemini_timeout_s must be positive")

    def _prompt(
        packet: Mapping[str, Any],
        *,
        retry_format: bool = False,
        fetch_engine: str | None = None,
    ) -> str:
        prefix = GEMINI_RESEARCH_MODE_V1_1
        if is_jaytec_read_packet(packet):
            prefix += "\n" + JAYTEC_READ_PROMPT
            prefix += "\nSOURCE_URL: " + source_url_from_packet(packet)
            if fetch_engine:
                prefix += "\nFETCH_ENGINE: " + fetch_engine
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

    def _single_call(
        packet: Mapping[str, Any],
        *,
        retry_format: bool,
        fetch_engine: str | None = None,
    ) -> Mapping[str, Any]:
        read_source_url = source_url_from_packet(packet) if is_jaytec_read_packet(packet) else None
        request_kwargs: dict[str, Any] = {
            "model": gemini_model,
            "messages": [{
                "role": "user",
                "content": _prompt(
                    packet,
                    retry_format=retry_format,
                    fetch_engine=fetch_engine,
                ),
            }],
            "temperature": 0,
            "timeout": gemini_timeout_s,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "jaytec_reviewer_result",
                    "strict": True,
                    "schema": _specialist_result_schema(),
                },
            },
            "extra_body": {
                "provider": {
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }
            },
        }
        if read_source_url is not None:
            engine = fetch_engine or JAYTEC_READ_FETCH_ENGINES[0]
            request_kwargs["tools"] = [
                build_openrouter_web_fetch_tool(read_source_url, engine=engine)
            ]
            request_kwargs["tool_choice"] = "required"

        try:
            response = openrouter_client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            _normalize_provider_exception(exc, context="reviewer_provider")
        if not response.choices:
            raise RuntimeError("reviewer_no_choices")

        returned_provider_model = _provider_model(response)
        if returned_provider_model is not None:
            require_exact_model(
                returned_provider_model,
                gemini_model,
                context="reviewer_provider_response",
            )

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
        transport_diagnostics = _safe_transport_diagnostics(
            content=content,
            finish_reason=finish_reason,
            provider_model=returned_provider_model,
            extracted_object=diagnostics.extracted_object,
        )
        if read_source_url is not None:
            engine = fetch_engine or JAYTEC_READ_FETCH_ENGINES[0]
            transport_diagnostics["web_retrieval"] = {
                "enabled": True,
                "engine": engine,
                "source_url_sha256": hashlib.sha256(
                    read_source_url.encode("utf-8")
                ).hexdigest(),
                "notion_fallback": False,
            }
            result = enforce_read_report(result, read_source_url)
        result["bridge_diagnostics"] = transport_diagnostics
        return result

    def _single_with_format_retry(
        packet: Mapping[str, Any],
        *,
        fetch_engine: str | None = None,
    ) -> Mapping[str, Any]:
        try:
            return _single_call(
                packet,
                retry_format=False,
                fetch_engine=fetch_engine,
            )
        except WorkerJsonError:
            retries = packet.get("max_retries", 0)
            if not isinstance(retries, int) or isinstance(retries, bool) or retries < 1:
                raise
            return _single_call(
                packet,
                retry_format=True,
                fetch_engine=fetch_engine,
            )

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        operations = packet.get("allowed_operations") or []
        reviewer_packet: Mapping[str, Any] = packet
        disallowed_reviewer_ops = {
            "code_staging", "engineering_write", "production_write"
        }
        if isinstance(operations, list) and any(
            op in disallowed_reviewer_ops
            for op in operations
            if isinstance(op, str)
        ):
            workflow_id = str(packet.get("workflow_id") or "")
            if workflow_id.startswith("JAYTEC_WATCH_RECOVERY_"):
                scoped = dict(packet)
                scoped["allowed_operations"] = [
                    op for op in operations
                    if isinstance(op, str)
                    and op not in disallowed_reviewer_ops
                ]
                reviewer_packet = scoped
            else:
                raise CircuitIgnoredError("reviewer_role_task_not_authorized")
        if not is_jaytec_read_packet(reviewer_packet):
            return _single_with_format_retry(reviewer_packet)

        attempts: list[dict[str, Any]] = []
        last_result: Mapping[str, Any] | None = None
        for engine in JAYTEC_READ_FETCH_ENGINES:
            result = _single_with_format_retry(packet, fetch_engine=engine)
            last_result = result
            attempts.append(
                {
                    "engine": engine,
                    "status": result.get("status"),
                    "verified": (
                        isinstance(result.get("conclusion"), Mapping)
                        and isinstance(result["conclusion"].get("READ_REPORT"), Mapping)
                        and result["conclusion"]["READ_REPORT"].get("VERIFIED") is True
                    ),
                }
            )
            if result.get("status") == "SUCCESS" and attempts[-1]["verified"] is True:
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