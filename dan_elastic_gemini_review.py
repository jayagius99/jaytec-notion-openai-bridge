"""Independent JAYTEC Gemini hardening review for DAN / role-elastic completion.

Staging-only, one-shot, review-only. This module is deliberately inert on import.
It may call paid Gemini only through the same fail-closed terminal-reserve policy
used by the rest of JAYTEC. No environment flag or startup import can authorize
or trigger a paid provider call.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

from openai import OpenAI

from gemini_paid_reserve import require_gemini_paid_reserve

MODEL = "google/gemini-3.1-pro-preview"
WORKFLOW_ID = "JAYTEC_PAID_GEMINI_RESERVE_DAN_ELASTIC_HARDENING_V1"
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

PROMPT = """You are the JAYTEC independent hardening reviewer. Review-only; no tools,
writes, deployments, purchases, credentials, or external side effects.

Contract under review:
- Roles remain ownership/accountability labels, not hard capability walls.
- For an owner-authorized benign objective, a specialist may perform adjacent
  legitimate capabilities needed to complete the SAME task.
- Capability never creates authority.
- STOP/HOLD, safety, permissions, spend gates, canonical state, idempotency,
  collision rules, and evidence requirements remain binding.
- Deterministic facts should be computed/verified by deterministic runtime when
  possible rather than trusted to generative output.
- Provider/model fallback is explicit and controlled; no silent paid fallback.
- Completion claims require evidence, not self-report.

Observed evidence/failures:
1. Strict research-only baseline stopped on an adjacent JSON transformation.
2. Role-elastic version completed that transformation.
3. First elastic version produced an incorrect SHA-256; runtime verification was
   then made authoritative and the corrected flow passed.
4. Live boundary tests preserved HOLD, secret protection, spend gate,
   prompt-injection resistance, stale-state refresh, ambiguous-write reconciliation,
   and adjacent benign capability allowance.
5. A broader regression produced the correct 3 task outputs and 8 boundary outputs,
   but the verifier initially rejected a top-level array shape. Verifier was hardened
   to treat transport/schema conformance separately from semantic correctness.
6. DeepSeek and Nemo review routes may be unavailable; the caller must provide
   current bounded exhaustion evidence rather than relying on this statement.

Task:
Attack this design for remaining ambiguity, overreach, false-completion risk,
verifier weakness, route/fallback danger, stale-state/collision risk, and
unbounded 'do anything' interpretation. Recommend only concrete changes that
materially improve reliability while preserving useful role elasticity.
Classify each recommendation as REQUIRED, SHOULD, or OPTIONAL.
Also propose a concise production acceptance standard and a concise definition
of what DAN means and does NOT mean in JAYTEC.
Return JSON only."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["PASS_WITH_HARDENING", "NEEDS_MORE_EVIDENCE", "REJECT"]},
        "model": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "priority": {"type": "string", "enum": ["REQUIRED", "SHOULD", "OPTIONAL"]},
                    "change": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["priority", "change", "reason"],
            },
        },
        "acceptance_standard": {"type": "array", "items": {"type": "string"}},
        "dan_means": {"type": "string"},
        "dan_does_not_mean": {"type": "string"},
        "residual_limits": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "model",
        "strengths",
        "risks",
        "recommendations",
        "acceptance_standard",
        "dan_means",
        "dan_does_not_mean",
        "residual_limits",
    ],
}


def _validate_specialized_reserve_packet(packet: Mapping[str, Any]) -> None:
    """Apply the global paid-reserve gate plus this review's exact workflow lock."""
    require_gemini_paid_reserve(packet)
    if packet.get("workflow_id") != WORKFLOW_ID:
        raise RuntimeError("GEMINI_PAID_RESERVE_POLICY_BLOCKED:dan_elastic_workflow_required")
    allowed = packet.get("allowed_operations")
    if not isinstance(allowed, list) or not set(allowed).issubset({"research", "validate"}):
        raise RuntimeError("GEMINI_PAID_RESERVE_POLICY_BLOCKED:dan_elastic_operations_invalid")


def run(
    packet: Mapping[str, Any],
    *,
    openrouter_client: OpenAI | None = None,
) -> dict[str, Any]:
    """Run only after a caller supplies a complete, currently valid reserve proof."""
    _validate_specialized_reserve_packet(packet)

    client = openrouter_client
    if client is None:
        if not API_KEY:
            raise RuntimeError("OPENROUTER_API_KEY_MISSING")
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT}],
        temperature=0,
        max_tokens=2600,
        stream=False,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "jaytec_dan_elastic_hardening_review",
                "strict": True,
                "schema": SCHEMA,
            },
        },
        extra_body={
            "provider": {"allow_fallbacks": False, "require_parameters": True},
            "reasoning": {"effort": "medium"},
        },
    )
    if not r.choices:
        raise RuntimeError("no_choices")
    returned = getattr(r, "model", None)
    if returned != MODEL:
        raise RuntimeError("model_identity_mismatch:" + str(returned))

    content = r.choices[0].message.content or ""
    parsed = json.loads(content)
    if parsed.get("model") != MODEL:
        parsed["model"] = MODEL

    return {
        "schema": "JAYTEC_DAN_ELASTIC_GEMINI_HARDENING_REVIEW_V1",
        "response_id": getattr(r, "id", None),
        "model": returned,
        "finish_reason": getattr(r.choices[0], "finish_reason", None),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "review": parsed,
        "provider_fallback": False,
        "side_effects": False,
        "paid_reserve_policy_enforced": True,
    }
