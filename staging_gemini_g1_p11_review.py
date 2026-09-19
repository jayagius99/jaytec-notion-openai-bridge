"""One-shot exact-Gemini independent G1 P11 review for V2 unlock.

Review-only. Uses the bounded sanitized P11 evidence packet supplied through
JAYTEC_G1_REVIEW_PACKET_JSON. No tools, writes, provider fallback, Manus, Notion,
or engineering actions are authorized.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch

ENABLED = os.environ.get("RUN_LIVE_GEMINI_G1_P11_REVIEW", "0").strip() == "1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = min(max(float(os.environ.get("GEMINI_TIMEOUT_S", "120")), 10), 180)
MAX_PACKET_CHARS = 24000


def _load_evidence() -> Mapping[str, Any]:
    raw = os.environ.get("JAYTEC_G1_REVIEW_PACKET_JSON", "").strip()
    if not raw:
        raise RuntimeError("JAYTEC_G1_REVIEW_PACKET_JSON_REQUIRED")
    if len(raw) > MAX_PACKET_CHARS:
        raise RuntimeError("G1_P11_PACKET_TOO_LARGE")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise RuntimeError("G1_P11_PACKET_INVALID")
    return value


def _blocked(result: Mapping[str, Any]) -> bool:
    if str(result.get("status") or "") not in {
        "SUCCESS", "PARTIAL_SUCCESS", "NEEDS_VALIDATION"
    }:
        return True
    if result.get("side_effects_attempted") not in ([], None):
        return True
    conclusion = result.get("conclusion")
    if not isinstance(conclusion, Mapping):
        return True
    verdict = str(conclusion.get("verdict") or "")
    if verdict not in {"PASS", "PASS_WITH_FINDINGS", "FAIL"}:
        return True
    if conclusion.get("safe_to_unlock_v2") is not True:
        return True
    if conclusion.get("critical_findings") not in ([], None):
        return True
    if conclusion.get("missing_evidence") not in ([], None):
        return True
    return False


def main() -> int:
    if not ENABLED:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_G1_P11_REVIEW",
            "status": "SKIP",
            "reason": "RUN_LIVE_GEMINI_G1_P11_REVIEW_DISABLED",
        }, sort_keys=True), flush=True)
        return 0

    if not OPENROUTER_API_KEY:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_G1_P11_REVIEW",
            "status": "FAILED_CLOSED",
            "error": "OPENROUTER_API_KEY_NOT_CONFIGURED",
        }, sort_keys=True), flush=True)
        return 4

    evidence = _load_evidence()
    request = """Act as an INDEPENDENT adversarial software-safety reviewer for
JAYTEC G1 proof P11. You did not implement G1. Review ONLY the supplied
sanitized evidence packet. Do not rely on conversational memory and do not
perform implementation or external actions.

P11 is the final gate before V2 may unlock. The reviewer cannot unlock V2
directly. A PASS must be affirmative and evidence-based, not merely absence of
comments.

Challenge:
1. hidden V1/current-production dependency;
2. duplicate/replay safety and ambiguous external-effect recovery;
3. stale-worker fencing and execution ownership;
4. workflow/version ownership;
5. exact provider/model identity and fail-closed behavior;
6. malformed/truncated/oversized result containment;
7. real concurrency/rate limiting and bounded behavior;
8. Windows V1/V2 coexistence;
9. destructive V2/G1 isolation;
10. proof-gate bypass or unsupported PASS claims;
11. any mismatch between the reviewed head and the last live runtime candidate.

If the evidence packet is insufficient to affirm any required challenge, set
safe_to_unlock_v2=false and identify the SMALLEST exact missing proof.

Return conclusion as an object with EXACT keys:
- verdict: PASS | PASS_WITH_FINDINGS | FAIL
- safe_to_unlock_v2: boolean
- critical_findings: array of strings
- evidence_challenges: array of strings
- missing_evidence: array of strings
- affirmative_coverage: object whose keys are the 11 challenge areas above and
  whose values are concise evidence-based conclusions
- candidate_delta_assessment: string
- confidence: LOW | MEDIUM | HIGH
- final_reason: string

Set safe_to_unlock_v2=true ONLY when there are no critical findings and no
missing evidence preventing affirmative P11 coverage. Do not weaken the gate.
No side effects.
"""

    packet = {
        "task_id": "JAYTEC-G1-P11-INDEPENDENT-REVIEW",
        "subtask_id": "GEMINI-P11-ADVERSARIAL-1",
        "request": request,
        "intent": "Independent evidence-based P11 challenge before V2 unlock",
        "workflow_id": "G1_P11_INDEPENDENT_REVIEW",
        "risk_level": "read_only_review",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "analyze", "validate"],
        "expected_output": "Strict P11 verdict and safe_to_unlock_v2 flag",
        "validation_requirements": [
            "no side effects",
            "exact Gemini identity",
            "affirmative evidence coverage of all P11 challenge areas",
            "identify smallest missing proof if not safe",
            "do not infer facts outside the sanitized packet",
        ],
        "side_effect_policy": "none",
        "idempotency_key": "g1-p11-gemini-independent-review-v1",
        "deadline": "2099-01-01T00:00:00Z",
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": {"sanitized_g1_evidence_packet": evidence},
        "constraints": [
            "review only",
            "no implementation",
            "no Notion",
            "no Manus",
            "no engineering writes",
            "no provider fallback",
            "reviewer cannot directly unlock V2",
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
        status = "REVIEW_BLOCKED" if _blocked(result) else "PASS"
        print(json.dumps({
            "event": "JAYTEC_GEMINI_G1_P11_REVIEW",
            "status": status,
            "reviewed_candidate": evidence.get("candidate_review_head"),
            "last_runtime_candidate": evidence.get("last_runtime_candidate"),
            "result": result,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if status == "PASS" else 5
    except Exception as exc:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_G1_P11_REVIEW",
            "status": "FAILED_CLOSED",
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
