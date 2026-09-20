import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXCLUDED = {
    "manus_policy.py",
    "manus_governance.py",
    "manus_dispatch_contract.py",
    "relationship_policy.py",
    "participant_contracts.py",
    "test_manus_policy.py",
    "test_manus_governance.py",
    "test_manus_dispatch_contract.py",
    "test_relationship_policy.py",
    "test_manus_policy_integration_guard.py",
}


class ManusPolicyIntegrationGuardTests(unittest.TestCase):
    def test_direct_manus_provider_routing_must_use_composed_contract(self):
        """Only files that directly hit Manus task execution endpoints need this guard.

        Review/readback scripts may mention Manus but cannot bypass the adapter
        merely by mentioning a client/provider/profile word.
        """
        violations = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name in EXCLUDED or path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8")
            lowered = source.casefold()
            if "task.create" not in lowered and "task.sendmessage" not in lowered:
                continue

            if "manus_dispatch_contract" not in lowered:
                violations.append(
                    f"{path.name}:missing_manus_dispatch_contract_import"
                )
                continue
            if "authorize_manus_dispatch" not in source:
                violations.append(
                    f"{path.name}:missing_composed_pre_dispatch_authorization"
                )
            if "verify_manus_dispatch_result" not in source:
                violations.append(
                    f"{path.name}:missing_composed_post_dispatch_verification"
                )

        self.assertEqual(
            violations,
            [],
            "Direct Manus provider route bypasses composed JAYTEC contract: "
            + ", ".join(violations),
        )

    def test_no_operational_source_can_enable_paid_manus_override(self):
        violations = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name.startswith("test_") or path.name == "manus_policy.py":
                continue
            source = path.read_text(encoding="utf-8")
            compact = source.replace(" ", "").replace("\n", "")
            if "explicit_paid_override=True" in compact:
                violations.append(path.name)
        self.assertEqual(
            violations,
            [],
            "Operational source contains a paid Manus override: "
            + ", ".join(violations),
        )

    def test_adapter_checks_lite_policy_before_any_manus_discovery_read(self):
        source = (ROOT / "manus_adapter.py").read_text(encoding="utf-8")
        prepare = source.index("def prepare_route(")
        policy = source.index("authorize_manus_route(", prepare)
        project = source.index("self.resolve_manus_project()", prepare)
        self.assertLess(
            policy,
            project,
            "Manus profile policy must fail closed before provider discovery reads",
        )

    def test_command_policy_marks_unpinnable_manus_surfaces_unavailable(self):
        source = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn("Lite-only, permanently and without exception", source)
        self.assertIn(
            "does not expose a Lite\n  selector and observable profile verification",
            source,
        )

    def test_no_stale_paid_profile_exception_in_live_manus_material(self):
        forbidden = (
            "unless Jay explicitly authorizes a paid profile",
            "paid profiles remain one-task-only exceptions",
            "paid-profile override is one-task-only",
        )
        paths = (
            "JAYTEC_COMMAND_POLICY.md",
            "MANUS_OPERATING_DIRECTIVE.md",
            "manus_governance.py",
            "staging_manus_governance_acceptance.py",
            "staging_gemini_manus_governance_review.py",
        )
        violations = []
        for name in paths:
            source = (ROOT / name).read_text(encoding="utf-8")
            for phrase in forbidden:
                if phrase.casefold() in source.casefold():
                    violations.append(f"{name}:{phrase}")
        self.assertEqual(
            violations,
            [],
            "Stale paid Manus exception survived: " + ", ".join(violations),
        )


    def test_manus_home_is_consistently_subordinate_to_jaytec(self):
        sources = {
            "runtime": (ROOT / "manus_governance.py").read_text(encoding="utf-8"),
            "operator": (ROOT / "MANUS_OPERATING_DIRECTIVE.md").read_text(encoding="utf-8"),
            "policy": (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8"),
        }
        required = (
            "Manus Home",
            "JAYTEC",
            "spare-capacity",
        )
        for name, source in sources.items():
            with self.subTest(source=name):
                for phrase in required:
                    self.assertIn(phrase, source)
        self.assertIn(
            "JAYTEC / active owner-authorized work comes first",
            sources["runtime"],
        )
        self.assertIn(
            "JAYTEC and current owner-authorized objectives come first",
            sources["operator"],
        )
        self.assertIn(
            "JAYTEC and the current owner-authorized objective come first",
            sources["policy"],
        )

    def test_live_acceptance_uses_canonical_provenance_evidence(self):
        source = (ROOT / "staging_manus_governance_acceptance.py").read_text(
            encoding="utf-8"
        )
        for field in (
            '"kind": "provider_observation"',
            '"source": "Manus v2 task detail"',
            '"observed_at": observed_at',
            '"supports": [',
        ):
            self.assertIn(field, source)
        self.assertNotIn(
            '"evidence": [\n                "structured governance acknowledgement validated"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
