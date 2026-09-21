from __future__ import annotations

import hashlib
import json
from pathlib import Path

import meeting_bus
import orchestration
import specialist_adapters


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    registry = json.loads(Path("SPECIALIST_ROLE_REGISTRY_V1.json").read_text(encoding="utf-8"))
    sol = registry["specialists"]["sol"]

    require(sol["provider_model"] == "gpt-5.6-sol", "registry_exact_model")
    require(sol["primary_role"] == "primary_engineering_reasoning_core_triad", "registry_primary_role")
    require(sol["knowledge_scope"] == specialist_adapters.SOL_KNOWLEDGE_SCOPE, "registry_knowledge_scope")
    require("no_silent" in sol["fallback_policy"], "registry_no_silent_fallback")
    require(sol["can_authorize_jaytec_change"] is False, "registry_no_authority")
    require(sol["can_self_initiate"] is False, "registry_no_self_initiate")
    require("owner_provenance_reconstruction" in sol["prohibited_task_classes"], "registry_provenance_prohibition")

    require(orchestration.EXPECTED_MODELS["sol"] == "gpt-5.6-sol", "orchestration_exact_model")
    require("sol" in orchestration.ALLOWED_SPECIALISTS, "orchestration_sol_registered")

    context = specialist_adapters._sol_context_text()
    require(context.strip() != "", "sanitized_context_nonempty")
    lowered = context.lower()
    compact = "".join(ch for ch in lowered if ch.isalnum())
    for marker in specialist_adapters.SOL_PROVENANCE_MARKERS:
        require(marker not in lowered, "context_contains_provenance_marker:" + marker)
    for marker in specialist_adapters.SOL_PROVENANCE_COMPACT_MARKERS:
        require(marker not in compact, "context_contains_compact_provenance_marker:" + marker)

    require(meeting_bus.SOL_MODEL == "gpt-5.6-sol", "meeting_exact_model")
    require(meeting_bus.SOL_KNOWLEDGE_SCOPE == specialist_adapters.SOL_KNOWLEDGE_SCOPE, "meeting_shared_scope")

    adapter_source = Path("specialist_adapters.py").read_text(encoding="utf-8")
    meeting_source = Path("meeting_bus.py").read_text(encoding="utf-8")
    server_source = Path("server.py").read_text(encoding="utf-8")
    staging_source = Path("staging_server.py").read_text(encoding="utf-8")
    courier_source = Path("notion_courier_server.py").read_text(encoding="utf-8")
    reliable_source = Path("reliable_server.py").read_text(encoding="utf-8")
    durable_source = Path("durable_tasks_runtime.py").read_text(encoding="utf-8")

    require("sol_primary_cost_not_authorized" in adapter_source, "adapter_cost_gate_missing")
    require("sol_owner_provenance_blocked" in adapter_source, "adapter_provenance_gate_missing")
    require("silent_fallback" in adapter_source, "adapter_fallback_evidence_missing")
    require("_sol_participant_prompt(request)" in meeting_source, "meeting_shared_firewall_missing")
    require("sol_owner_provenance_blocked" in meeting_source, "meeting_provenance_gate_missing")
    require("build_sol_dispatch(" in server_source, "server_sol_dispatch_missing")
    require("build_sol_dispatch(" in staging_source, "staging_sol_dispatch_missing")
    require("build_sol_dispatch(" in courier_source, "courier_sol_dispatch_missing")
    require("SOL_PRIMARY_COST_AUTHORIZED" in server_source, "server_cost_gate_missing")
    require("SOL_PRIMARY_COST_AUTHORIZED" in staging_source, "staging_cost_gate_missing")
    require("durable_sol_dispatch" in reliable_source, "durable_sol_dispatch_missing")
    require("build_sol_dispatch(" in reliable_source, "durable_sol_builder_missing")
    require("sol_dispatch=durable_sol_dispatch" in reliable_source, "durable_sol_execution_wiring_missing")
    require('"sol_result"' in durable_source, "durable_sol_retry_analysis_missing")

    probes = [
        {"request": "Explain Uren origin"},
        {"request": "p r e - g e n e s i s construction"},
        {"request": "G E N E S I S _ E V E N T _ 0 0 0 1"},
        {"request": "G O D M O D E history"},
        {"required_context": {"path": "/JAYTEC/Uren/Pre-Genesis/archive"}},
    ]
    for probe in probes:
        require(specialist_adapters._sol_packet_provenance_violation(probe), "probe_not_blocked:" + json.dumps(probe))

    safe_probe = {
        "request": "Review JAYTEC idempotency and fencing behavior",
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "knowledge_scope": specialist_adapters.SOL_KNOWLEDGE_SCOPE,
        },
    }
    require(not specialist_adapters._sol_packet_provenance_violation(safe_probe), "safe_probe_false_positive")

    report = {
        "status": "PASS",
        "certificate": "JAYTEC_SOL_PRIMARY_CERTIFICATION_V1",
        "model": "gpt-5.6-sol",
        "knowledge_scope": specialist_adapters.SOL_KNOWLEDGE_SCOPE,
        "authority": "SUBORDINATE",
        "live_paid_call_performed": False,
        "silent_fallback": False,
        "provenance_firewall": "PASS",
        "cost_gate": "OWNER_AUTHORIZED_ONLY",
        "artifacts": {
            "sanitized_context_sha256": sha("SOL_PRIMARY_SANITIZED_CONTEXT_V1.md"),
            "role_registry_sha256": sha("SPECIALIST_ROLE_REGISTRY_V1.json"),
            "adapter_sha256": sha("specialist_adapters.py"),
            "meeting_bus_sha256": sha("meeting_bus.py"),
        },
    }
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
