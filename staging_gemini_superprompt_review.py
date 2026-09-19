"""One-shot Gemini adversarial review of the JAYTEC Live research SUPERPROMPT.

Review-only. No Notion, Manus, engineering writes, connector mutation, provider
fallback, or implementation authority.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch

ENABLED = os.environ.get("RUN_LIVE_GEMINI_SUPERPROMPT_REVIEW", "0").strip() == "1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = min(
    max(float(os.environ.get("GEMINI_TIMEOUT_S", "120")), 10), 180
)

FILES = (
    "research/JAYTEC_LIVE_RESEARCH_SUPERPROMPT_V3.md",
    "JAYTEC_COMMAND_POLICY.md",
    "MANUS_OPERATING_DIRECTIVE.md",
    "relationship_policy.py",
    "manus_governance.py",
    "manus_policy.py",
    "manus_dispatch_contract.py",
    "manus_adapter.py",
    "orchestration.py",
    "idempotency_postgres.py",
)


def _sources() -> tuple[dict[str, str], dict[str, str]]:
    content: dict[str, str] = {}
    digests: dict[str, str] = {}
    for name in FILES:
        raw = Path(name).read_text(encoding="utf-8")
        content[name] = raw
        digests[name] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return content, digests


def _blocked(result: Mapping[str, Any]) -> bool:
    if str(result.get("status") or "") not in {
        "SUCCESS",
        "PARTIAL_SUCCESS",
        "NEEDS_VALIDATION",
    }:
        return True
    if result.get("side_effects_attempted") not in ([], None):
        return True
    conclusion = result.get("conclusion")
    if not isinstance(conclusion, Mapping):
        return True
    if conclusion.get("hierarchy_preserved") is not True:
        return True
    if conclusion.get("research_only_boundary_preserved") is not True:
        return True
    return False


def main() -> int:
    if not ENABLED:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_SUPERPROMPT_REVIEW",
            "status": "SKIP",
            "reason": "RUN_LIVE_GEMINI_SUPERPROMPT_REVIEW_DISABLED",
        }, sort_keys=True), flush=True)
        return 0

    if not OPENROUTER_API_KEY:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_SUPERPROMPT_REVIEW",
            "status": "FAILED_CLOSED",
            "error": "OPENROUTER_API_KEY_NOT_CONFIGURED",
        }, sort_keys=True), flush=True)
        return 4

    sources, digests = _sources()

    request = """Act as JAYTEC's independent architecture/reliability reviewer.

Review the supplied JAYTEC LIVE SYSTEM research SUPERPROMPT together with the
current JAYTEC policy/runtime source excerpts. This is a prompt-quality and
research-design review ONLY. Do not implement anything.

The non-negotiable hierarchy is:
Jay -> ChatGPT coordinator -> JAYTEC durable authoritative system -> specialists.
Manus is the AUTOMATION SPECIALIST, not Chief Operator or coordinator.
GPT-5.6 Sol is Engineering Specialist.
Gemini 3.1 Pro is Reviewer/Research Specialist.

Your job is to make the research assignment harder to misinterpret and stronger
for eventual implementation planning. Be adversarial. Look for ambiguities that
could later produce:
- Manus becoming coordinator/global owner;
- ChatGPT becoming a 24/7 process dependency;
- duplicate schedulers/ledgers/watchdogs/executors;
- conversational claims being treated as evidence;
- task authority becoming mutation/spend/privilege authority;
- stale authority replay;
- split-brain/multi-chat execution;
- missing leases/fencing/idempotency/reconciliation;
- exactly-once fallacies;
- unsafe retry after uncertain side effects;
- missing cancellation/revocation;
- missing event/webhook durability;
- missing disaster recovery;
- weak policy/version/schema controls;
- prompt/tool-result injection;
- provider/profile/connector substitution;
- hidden paid fallbacks;
- poor cost/quota classification;
- weak completion/evidence rules;
- untestable definitions of LIVE;
- research output accidentally authorizing implementation.

Do NOT rewrite the whole prompt. Return compact, precise amendments that ChatGPT
can integrate.

In conclusion return an object with EXACT keys:
- verdict: string
- hierarchy_preserved: boolean
- research_only_boundary_preserved: boolean
- critical_ambiguities: array of strings
- enforcement_clauses_to_add: array of strings
- reliability_research_topics_to_add: array of strings
- certification_tests_to_add: array of strings
- anti_duplication_strengthening: array of strings
- authority_model_strengthening: array of strings
- manus_role_strengthening: array of strings
- chatgpt_jaytec_continuity_strengthening: array of strings
- cost_safety_strengthening: array of strings
- wording_or_structure_changes: array of strings
- implementation_enforcement_notes: array of strings
- disagreements_or_cautions: array of strings
- strongest_missing_requirement: string
- final_recommendation: string

Do not weaken the hierarchy. Do not recommend making Manus coordinator.
Do not recommend implementation or spending. Preserve meaningful disagreement.
Use supplied implementation evidence to distinguish what already exists from
what still requires research.
"""

    packet = {
        "task_id": "JAYTEC-LIVE-SUPERPROMPT-REVIEW",
        "subtask_id": "GEMINI-ADVERSARIAL-PROMPT-REVIEW-1",
        "request": request,
        "intent": "Strengthen the JAYTEC Live research assignment before any implementation phase",
        "workflow_id": "JAYTEC_LIVE_RESEARCH_PROMPT_REVIEW",
        "risk_level": "read_only_review",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "analyze", "validate"],
        "expected_output": "Adversarial amendment set for the research superprompt",
        "validation_requirements": [
            "preserve Jay -> ChatGPT -> JAYTEC -> specialists hierarchy",
            "keep Manus as automation specialist",
            "preserve research-only no-implementation boundary",
            "identify enforceable reliability gaps",
            "separate existing evidence from proposals",
            "no side effects",
        ],
        "side_effect_policy": "none",
        "idempotency_key": "jaytec-live-superprompt-gemini-review-v1",
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
            "no implementation",
            "no Notion",
            "no Manus calls",
            "no engineering writes",
            "no provider change",
            "no billing change",
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
            "event": "JAYTEC_GEMINI_SUPERPROMPT_REVIEW",
            "status": "REVIEW_BLOCKED" if _blocked(result) else "PASS",
            "source_digests": digests,
            "result": result,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if output["status"] == "PASS" else 5
    except Exception as exc:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_SUPERPROMPT_REVIEW",
            "status": "FAILED_CLOSED",
            "error": type(exc).__name__,
            "message": str(exc)[:500],
            "source_digests": digests,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
