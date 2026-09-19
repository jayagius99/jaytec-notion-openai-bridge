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


if __name__ == "__main__":
    unittest.main()
