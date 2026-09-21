"""Shared specialist adapter contracts + dispatch construction.

This module exists to prevent drift between staging_server.py and server.py.
It contains the exact staging-tested CODEX_CONTRACT and GEMINI_RESEARCH_MODE_V1_1
strings and the dispatch semantics used by both entrypoints.

Security / safety:
- Enforces exact model identity BEFORE provider calls.
- Prompts require strict JSON-only output and no side effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping

from openai import OpenAI, RateLimitError as OpenAIRateLimitError

from orchestration import (
    CreditTopupRequiredError,
    ProviderUnavailableError,
    RateLimitError as OrchestrationRateLimitError,
)

from circuit_breaker import CircuitBreaker
from jaytec_read import (
    JAYTEC_READ_FETCH_ENGINES,
    JAYTEC_READ_PROMPT,
    build_openrouter_web_fetch_tool,
    enforce_read_report,
    is_jaytec_read_packet,
    source_url_from_packet,
)
from participant_contracts import render_actor_contract
from relationship_policy import Actor
from worker_json import WorkerJsonError, json_object, json_object_with_diagnostics

EXPECTED_ENGINEERING_MODEL = "gpt-5.6-sol"
SOL_KNOWLEDGE_SCOPE = "JAYTEC_SANITIZED_CORE_V3"
SOL_CONTEXT_PATH = Path(__file__).with_name("SOL_PRIMARY_SANITIZED_CONTEXT_V3.md")
SOL_PROVENANCE_MARKERS = (
    "genesis_event_0001", "pre-genesis", "pre genesis", "/jaytec/uren/pre-genesis",
    "uren_identity_genesis", "uren identity genesis", "god mode", "owner manual source index",
    "how uren was born", "how uren will be born", "uren birth", "uren origin",
    "uren construction", "uren activation sequence", "uren genesis",
)
SOL_PROVENANCE_COMPACT_MARKERS = (
    "genesisevent0001", "pregenesis", "jaytecurenpregenesis", "urenidentitygenesis",
    "godmode", "ownermanualsourceindex", "howurenwasborn", "howurenwillbeborn",
    "urenbirth", "urenorigin", "urenconstruction", "urenactivationsequence", "urengenesis",
)
# TaskPacket v1 keeps the historical "codex" specialist key for wire compatibility.
# Semantically it now means the JAYTEC engineering specialist role.
EXPECTED_CODEX_MODEL = EXPECTED_ENGINEERING_MODEL
EXPECTED_GEMINI_MODEL = "google/gemini-3.1-pro-preview"
GEMINI_MAX_OUTPUT_TOKENS = 4096
ENGINEERING_PROVIDER_ACTIVE = "ACTIVE"
ENGINEERING_PROVIDER_LOCKED_RESERVE = "LOCKED_RESERVE"
OPENROUTER_PROVIDER_ACTIVE = "ACTIVE"
OPENROUTER_PROVIDER_LOCKED_RESERVE = "LOCKED_RESERVE"

ENGINEERING_CONTRACT = """Return ONLY one JSON object. Preserve task_id and subtask_id.

REQUIRED SHAPE (types are strict):
- status: string enum (SUCCESS, PARTIAL_SUCCESS, NEEDS_VALIDATION, POLICY_BLOCKED, FAILED_CLOSED, INVALID_PACKET, TIMEOUT, RATE_LIMITED)
- model: string exactly gpt-5.6-sol
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

# TaskPacket v1/source compatibility alias. New code uses ENGINEERING_CONTRACT.
CODEX_CONTRACT = ENGINEERING_CONTRACT

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

GEMINI_FORMAT_RETRY = """Your previous transport attempt did not produce a complete parseable JSON object.
Re-answer the ORIGINAL TASK_PACKET_JSON from scratch. Do not quote or repair the prior response.
Return one compact valid JSON object only, using exactly the required JAYTEC Gemini research fields.
Never include markdown fences, comments, trailing prose, NaN/Infinity, or unescaped newlines inside JSON strings."""



def _sol_context_text() -> str:
    try:
        text = SOL_CONTEXT_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        raise RuntimeError("sol_sanitized_context_unavailable") from exc
    if not text.strip():
        raise RuntimeError("sol_sanitized_context_empty")
    lowered = unicodedata.normalize("NFKC", text).lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    if any(marker in lowered for marker in SOL_PROVENANCE_MARKERS):
        raise RuntimeError("sol_sanitized_context_provenance_violation")
    if any(marker in compact for marker in SOL_PROVENANCE_COMPACT_MARKERS):
        raise RuntimeError("sol_sanitized_context_provenance_violation")
    return text


def _sol_normalized_probe(value: Any) -> tuple[str, str]:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return "", ""
    lowered = unicodedata.normalize("NFKC", raw).lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    return lowered, compact


