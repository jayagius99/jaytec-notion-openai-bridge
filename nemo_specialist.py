"""Exact-model Nemo/Nemotron specialist adapter for JAYTEC.

Nemo is a subordinate zero-cost-capable OpenRouter specialist. It never receives
WATCH authority, provider fallback authority, or direct mutation authority.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from worker_json import WorkerJsonError, json_object_with_diagnostics

EXPECTED_NEMO_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
NEMO_MAX_OUTPUT_TOKENS = 3000

NEMO_CONTRACT = """ROLE: JAYTEC SUBORDINATE NEMO/NEMOTRON SPECIALIST
Jay is owner/root authority. ChatGPT/JAYTEC is controller and convergence authority.
You provide bounded engineering/reasoning/review assistance only.
Do not self-initiate work, broaden scope, approve your own work, mutate repositories,
change deployments, spend money, change credentials, or dispatch another specialist.
Do not infer or reconstruct sealed identity/origin provenance.
Return ONLY one JSON object with:
status, model, findings, evidence, confidence, conclusion, unresolved_items,
files_or_artifacts, architecture_changes_required, knowledge_writeback_proposal,
side_effects_attempted, requested_operations.
model MUST be exactly nvidia/nemotron-3-ultra-550b-a55b:free.
side_effects_attempted MUST be [] and requested_operations MUST be [].
Never include credentials, secrets, markdown fences, or surrounding prose.
"""


def _provider_model(response: Any) -> str | None:
    value = getattr(response, "model", None)
    return value if isinstance(value, str) and value.strip() else None


def _finish_reason(choice: Any) -> str | None:
    value = getattr(choice, "finish_reason", None)
    return value if isinstance(value, str) and value.strip() else None


def _diagnostics(
    *,
    content: str,
    finish_reason: str | None,
    provider_model: str | None,
    extracted: bool,
) -> dict[str, Any]:
    encoded = (content or "").encode("utf-8", errors="replace")
    return {
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_bytes": len(encoded),
        "finish_reason": finish_reason,
        "provider_model": provider_model,
        "extracted_balanced_object": bool(extracted),
        "provider_fallbacks": False,
    }


def build_nemo_dispatch(
    *,
    openrouter_client: OpenAI,
    model: str,
    timeout_s: float,
    circuit: CircuitBreaker,
    max_output_tokens: int = NEMO_MAX_OUTPUT_TOKENS,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    if model != EXPECTED_NEMO_MODEL:
        raise RuntimeError(
            "nemo_model_mismatch:"
            f"expected={EXPECTED_NEMO_MODEL};got={model}"
        )
    if timeout_s <= 0:
        raise ValueError("nemo_timeout_must_be_positive")
    if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 4000:
        raise ValueError("nemo_max_output_tokens_invalid")

    def _single(packet: Mapping[str, Any], *, retry_format: bool) -> Mapping[str, Any]:
        prompt = NEMO_CONTRACT
        if retry_format:
            prompt += (
                "\nYour previous transport output was malformed. Re-answer the original "
                "task from scratch as one compact JSON object only."
            )
        prompt += "\nTASK_PACKET_JSON:\n" + json.dumps(
            packet, sort_keys=True, ensure_ascii=False
        )
        response = openrouter_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=max_output_tokens,
            timeout=timeout_s,
            stream=False,
            extra_body={
                "provider": {
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }
            },
        )
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError("nemo_no_choices")
        returned = _provider_model(response)
        if returned is None:
            raise RuntimeError("nemo_provider_model_unobservable")
        if returned != model:
            raise RuntimeError(
                f"nemo_provider_model_mismatch:expected={model};got={returned}"
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        content = str(getattr(message, "content", "") or "")
        result, parsed = json_object_with_diagnostics(content)
        result.setdefault("model", model)
        if result.get("model") != model:
            raise RuntimeError("nemo_result_model_mismatch")
        if result.get("side_effects_attempted") not in (None, []):
            raise RuntimeError("nemo_side_effect_violation")
        if result.get("requested_operations") not in (None, []):
            raise RuntimeError("nemo_requested_operations_nonempty")
        result["bridge_diagnostics"] = _diagnostics(
            content=content,
            finish_reason=_finish_reason(choice),
            provider_model=returned,
            extracted=parsed.extracted_object,
        )
        return result

    def _dispatch(packet: Mapping[str, Any]) -> Mapping[str, Any]:
        context = packet.get("required_context")
        if not isinstance(context, Mapping):
            raise RuntimeError("nemo_authority_context_required")
        if context.get("authority_controller") != "CHATGPT_OPENAI_LEAD":
            raise RuntimeError("nemo_controller_authority_required")
        if context.get("specialist_authority") != "SUBORDINATE":
            raise RuntimeError("nemo_must_be_subordinate")
        try:
            return _single(packet, retry_format=False)
        except WorkerJsonError:
            return _single(packet, retry_format=True)

    return circuit.guard(_dispatch)
