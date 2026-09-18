"""STAGING ONLY: one-shot exact-Gemini adversarial review of the engineering migration.

Uses the existing durable TaskPacket registry with a stable idempotency key, so
subsequent staging boots replay the stored review instead of re-spending provider
credits. No tools or side effects are authorized.
"""
from __future__ import annotations

import json

from orchestration import execute_task_packet_core, redact
from staging_server import GEMINI_DISPATCH, GEMINI_MODEL, REGISTRY

KEY = "engineering-migration-gemini-review-v2"
TASK_ID = "JAYTEC-2026-0003-G1"
SUBTASK_ID = "G1-ENGINEERING-MIGRATION-GEMINI-REVIEW"


def packet():
    return {
        "packet_version": "1.0",
        "task_id": TASK_ID,
        "subtask_id": SUBTASK_ID,
        "parent_task_id": TASK_ID,
        "request": (
            "Act as an independent adversarial architecture reviewer. Review staging commit "
            "migration lineage through 6e3cdfa1b7d9c7ca3a57d656bec05e6d89a28cd1 in "
            "jayagius99/jaytec-notion-openai-bridge. JAYTEC retains legacy TaskPacket key "
            "'codex' only for wire compatibility, but changes the semantic role from a "
            "hard-pinned gpt-5.3-codex specialist to a provider-neutral engineering-specialist "
            "contract whose current approved primary exact model is gpt-5.6-sol. Exact model "
            "identity checks, no silent fallback, fail-closed behavior, idempotency/replay "
            "protection, V1 isolation, G1/V2 gates, cost safety, and specialist-result validation "
            "must remain at least as strong as before. Identify regression risks, hidden coupling, "
            "backward-compatibility hazards, config/deployment hazards, security/cost concerns, "
            "and tests required before acceptance. State a verdict of PASS, PASS-WITH-FIXES, or "
            "FAIL in conclusion. Do not perform implementation or external tool actions."
        ),
        "intent": "independent migration safety review",
        "workflow_id": "WORKFLOW_ARCHITECTURE_DECISION",
        "risk_level": "low",
        "required_context": {
            "migration_baseline": "6e3cdfa1b7d9c7ca3a57d656bec05e6d89a28cd1",
            "current_primary_engineering_model": "gpt-5.6-sol",
            "legacy_wire_key": "codex",
            "sealed_v1": True,
        },
        "context_digests": {},
        "known_facts": [
            "existing 75-test suite passed on earlier migration commit",
            "a hidden render.yaml gpt-5.3-codex pin was found and fixed before acceptance",
            "new migration stress guards reject reintroduction of active gpt-5.3-codex pins",
        ],
        "constraints": [
            "no side effects",
            "no external tools",
            "no model fallback",
            "do not claim verified facts beyond supplied evidence",
            "NOTION_GATEWAY_POLICY: return any Notion need to ChatGPT; do not invoke Notion agent",
        ],
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "research", "analyze", "validate"],
        "expected_output": "adversarial migration verdict with risks and required validation",
        "validation_requirements": [
            "exact Gemini model identity",
            "no side effects",
            "explicit verdict",
            "identify uncertainty and missing evidence",
        ],
        "side_effect_policy": "none",
        "idempotency_key": KEY,
        "deadline": "2026-09-20T00:00:00+00:00",
        "max_fanout": 1,
        "max_retries": 0,
        "return_schema_version": "1.0",
    }


def main() -> int:
    p = packet()
    result = execute_task_packet_core(p, {"gemini": GEMINI_DISPATCH}, REGISTRY)
    g = result.get("gemini_result") if isinstance(result, dict) else None
    summary = {
        "event": "JAYTEC_GEMINI_MIGRATION_REVIEW",
        "overall_status": result.get("overall_status") if isinstance(result, dict) else "FAILED_CLOSED",
        "model": g.get("model") if isinstance(g, dict) else None,
        "status": g.get("status") if isinstance(g, dict) else None,
        "findings": g.get("findings", []) if isinstance(g, dict) else [],
        "evidence": g.get("evidence", []) if isinstance(g, dict) else [],
        "conclusion": g.get("conclusion") if isinstance(g, dict) else None,
        "unresolved_items": g.get("unresolved_items", []) if isinstance(g, dict) else result.get("unresolved_items", []),
        "side_effects_attempted": result.get("side_effects_attempted", []) if isinstance(result, dict) else [],
        "idempotent_replay": result.get("usage_summary", {}).get("idempotent_replay") if isinstance(result, dict) else None,
        "gemini_model_config": GEMINI_MODEL,
    }
    print(json.dumps(redact(summary), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["overall_status"] in {"SUCCESS", "NEEDS_VALIDATION", "PARTIAL_SUCCESS"} else 6


if __name__ == "__main__":
    raise SystemExit(main())