def _engineering_packet_provenance_violation(packet: Mapping[str, Any]) -> bool:
    lowered, compact = _sol_normalized_probe(packet)
    if not lowered:
        return True
    if any(marker in lowered for marker in SOL_PROVENANCE_MARKERS):
        return True
    return any(marker in compact for marker in SOL_PROVENANCE_COMPACT_MARKERS)


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
        "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
        "reasoning_effort": "low",
        "provider_fallbacks": False,
    }


def _status_code(exc: Exception) -> int | None:
    for name in ("status_code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _credit_exhaustion_details(exc: Exception) -> dict[str, Any] | None:
    """Return safe diagnostics when a provider is blocked on account credits."""
    status = _status_code(exc)
    body = getattr(exc, "body", None)
    error = body.get("error", {}) if isinstance(body, Mapping) else {}
    if not isinstance(error, Mapping):
        error = {}
    code = str(error.get("code") or "").strip().casefold()
    err_type = str(error.get("type") or "").strip().casefold()
    text = " ".join(
        [
            type(exc).__name__,
            str(exc),
            str(error.get("message") or ""),
            code,
            err_type,
        ]
    ).casefold()
    markers = (
        "insufficient credits",
        "not enough credits",
        "insufficient_quota",
        "billing_hard_limit",
        "billing hard limit",
        "credit balance",
        "requires more credits",
        "can only afford",
        "payment required",
    )
    if status == 402 or code in {"insufficient_quota", "billing_hard_limit_reached"} or any(m in text for m in markers):
        out = {"status_code": status, "error_code": code or None, "error_type": err_type or None}
        return {k: v for k, v in out.items() if v is not None}
    return None


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
    credit = _credit_exhaustion_details(exc)
    if credit is not None:
        raise CreditTopupRequiredError(
            "OpenRouter",
            blocked_work=context,
            details=credit,
        ) from exc
    if "timeout" in name or "timedout" in name or "timeout" in text or "timed out" in text:
        raise TimeoutError(f"{context}_timeout") from exc
    if status == 429 or "ratelimit" in name or "rate limit" in text or "rate_limit" in text:
        raise OrchestrationRateLimitError(
            f"{context}_rate_limited", retry_after=_retry_after(exc)
        ) from exc
    if (status is not None and status >= 500) or any(
        token in name or token in text
        for token in ("serviceunavailable", "service unavailable", "connectionerror", "connection error", "temporarily unavailable")
    ):
        raise ProviderUnavailableError(
            f"{context}_provider_unavailable", retry_after=_retry_after(exc)
        ) from exc
    raise exc


def resolve_engineering_provider_mode(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    mode = str(
        source.get("ENGINEERING_PROVIDER_MODE", ENGINEERING_PROVIDER_LOCKED_RESERVE) or ""
    ).strip().upper()
    if mode not in {ENGINEERING_PROVIDER_ACTIVE, ENGINEERING_PROVIDER_LOCKED_RESERVE}:
        raise RuntimeError(f"invalid ENGINEERING_PROVIDER_MODE: {mode}")
    return mode


def resolve_openrouter_provider_mode(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    mode = str(
        source.get("OPENROUTER_PROVIDER_MODE", OPENROUTER_PROVIDER_LOCKED_RESERVE)
        or ""
    ).strip().upper()
    if mode not in {
        OPENROUTER_PROVIDER_ACTIVE,
        OPENROUTER_PROVIDER_LOCKED_RESERVE,
    }:
        raise RuntimeError(f"invalid OPENROUTER_PROVIDER_MODE: {mode}")
    return mode


def resolve_engineering_model(env: Mapping[str, str] | None = None) -> str:
    """Resolve authoritative ENGINEERING_MODEL with legacy CODEX_MODEL compatibility.

    If both variables are set, they must match. A mismatch fails closed before
    any provider call so stale legacy configuration cannot silently override the
    provider-neutral engineering contract.
    """
    source = os.environ if env is None else env
    primary = str(source.get("ENGINEERING_MODEL", "") or "").strip()
    legacy = str(source.get("CODEX_MODEL", "") or "").strip()
    if primary and legacy and primary != legacy:
        raise RuntimeError(
            f"engineering model config conflict (ENGINEERING_MODEL={primary}, CODEX_MODEL={legacy})"
        )
    return primary or legacy or EXPECTED_ENGINEERING_MODEL


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
    require_exact_model(codex_model, EXPECTED_ENGINEERING_MODEL, context="engineering")
    sanitized_context = _sol_context_text()
    context_digest = hashlib.sha256(sanitized_context.encode("utf-8")).hexdigest()

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        if _engineering_packet_provenance_violation(packet):
            raise RuntimeError("engineering_owner_provenance_blocked")
        prompt = (
            render_actor_contract(Actor.ENGINEERING)
            + "\nROLE: JAYTEC ENGINEERING SPECIALIST — GPT-5.6 SOL\n"
            + ENGINEERING_CONTRACT
            + "\nKNOWLEDGE_SCOPE: " + SOL_KNOWLEDGE_SCOPE
            + "\nSANITIZED_JAYTEC_CONTEXT_SHA256: " + context_digest
            + "\nSANITIZED_JAYTEC_CONTEXT:\n" + sanitized_context
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
            code = str(details.get("error_code") or "").strip().casefold()
            err_type = str(details.get("error_type") or "").strip().casefold()
            if code in {"insufficient_quota", "billing_hard_limit_reached"} or err_type in {"insufficient_quota", "billing_hard_limit_reached"}:
                raise CreditTopupRequiredError(
                    "OpenAI",
                    blocked_work="JAYTEC engineering specialist work",
                    details=details,
                ) from exc
            raise OrchestrationRateLimitError(
                "OpenAI rate limited",
                retry_after=details.get("retry_after"),
                details=details,
            ) from exc
        returned_provider_model = _provider_model(response)
        if returned_provider_model is None:
            raise RuntimeError(
                "engineering_provider_response: provider model identity unavailable"
            )
        require_exact_model(
            returned_provider_model,
            codex_model,
            context="engineering_provider_response",
        )
        result = json_object(response.output_text or "")
        result.setdefault("model", codex_model)
        result["bridge_diagnostics"] = {
            "provider_model": returned_provider_model,
            "model_identity_observed": True,
            "provider_fallbacks": False,
            "knowledge_scope": SOL_KNOWLEDGE_SCOPE,
            "sanitized_context_sha256": context_digest,
            "owner_provenance_firewall": True,
        }
        return result

    return circuit.guard(_dispatch)



def build_engineering_dispatch(
    *,
    openai_client: OpenAI,
    engineering_model: str,
    circuit: CircuitBreaker,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """Canonical provider-neutral engineering dispatch entrypoint.

    TaskPacket v1 still serializes the role under the legacy codex key, but new
    callers should use this function and ENGINEERING_MODEL terminology.
    """
    return build_codex_dispatch(
        openai_client=openai_client,
        codex_model=engineering_model,
        circuit=circuit,
    )

def build_gemini_dispatch(
    *,
    openrouter_client: OpenAI,
    gemini_model: str,
    gemini_timeout_s: float,
    circuit: CircuitBreaker,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    # Fail closed BEFORE any upstream call.
    require_exact_model(gemini_model, EXPECTED_GEMINI_MODEL, context="gemini")
    if gemini_timeout_s <= 0:
        raise ValueError("gemini_timeout_s must be positive")

    def _prompt(
        packet: Mapping[str, Any],
        *,
        retry_format: bool = False,
        fetch_engine: str | None = None,
    ) -> str:
        prefix = (
            render_actor_contract(Actor.GEMINI)
            + "\n"
            + GEMINI_RESEARCH_MODE_V1_1
        )
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
        read_source_url = (
            source_url_from_packet(packet) if is_jaytec_read_packet(packet) else None
        )
        request_kwargs: dict[str, Any] = {
            "model": gemini_model,
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
            "max_tokens": GEMINI_MAX_OUTPUT_TOKENS,
            "timeout": gemini_timeout_s,
            "stream": False,
            "response_format": {"type": "json_object"},
            "extra_body": {
                "provider": {
                    "sort": "price",
                    "allow_fallbacks": False,
                },
                "reasoning": {
                    "effort": "low",
                },
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
            _normalize_provider_exception(exc, context="gemini_provider")

        if not response.choices:
            raise RuntimeError("gemini_no_choices")

        returned_provider_model = _provider_model(response)
        if returned_provider_model is None:
            raise RuntimeError(
                "gemini_provider_response: provider model identity unavailable"
            )
        require_exact_model(
            returned_provider_model,
            gemini_model,
            context="gemini_provider_response",
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
            # Exactly one format-only retry; never loop indefinitely or silently
            # switch model/provider/profile.
            return _single_call(
                packet,
                retry_format=True,
                fetch_engine=fetch_engine,
            )

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        if not is_jaytec_read_packet(packet):
            return _single_with_format_retry(packet)

        attempts: list[dict[str, Any]] = []
        last_result: Mapping[str, Any] | None = None
        for engine in JAYTEC_READ_FETCH_ENGINES:
            result = _single_with_format_retry(packet, fetch_engine=engine)
            last_result = result
            conclusion = result.get("conclusion")
            report = (
                conclusion.get("READ_REPORT")
                if isinstance(conclusion, Mapping)
                else None
            )
            verified = (
                isinstance(report, Mapping)
                and report.get("VERIFIED") is True
            )
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

