"""STAGING ONLY: one-shot exact-Codex 429 diagnostic.

This runs once per 24h idempotency window and never blocks staging boot.
It prints only redacted metadata required to classify the provider failure.
"""
from __future__ import annotations

import json

from orchestration import RateLimitError, redact
from staging_server import CODEX_DISPATCH, CODEX_MODEL, IDEMPOTENCY_STORE, REGISTRY

KEY = "codex-429-diagnostic-v1"
DIGEST = "codex-429-diagnostic-v1"

PACKET = {
    "task_id": "JAYTEC-G1-CODEX-DIAGNOSTIC",
    "subtask_id": "CODEX-429-DIAGNOSTIC-V1",
    "request": "Harmless staging provider diagnostic. Confirm receipt only. No tools, files, external actions or side effects.",
    "intent": "classify exact gpt-5.3-codex provider availability",
    "workflow_id": "JAYTEC_G1_CODEX_DIAGNOSTIC",
    "risk_level": "low",
    "specialist_plan": ["codex"],
    "allowed_operations": ["read", "validate"],
    "expected_output": "one harmless structured acknowledgement",
    "validation_requirements": ["exact model identity", "no side effects"],
    "side_effect_policy": "staging_only",
    "constraints": ["no tools", "no writes", "no external actions", "no fallback"],
}


def main() -> int:
    try:
        prior = REGISTRY.lookup(KEY, DIGEST)
    except Exception as exc:
        prior = None
        print(
            "JAYTEC_CODEX_429_DIAGNOSTIC "
            + json.dumps(
                {
                    "phase": "idempotency_lookup",
                    "status": "ERROR",
                    "error": type(exc).__name__,
                    "idempotency_store": IDEMPOTENCY_STORE,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    if prior is not None:
        print(
            "JAYTEC_CODEX_429_DIAGNOSTIC "
            + json.dumps(
                redact(
                    {
                        "phase": "provider_probe",
                        "status": "SKIP_REPLAY",
                        "model": CODEX_MODEL,
                        "idempotency_store": IDEMPOTENCY_STORE,
                        "prior": prior,
                    }
                ),
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    summary = {
        "phase": "provider_probe",
        "model": CODEX_MODEL,
        "idempotency_store": IDEMPOTENCY_STORE,
    }

    try:
        result = CODEX_DISPATCH(PACKET)
        summary.update(
            {
                "status": "SUCCESS",
                "returned_model": result.get("model") if isinstance(result, dict) else None,
            }
        )
    except RateLimitError as exc:
        summary.update(
            {
                "status": "RATE_LIMITED",
                "retry_after": getattr(exc, "retry_after", None),
                "provider_details": getattr(exc, "details", {}),
            }
        )
    except Exception as exc:
        summary.update({"status": "ERROR", "error": type(exc).__name__})

    safe = redact(summary)
    try:
        REGISTRY.store(KEY, DIGEST, safe)
    except Exception as exc:
        safe = {
            **safe,
            "idempotency_store_error": type(exc).__name__,
        }

    print(
        "JAYTEC_CODEX_429_DIAGNOSTIC " + json.dumps(redact(safe), sort_keys=True),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
