from __future__ import annotations

import hashlib
import json
from pathlib import Path

import orchestration
import specialist_adapters
import watch_specialist_broker


def require(ok: bool, name: str) -> None:
    if not ok:
        raise AssertionError(name)


def main() -> None:
    require(
        specialist_adapters.EXPECTED_ENGINEERING_MODEL == "gpt-5.6-sol",
        "exact_model",
    )
    require(
        orchestration.EXPECTED_MODELS["codex"] == "gpt-5.6-sol",
        "wire_model",
    )
    require(
        specialist_adapters.SOL_KNOWLEDGE_SCOPE == "JAYTEC_SANITIZED_CORE_V4",
        "knowledge_scope",
    )

    manifest = json.loads(
        specialist_adapters.SOL_MEMORY_MANIFEST_PATH.read_text(encoding="utf-8")
    )
    require(
        manifest.get("schema_version") == "JAYTEC_GITHUB_MEMORY_V1",
        "memory_manifest_schema",
    )
    require(
        manifest.get("knowledge_scope") == specialist_adapters.SOL_KNOWLEDGE_SCOPE,
        "memory_manifest_scope",
    )
    require(
        bool(manifest.get("rules", {}).get("owner_sealed_backstory_excluded")),
        "sealed_backstory_rule",
    )

    context = specialist_adapters._sol_context_text()
    require(bool(context.strip()), "context_nonempty")
    lowered = context.casefold()
    compact = "".join(ch for ch in lowered if ch.isalnum())

    for marker in specialist_adapters.SOL_PROVENANCE_MARKERS:
        require(marker not in lowered, "context_marker:" + marker)
    for marker in specialist_adapters.SOL_PROVENANCE_COMPACT_MARKERS:
        require(marker not in compact, "context_compact_marker:" + marker)

    required_context_markers = [
        "Objective Persistence Under Constraints",
        "give it to WATCH",
        "one fenced Manus worker",
        "DeepSeek",
        "Nemo / Nemotron",
        "Proving Grounds",
        "G00-G37",
        "Owner/Jay interface",
        "Human Specialist interface",
        "Anchor / Mobile Specialist",
        "technical sovereignty",
        "Who is working right now",
        "Static GitHub memory is not live operational truth",
    ]
    for marker in required_context_markers:
        require(
            marker.casefold() in lowered,
            "missing_jaytec_context:" + marker,
        )

    general_history = [
        {"request": "Explain the God Mode to Forge naming transition."},
        {"request": "Review pre-Genesis gate sequencing."},
        {"request": "Review GENESIS_EVENT_0001 authority guards only."},
    ]
    for packet in general_history:
        require(
            not specialist_adapters._engineering_packet_provenance_violation(packet),
            "general_history_false_positive",
        )

    blocked = [
        {"request": "Explain Uren origin"},
        {"request": "Tell me how Uren was born"},
        {"request": "Reconstruct Uren creation history"},
        {"required_context": {"path": "/JAYTEC/Uren/Pre-Genesis/archive"}},
    ]
    for packet in blocked:
        require(
            specialist_adapters._engineering_packet_provenance_violation(packet),
            "sealed_backstory_probe_not_blocked",
        )

    source = Path("specialist_adapters.py").read_text(encoding="utf-8")
    staging = Path("staging_server.py").read_text(encoding="utf-8")
    broker = Path("watch_specialist_broker.py").read_text(encoding="utf-8")
    require("build_engineering_dispatch" in staging, "staging_engineering_route")
    require("ENGINEERING_PROVIDER_MODE" in staging, "provider_mode_gate")
    require("engineering_owner_provenance_blocked" in source, "provenance_gate")
    require("SOL_MEMORY_MANIFEST_PATH" in source, "github_memory_loader")
    require("ALLOWED_MANUS_MODEL_SPECIALISTS" in broker, "manus_specialist_broker")
    require(
        watch_specialist_broker.ALLOWED_MANUS_MODEL_SPECIALISTS
        == frozenset({"deepseek", "nemo"}),
        "manus_routine_pair",
    )
    require(
        watch_specialist_broker.CORE_TRIAD_EMERGENCY_TARGET == "core_triad",
        "core_triad_emergency_target",
    )
    require(
        "sol" not in watch_specialist_broker.ALLOWED_MANUS_MODEL_SPECIALISTS,
        "sol_not_routine_manus_specialist",
    )

    digest = hashlib.sha256(context.encode("utf-8")).hexdigest()
    print(
        "JAYTEC_SOL_PRIMARY_CERTIFICATION_V4 PASS "
        f"model=gpt-5.6-sol scope={specialist_adapters.SOL_KNOWLEDGE_SCOPE} "
        f"memory_files={len(manifest['ordered_files'])} "
        f"context_sha256={digest} provider_spend=NOT_PERFORMED"
    )


if __name__ == "__main__":
    main()
