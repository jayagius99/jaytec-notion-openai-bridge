"""STAGING ONLY: bounded live-provider probe for JAYTEC orchestration.

Runs harmless Gemini-only, Codex-only, and combined task packets from inside the
Render staging runtime.

Safety:
- Never prints prompts, findings, evidence, credentials, or raw provider responses.
- Emits only redacted contract metadata.
- Adds idempotency-store observability and a restart sentinel for durable stores.

IMPORTANT: The durability sentinel/probe does NOT call providers. It only
exercises the idempotency registry layer.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable

from orchestration import SAFE_OPERATIONS, execute_task_packet_core, redact
from staging_server import (
    CODEX_DISPATCH,
    CODEX_MODEL,
    GEMINI_DISPATCH,
    GEMINI_MODEL,
    IDEMPOTENCY_STORE,
    MCP_AUTH_TOKEN,
    OPENAI_CLIENT,
    OPENROUTER_CLIENT,
    REGISTRY,
)

TASK_ID = "JAYTEC-2026-0001"
WORKFLOW_ID = "WORKFLOW_ARCHITECTURE_DECISION"
BAD_TERMINAL_STATUSES = {
    "POLICY_BLOCKED",
    "FAILED_CLOSED",
    "INVALID_PACKET",
    "TIMEOUT",
    "RATE_LIMITED",
}


def _packet(subtask_id: str, specialists: Iterable[str], idempotency_key: str) -> Dict[str, Any]:
    plan = list(specialists)
    return {
        "packet_version": "1.0",
        "task_id": TASK_ID,
        "subtask_id": subtask_id,
        "parent_task_id": TASK_ID,
        "request": (
            "Harmless staging runtime validation. Preserve TASK_ID and SUBTASK_ID. "
            "Return one concise validation finding confirming receipt of the packet. "
            "Do not use external tools, modify code, write files, mutate production, "
            "or request any side effect."
        ),
        "intent": "validate live staging specialist adapter and Bridge Contract v1",
        "workflow_id": WORKFLOW_ID,
        "risk_level": "low",
        "required_context": {
            "environment": "isolated Render staging",
            "purpose": "runtime adapter verification only",
        },
        "context_digests": {},
        "known_facts": ["production is out of scope", "no vehicle operations are allowed"],
        "constraints": [
            "no production writes",
            "no vehicle or PCM operations",
            "no credentials in output",
            "no external tool actions",
        ],
        "specialist_plan": plan,
        "allowed_operations": ["read", "research", "analyze", "validate", "test"],
        "expected_output": "normalized structured result envelope",
        "validation_requirements": [
            "preserve task_id and subtask_id",
            "exact specialist model identity",
            "no side effects",
            "no unauthorized requested operations",
        ],
        "side_effect_policy": "staging_only",
        "idempotency_key": idempotency_key,
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat(),
        "max_fanout": len(plan),
        "max_retries": 1,
        "return_schema_version": "1.0",
    }


def _worker_meta(result: Any) -> Dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "status": result.get("status"),
        "model": result.get("model"),
        "unresolved_items": result.get("unresolved_items", []),
        "requested_operations": result.get("requested_operations", []),
        "side_effects_attempted": result.get("side_effects_attempted", []),
    }


def _summarize(label: str, packet: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    worker_order = [
        item.get("specialist")
        for item in result.get("worker_trace", [])
        if isinstance(item, dict)
    ]
    summary = {
        "phase": label,
        "overall_status": result.get("overall_status"),
        "ids_preserved": (
            result.get("task_id") == packet["task_id"]
            and result.get("subtask_id") == packet["subtask_id"]
        ),
        "codex_result": _worker_meta(result.get("codex_result")),
        "gemini_result": _worker_meta(result.get("gemini_result")),
        "worker_order": worker_order,
        "conflicts_count": len(result.get("conflicts", [])),
        "unresolved_items": result.get("unresolved_items", []),
        "side_effects_attempted": result.get("side_effects_attempted", []),
        "approval_required": result.get("approval_required"),
        "retry_count": result.get("usage_summary", {}).get("retry_count", 0),
        "return_schema_version": result.get("return_schema_version"),
        "idempotent_replay": result.get("usage_summary", {}).get("idempotent_replay"),
    }
    return redact(summary)


def _operations_safe(meta: Dict[str, Any]) -> bool:
    requested = meta.get("requested_operations", [])
    if not isinstance(requested, list):
        return False
    return all(op in SAFE_OPERATIONS for op in requested)


def _phase_ok(summary: Dict[str, Any], expected_specialists: Iterable[str]) -> bool:
    if summary.get("overall_status") in BAD_TERMINAL_STATUSES:
        return False
    if not summary.get("ids_preserved"):
        return False
    if summary.get("side_effects_attempted"):
        return False
    if summary.get("approval_required") not in (False, None):
        return False
    if summary.get("return_schema_version") != "1.0":
        return False

    expected = set(expected_specialists)
    if "codex" in expected:
        meta = summary.get("codex_result") or {}
        if meta.get("model") != CODEX_MODEL or meta.get("status") in BAD_TERMINAL_STATUSES:
            return False
        if meta.get("status") is None or not _operations_safe(meta) or meta.get("side_effects_attempted"):
            return False
    if "gemini" in expected:
        meta = summary.get("gemini_result") or {}
        if meta.get("model") != GEMINI_MODEL or meta.get("status") in BAD_TERMINAL_STATUSES:
            return False
        if meta.get("status") is None or not _operations_safe(meta) or meta.get("side_effects_attempted"):
            return False
    return True


def _durability_sentinel() -> Dict[str, Any]:
    """Startup-safe sentinel that proves whether durable state is visible.

    - For memory: reports SKIP.
    - For postgres: attempts lookup of prior sentinel payload and sets restart_proof.

    Does not call providers.
    """
    if IDEMPOTENCY_STORE != "postgres":
        return {"status": "SKIP", "idempotency_store": IDEMPOTENCY_STORE}

    boot_id = str(uuid.uuid4())
    key = "durability-sentinel"
    digest = "sentinel-v1"
    now = datetime.now(timezone.utc)

    prior = None
    try:
        prior = REGISTRY.lookup(key, digest, now=now)
    except Exception as exc:
        return {
            "status": "FAIL",
            "idempotency_store": IDEMPOTENCY_STORE,
            "error": type(exc).__name__,
        }

    restart_proof = False
    prior_boot_id = None
    if isinstance(prior, dict):
        prior_boot_id = prior.get("boot_id")
        if prior_boot_id and prior_boot_id != boot_id:
            restart_proof = True

    payload = {
        "boot_id": boot_id,
        "observed_prior": bool(prior_boot_id),
        "prior_boot_id": prior_boot_id,
        "ts": now.isoformat(),
    }

    try:
        REGISTRY.store(key, digest, payload, now=now)
    except Exception as exc:
        return {
            "status": "FAIL",
            "idempotency_store": IDEMPOTENCY_STORE,
            "error": type(exc).__name__,
        }

    return {
        "status": "PASS",
        "idempotency_store": IDEMPOTENCY_STORE,
        "restart_proof": restart_proof,
        "observed_prior": bool(prior_boot_id),
    }


def main() -> int:
    config = {
        "phase": "configuration",
        "status": "STAGING",
        "mcp_auth_configured": bool(MCP_AUTH_TOKEN),
        "codex_model": CODEX_MODEL,
        "codex_adapter_configured": OPENAI_CLIENT is not None,
        "gemini_model": GEMINI_MODEL,
        "gemini_adapter_configured": OPENROUTER_CLIENT is not None,
        "idempotency_store": IDEMPOTENCY_STORE,
        "production_ready": False,
        "durability_sentinel": _durability_sentinel(),
    }
    print("JAYTEC_STAGING_RUNTIME_PROBE " + json.dumps(redact(config), sort_keys=True), flush=True)

    if not all(
        [
            config["mcp_auth_configured"],
            config["codex_adapter_configured"],
            config["gemini_adapter_configured"],
            CODEX_MODEL == "gpt-5.3-codex",
            GEMINI_MODEL == "google/gemini-3.1-pro-preview",
        ]
    ):
        print(
            "JAYTEC_STAGING_RUNTIME_PROBE "
            + json.dumps({"phase": "configuration", "probe_pass": False}, sort_keys=True),
            flush=True,
        )
        return 2

    # Use staging_server's configured registry (memory or Postgres).
    registry = REGISTRY

    phases = [
        (
            "gemini_only",
            _packet("JAYTEC-2026-0001-V2", ["gemini"], "runtime-probe-v2-gemini"),
            {"gemini": GEMINI_DISPATCH},
        ),
        (
            "codex_only",
            _packet("JAYTEC-2026-0001-V3", ["codex"], "runtime-probe-v3-codex"),
            {"codex": CODEX_DISPATCH},
        ),
        (
            "combined",
            _packet("JAYTEC-2026-0001-V4", ["gemini", "codex"], "runtime-probe-v4-combined"),
            {"codex": CODEX_DISPATCH, "gemini": GEMINI_DISPATCH},
        ),
    ]

    all_ok = True
    for label, packet, dispatchers in phases:
        result = execute_task_packet_core(packet, dispatchers, registry)
        summary = _summarize(label, packet, result)
        phase_ok = _phase_ok(summary, packet["specialist_plan"])
        summary["probe_pass"] = phase_ok
        all_ok = all_ok and phase_ok
        print(
            "JAYTEC_STAGING_RUNTIME_PROBE " + json.dumps(redact(summary), sort_keys=True),
            flush=True,
        )

    final = {"phase": "final", "probe_pass": all_ok}
    print("JAYTEC_STAGING_RUNTIME_PROBE " + json.dumps(final, sort_keys=True), flush=True)
    return 0 if all_ok else 3


if __name__ == "__main__":
    sys.exit(main())
