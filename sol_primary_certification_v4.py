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
    require(specialist_adapters.SOL_KNOWLEDGE_SCOPE == "JAYTEC_SANITIZED_CORE_V4", "knowledge_scope")

    base_path = Path("SOL_PRIMARY_SANITIZED_CONTEXT_V3.md")
    memory_path = Path("JAYTEC_SANITIZED_SYSTEM_MEMORY_V1.md")
    require(base_path.is_file(), "base_context_missing")
    require(memory_path.is_file(), "system_memory_missing")

    context = specialist_adapters._sol_context_text()
    require(bool(context.strip()), "context_nonempty")
    lowered = context.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    for marker in specialist_adapters.SOL_PROVENANCE_MARKERS:
        require(marker not in lowered, "context_marker:" + marker)
    for marker in specialist_adapters.SOL_PROVENANCE_COMPACT_MARKERS:
        require(marker not in compact, "context_compact_marker:" + marker)

    required_context_markers = [
        "WATCH execution model",
        "Active WATCH specialist team",
        "Manus operating relationship",
        "specialist request",
        "same Manus worker",
        "DeepSeek",
        "Nemo/Nemotron",
        "Proving Grounds",
        "Objective persistence",
        "Traffic control and handovers",
        "headless execution",
        "canonical unattended execution loop",
        "Forge cognition architecture",
        "GitHub working doctrine",
        "Live-state rule",
        "No individual model or worker is the whole system",
    ]
    for marker in required_context_markers:
        require(marker.casefold() in context.casefold(), "missing_jaytec_context:" + marker)

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
        require(
            specialist_adapters._engineering_packet_provenance_violation(probe),
            "probe_not_blocked",
        )

    source = Path("specialist_adapters.py").read_text(encoding="utf-8")
    staging = Path("staging_server.py").read_text(encoding="utf-8")
    broker = Path("watch_specialist_broker.py").read_text(encoding="utf-8")
    require("build_engineering_dispatch" in staging, "staging_engineering_route")
    require("ENGINEERING_PROVIDER_MODE" in staging, "provider_mode_gate")
    require("engineering_owner_provenance_blocked" in source, "provenance_gate")
    require("SOL_MEMORY_PATH" in source, "github_memory_binding")
    require("JAYTEC_SANITIZED_SYSTEM_MEMORY_V1.md" in source, "memory_file_binding")
    require("ALLOWED_MANUS_MODEL_SPECIALISTS" in broker, "manus_specialist_broker")
    require('"sol"' in broker and '"deepseek"' in broker and '"nemo"' in broker, "watch_trio")

    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    memory_digest = hashlib.sha256(memory_path.read_bytes()).hexdigest()
    print(
        "JAYTEC_SOL_PRIMARY_CERTIFICATION_V4 PASS "
        f"model=gpt-5.6-sol scope={specialist_adapters.SOL_KNOWLEDGE_SCOPE} "
        f"context_sha256={digest} memory_sha256={memory_digest} provider_spend=NOT_PERFORMED"
    )


if __name__ == "__main__":
    main()
