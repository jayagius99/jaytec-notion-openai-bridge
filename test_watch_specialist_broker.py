from __future__ import annotations

import json
import unittest

from manus_governance import specialist_request
from watch_specialist_broker import (
    ALLOWED_MANUS_MODEL_SPECIALISTS,
    CORE_TRIAD_EMERGENCY_TARGET,
    SpecialistBrokerError,
    build_dispatch_packet,
    dispatch_manus_model_requests,
    normalize_manus_model_request,
)


PARENT = "FORGE-GENESIS-ACTIVATION-001:recovery:14:test"


def req(specialist: str, objective: str = "Review this bounded item."):
    return specialist_request(
        parent_task_id=PARENT,
        specialist=specialist,
        objective=objective,
        reason="Manus needs bounded JAYTEC specialist assistance.",
        required_context={"gate_id": "G03", "artifact_sha256": "a" * 64},
    )


def success(model: str):
    return {
        "status": "SUCCESS",
        "model": model,
        "findings": ["bounded finding"],
        "evidence": ["bounded evidence"],
        "confidence": "high",
        "conclusion": {"ok": True},
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "requested_operations": [],
    }


class WatchSpecialistBrokerTests(unittest.TestCase):
    def test_manus_routine_specialists_are_exactly_two(self):
        self.assertEqual(ALLOWED_MANUS_MODEL_SPECIALISTS, {"deepseek", "nemo"})
        self.assertEqual(CORE_TRIAD_EMERGENCY_TARGET, "core_triad")

    def test_normalizes_each_routine_specialist(self):
        for name in ("deepseek", "nemo"):
            normalized = normalize_manus_model_request(
                req(name),
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
            )
            self.assertEqual(normalized["specialist"], name)
            self.assertEqual(normalized["parent_task_id"], PARENT)

    def test_gemini_is_not_implicit_watch_specialist(self):
        with self.assertRaisesRegex(SpecialistBrokerError, "SPECIALIST_NOT_ALLOWLISTED"):
            normalize_manus_model_request(
                req("gemini"),
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
            )

    def test_sealed_origin_request_fails_closed(self):
        with self.assertRaisesRegex(SpecialistBrokerError, "SEALED_PROVENANCE"):
            normalize_manus_model_request(
                req("deepseek", "Explain Uren origin."),
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
            )

    def test_secret_like_context_fails_closed(self):
        packet = specialist_request(
            parent_task_id=PARENT,
            specialist="nemo",
            objective="Review bounded code.",
            reason="Need second opinion.",
            required_context={"api_key": "sk-should-never-route"},
        )
        with self.assertRaisesRegex(SpecialistBrokerError, "SECRET_MATERIAL"):
            normalize_manus_model_request(
                packet,
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
            )

    def test_dispatch_packet_keeps_specialist_subordinate(self):
        normalized = normalize_manus_model_request(
            req("deepseek"),
            parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
        )
        packet = build_dispatch_packet(normalized)
        self.assertEqual(packet["allowed_operations"], [])
        self.assertEqual(packet["required_context"]["authority_controller"], "CHATGPT_OPENAI_LEAD")
        self.assertEqual(packet["required_context"]["specialist_authority"], "SUBORDINATE")
        self.assertEqual(packet["required_context"]["return_to"], "SAME_MANUS_WORKER")

    def test_two_specialists_round_trip_as_one_correlated_package(self):
        requests = [
            normalize_manus_model_request(req("deepseek", "Attack this."),
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001"),
            normalize_manus_model_request(req("nemo", "Second review."),
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001"),
        ]
        models = {
            "deepseek": "deepseek/deepseek-v4-flash-0731:free",
            "nemo": "nvidia/nemotron-3-ultra-550b-a55b:free",
        }
        seen = []
        dispatchers = {}
        for name, model in models.items():
            def make_dispatch(n=name, m=model):
                def _dispatch(packet):
                    seen.append((n, packet["subtask_id"], packet["allowed_operations"]))
                    return success(m)
                return _dispatch
            dispatchers[name] = make_dispatch()

        package = dispatch_manus_model_requests(requests, dispatchers=dispatchers)
        rows = package["request_results"]
        self.assertEqual([r["specialist"] for r in rows], ["deepseek", "nemo"])
        self.assertEqual([r["request_id"] for r in rows], [r["request_id"] for r in requests])
        self.assertEqual([r[0] for r in seen], ["deepseek", "nemo"])
        self.assertTrue(all(r[2] == [] for r in seen))
        self.assertEqual(package["authority"], "RESULTS_ONLY_NO_DISPATCH_AUTHORITY")
        self.assertEqual(len(package["sha256"]), 64)
        self.assertLess(len(json.dumps(package).encode("utf-8")), 6000)


    def test_core_triad_requires_explicit_emergency_flag(self):
        packet = specialist_request(
            parent_task_id=PARENT,
            specialist="core_triad",
            objective="Request emergency controller help.",
            reason="Bounded emergency.",
            required_context={"gate_id": "G03"},
        )
        with self.assertRaisesRegex(
            SpecialistBrokerError, "CORE_TRIAD_EMERGENCY_FLAG_REQUIRED"
        ):
            normalize_manus_model_request(
                packet,
                parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
            )

    def test_core_triad_emergency_never_dispatches_a_model_directly(self):
        packet = specialist_request(
            parent_task_id=PARENT,
            specialist="core_triad",
            objective="Request emergency controller help.",
            reason="Bounded emergency.",
            required_context={"gate_id": "G03", "emergency": True},
        )
        normalized = normalize_manus_model_request(
            packet,
            parent_task_prefix="FORGE-GENESIS-ACTIVATION-001",
        )

        def forbidden(_packet):
            raise AssertionError("Core Triad must not be called as a direct model specialist")

        package = dispatch_manus_model_requests(
            [normalized],
            dispatchers={"core_triad": forbidden},
        )
        row = package["request_results"][0]
        self.assertEqual(row["specialist"], "core_triad")
        self.assertEqual(row["status"], "NEEDS_JAYTEC_CORE_TRIAD")
        self.assertIsNone(row["model"])

    def test_specialist_side_effect_claim_is_rejected(self):
        request = normalize_manus_model_request(
            req("nemo"), parent_task_prefix="FORGE-GENESIS-ACTIVATION-001"
        )
        bad = success("nvidia/nemotron-3-ultra-550b-a55b:free")
        bad["side_effects_attempted"] = ["write repo"]
        with self.assertRaisesRegex(SpecialistBrokerError, "SIDE_EFFECT"):
            dispatch_manus_model_requests(
                [request], dispatchers={"nemo": lambda _packet: bad}
            )

    def test_provider_failure_becomes_bounded_failed_closed_result(self):
        request = normalize_manus_model_request(
            req("deepseek"), parent_task_prefix="FORGE-GENESIS-ACTIVATION-001"
        )

        def boom(_packet):
            raise RuntimeError("provider detail must not escape")

        package = dispatch_manus_model_requests([request], dispatchers={"deepseek": boom})
        row = package["request_results"][0]
        self.assertEqual(row["status"], "FAILED_CLOSED")
        text = json.dumps(row)
        self.assertNotIn("provider detail must not escape", text)
        self.assertIn("RuntimeError", text)


if __name__ == "__main__":
    unittest.main()
