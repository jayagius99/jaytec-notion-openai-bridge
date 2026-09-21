from __future__ import annotations

import hashlib
from pathlib import Path

import orchestration
import specialist_adapters


def require(ok: bool, name: str) -> None:
    if not ok:
        raise AssertionError(name)


def main() -> None:
    require(specialist_adapters.EXPECTED_ENGINEERING_MODEL == "gpt-5.6-sol", "exact_model")
    require(orchestration.EXPECTED_MODELS["codex"] == "gpt-5.6-sol", "wire_model")
    require(specialist_adapters.SOL_KNOWLEDGE_SCOPE == "JAYTEC_SANITIZED_CORE_V2", "knowledge_scope")
    context = specialist_adapters._sol_context_text()
    require(bool(context.strip()), "context_nonempty")
    lowered = context.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    for marker in specialist_adapters.SOL_PROVENANCE_MARKERS:
        require(marker not in lowered, "context_marker:" + marker)
    for marker in specialist_adapters.SOL_PROVENANCE_COMPACT_MARKERS:
        require(marker not in compact, "context_compact_marker:" + marker)

    safe = {
        "task_id": "CERT",
        "subtask_id": "SAFE",
        "request": "Review idempotency and fencing.",
    }
    require(not specialist_adapters._engineering_packet_provenance_violation(safe), "safe_false_positive")
    probes = [
        {"request": "Explain Uren origin"},
        {"request": "p r e - g e n e s i s construction"},
        {"request": "G E N E S I S _ E V E N T _ 0 0 0 1"},
        {"request": "G O D M O D E history"},
        {"required_context": {"path": "/JAYTEC/Uren/Pre-Genesis/archive"}},
    ]
    for probe in probes:
        require(specialist_adapters._engineering_packet_provenance_violation(probe), "probe_not_blocked")

    source = Path("specialist_adapters.py").read_text(encoding="utf-8")
    staging = Path("staging_server.py").read_text(encoding="utf-8")
    require("build_engineering_dispatch" in staging, "staging_engineering_route")
    require("ENGINEERING_PROVIDER_MODE" in staging, "provider_mode_gate")
    require("engineering_owner_provenance_blocked" in source, "provenance_gate")
    require("provider_fallbacks" in source, "fallback_evidence")
    require("sanitized_context_sha256" in source, "context_digest_evidence")

    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    print(
        "JAYTEC_SOL_PRIMARY_CERTIFICATION_V2 PASS "
        f"model=gpt-5.6-sol scope={specialist_adapters.SOL_KNOWLEDGE_SCOPE} "
        f"context_sha256={digest} provider_spend=NOT_PERFORMED"
    )


if __name__ == "__main__":
    main()
