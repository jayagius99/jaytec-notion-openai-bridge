"""One-shot independent Gemini review of JAYTEC Manus governance.

Runs only when RUN_LIVE_GEMINI_MANUS_GOVERNANCE_REVIEW=1. This is review-only:
no engineering writes, no Notion, no Manus call, no provider fallback outside the
existing JAYTEC Gemini route.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from specialist_adapters import (
    EXPECTED_GEMINI_MODEL,
    build_gemini_dispatch,
)

ENABLED = os.environ.get("RUN_LIVE_GEMINI_MANUS_GOVERNANCE_REVIEW", "0").strip() == "1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = min(max(float(os.environ.get("GEMINI_TIMEOUT_S", "90")), 10), 180)

FILES = (
    "JAYTEC_COMMAND_POLICY.md",
    "MANUS_OPERATING_DIRECTIVE.md",
    "JAYTEC_RELATIONSHIP_CONTRACT.md",
    "manus_policy.py",
    "manus_governance.py",
    "relationship_policy.py",
    "manus_dispatch_contract.py",
    "manus_adapter.py",
    "participant_contracts.py",
)


def _sources() -> tuple[dict[str, str], dict[str, str]]:
    content: dict[str, str] = {}
    digests: dict[str, str] = {}
    for name in FILES:
        raw = Path(name).read_text(encoding="utf-8")
        content[name] = raw
        digests[name] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return content, digests


def _critical_issue(result: Mapping[str, Any]) -> bool:
    if str(result.get("status") or "") not in {"SUCCESS", "PARTIAL_SUCCESS", "NEEDS_VALIDATION"}:
        return True
    if result.get("side_effects_attempted") not in ([], None):
        return True
    conclusion = result.get("conclusion")
    if not isinstance(conclusion, Mapping):
        return True
    if conclusion.get("ready_for_live_manus_acceptance") is not True:
        return True
    gaps = conclusion.get("critical_gaps")
    bypass = conclusion.get("bypass_paths")
    if isinstance(gaps, list) and gaps:
        return True
    if isinstance(bypass, list) and bypass:
        return True
    return False


def main() -> int:
    if not ENABLED:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_MANUS_GOVERNANCE_REVIEW",
            "status": "SKIP",
            "reason": "RUN_LIVE_GEMINI_MANUS_GOVERNANCE_REVIEW_DISABLED",
        }, sort_keys=True), flush=True)
        return 0

    if not OPENROUTER_API_KEY:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_MANUS_GOVERNANCE_REVIEW",
            "status": "FAILED_CLOSED",
            "error": "OPENROUTER_API_KEY_NOT_CONFIGURED",
        }, sort_keys=True), flush=True)
        return 4

    sources, digests = _sources()
    request = """Independently review the proposed permanent Manus/JAYTEC
relationship as a hostile architecture reviewer. Look specifically for hidden
routing paths, authority escalation, stale-authority replay, implicit connector
inheritance, Notion becoming an executor/fallback, direct Manus access to
OpenAI/OpenRouter, paid Manus profile leakage, inability to verify Lite, fake
completion, duplicate work, cross-specialist delegation, prompt drift, and ways
Manus could modify JAYTEC outside its own house.

The intended invariant is:
Jay -> ChatGPT -> JAYTEC -> Manus / specialists.
Manus may autonomously improve only its own JAYTEC-controlled Manus layer.
Manus may inspect/recommend JAYTEC changes but may not independently modify
JAYTEC. Notion is transfer-only under current Jay-through-ChatGPT authority.
Manus direct resources are exactly GitHub, Neon, Render; connector presence does
not itself authorize mutation. Manus must be Lite unless Jay explicitly
authorizes a paid profile for that single current task. Unknown paths fail
closed.

Return conclusion as an object with EXACT keys:
- verdict: string
- critical_gaps: array of strings
- bypass_paths: array of strings
- strengths: array of strings
- required_fixes: array of strings
- ready_for_live_manus_acceptance: boolean

Set ready_for_live_manus_acceptance=true only if you find no critical authority,
routing, profile, connector, or completion-verification hole in the supplied
implementation. Be strict. Do not perform any write or external side effect.
"""

    packet = {
        "task_id": "JAYTEC-MANUS-GOVERNANCE-REVIEW",
        "subtask_id": "GEMINI-INDEPENDENT-REVIEW-1",
        "request": request,
        "intent": "Independent fail-closed governance review before live Manus acceptance",
        "workflow_id": "MANUS_GOVERNANCE_REVIEW",
        "risk_level": "read_only_review",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "analyze", "validate"],
        "expected_output": "Strict architecture review with explicit critical gaps and readiness flag",
        "validation_requirements": [
            "no side effects",
            "explicitly assess hidden paths",
            "explicitly assess Lite profile enforcement",
            "explicitly assess Notion isolation",
            "explicitly assess connector inheritance",
            "explicitly assess authority replay and self-escalation",
        ],
        "side_effect_policy": "none",
        "idempotency_key": "gemini-manus-governance-review-v1",
        "deadline": "2099-01-01T00:00:00Z",
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": {
            "source_digests": digests,
            "source_files": sources,
        },
        "constraints": [
            "review only",
            "do not use Notion",
            "do not call Manus",
            "do not perform engineering writes",
        ],
    }

    try:
        client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
        dispatch = build_gemini_dispatch(
            openrouter_client=client,
            gemini_model=GEMINI_MODEL,
            gemini_timeout_s=GEMINI_TIMEOUT_S,
            circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=60),
        )
        result = dict(dispatch(packet))
        output = {
            "event": "JAYTEC_GEMINI_MANUS_GOVERNANCE_REVIEW",
            "status": "PASS" if not _critical_issue(result) else "REVIEW_BLOCKED",
            "source_digests": digests,
            "result": result,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if output["status"] == "PASS" else 5
    except Exception as exc:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_MANUS_GOVERNANCE_REVIEW",
            "status": "FAILED_CLOSED",
            "error": type(exc).__name__,
            "message": str(exc)[:500],
            "source_digests": digests,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
