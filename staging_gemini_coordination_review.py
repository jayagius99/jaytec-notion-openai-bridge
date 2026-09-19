"""One-shot Gemini advisory for JAYTEC coordination and task decomposition.

This path is advisory only. Gemini may recommend sequencing, parallel lanes,
specialist assignments, and risk controls. ChatGPT remains the sole coordinator
that can accept/reject those recommendations or assign work through JAYTEC.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch

ENABLED = os.environ.get("RUN_LIVE_GEMINI_COORDINATION_REVIEW", "0").strip() == "1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = min(max(float(os.environ.get("GEMINI_TIMEOUT_S", "90")), 10), 180)
MAX_CONTEXT_CHARS = 16000


def _load_context() -> Mapping[str, Any]:
    raw = os.environ.get("JAYTEC_COORDINATION_CONTEXT_JSON", "").strip()
    if not raw:
        raise RuntimeError("JAYTEC_COORDINATION_CONTEXT_JSON_REQUIRED")
    if len(raw) > MAX_CONTEXT_CHARS:
        raise RuntimeError("JAYTEC_COORDINATION_CONTEXT_TOO_LARGE")
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise RuntimeError("JAYTEC_COORDINATION_CONTEXT_INVALID")
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
    required = {
        "verdict",
        "recommended_parallel_lanes",
        "dependency_order",
        "specialist_assignment_suggestions",
        "do_not_duplicate",
        "risk_flags",
        "fastest_safe_sequence",
        "chatgpt_decisions_required",
    }
    if set(conclusion) != required:
        return True
    if not isinstance(conclusion.get("verdict"), str):
        return True
    for key in required - {"verdict"}:
        if not isinstance(conclusion.get(key), list):
            return True
    return False


def main() -> int:
    if not ENABLED:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_COORDINATION_REVIEW",
            "status": "SKIP",
            "reason": "RUN_LIVE_GEMINI_COORDINATION_REVIEW_DISABLED",
        }, sort_keys=True), flush=True)
        return 0

    if not OPENROUTER_API_KEY:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_COORDINATION_REVIEW",
            "status": "FAILED_CLOSED",
            "error": "OPENROUTER_API_KEY_NOT_CONFIGURED",
        }, sort_keys=True), flush=True)
        return 4

    context = _load_context()
    request = """Act as JAYTEC's independent coordination/reliability adviser.
You are NOT the coordinator and you may NOT assign or execute work.

Review only the supplied current-state packet. Recommend how ChatGPT can finish
the active work faster without reducing verification, authority, cost safety,
anti-duplication, or fail-closed behavior.

Rules:
- Jay -> ChatGPT -> JAYTEC -> specialists/resources remains absolute.
- ChatGPT is the final decision-maker and task assigner.
- You may suggest parallel lanes and specialist assignments, but cannot create
  tasks, route calls, mutate infrastructure, approve changes, or override
  ChatGPT.
- Do not use Notion or recommend inserting Notion into JAYTEC routing.
- Do not recommend provider/profile fallback to hide failures or costs.
- Preserve existing active-work ownership; do not duplicate work already
  running.
- Prefer the smallest independent tasks that can safely run in parallel.
- Explicitly identify dependencies that must remain serial.
- Treat missing evidence as a blocker, not as permission to infer success.

Return conclusion with EXACT keys:
- verdict: string
- recommended_parallel_lanes: array of strings
- dependency_order: array of strings
- specialist_assignment_suggestions: array of strings
- do_not_duplicate: array of strings
- risk_flags: array of strings
- fastest_safe_sequence: array of strings
- chatgpt_decisions_required: array of strings

No side effects. No implementation.
"""
    packet = {
        "task_id": "JAYTEC-COORDINATION-ADVISORY",
        "subtask_id": "GEMINI-COORDINATION-1",
        "request": request,
        "intent": "Advisory task decomposition and sequencing for faster reliable JAYTEC execution",
        "workflow_id": "JAYTEC_COORDINATION_ADVISORY",
        "risk_level": "read_only_review",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "analyze", "validate"],
        "expected_output": "Bounded advisory parallelization and dependency plan",
        "validation_requirements": [
            "ChatGPT remains final authority",
            "no side effects",
            "no Notion route",
            "no duplicate work",
            "no provider fallback",
            "preserve fail-closed verification",
        ],
        "side_effect_policy": "none",
        "idempotency_key": "jaytec-coordination-advisory-v1",
        "deadline": "2099-01-01T00:00:00Z",
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": {"current_state": context},
        "constraints": [
            "advisory only",
            "ChatGPT decides and assigns",
            "no implementation",
            "no Notion",
            "no Manus call",
            "no engineering write",
            "no provider change",
            "no spending change",
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
            "event": "JAYTEC_GEMINI_COORDINATION_REVIEW",
            "status": status,
            "result": result,
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if status == "PASS" else 5
    except Exception as exc:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_COORDINATION_REVIEW",
            "status": "FAILED_CLOSED",
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
