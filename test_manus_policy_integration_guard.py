import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXCLUDED = {
    "manus_policy.py",
    "manus_governance.py",
    "test_manus_policy.py",
    "test_manus_governance.py",
    "test_manus_policy_integration_guard.py",
}
ROUTING_MARKERS = re.compile(r"\b(dispatch|adapter|client|provider|profile|task\.create|task\.sendMessage)\b", re.I)


class ManusPolicyIntegrationGuardTests(unittest.TestCase):
    def test_any_manus_routing_code_must_use_profile_and_governance_guards(self):
        violations = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name in EXCLUDED or path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8")
            lowered = source.casefold()
            if "manus" not in lowered or not ROUTING_MARKERS.search(source):
                continue

            if "manus_policy" not in lowered:
                violations.append(f"{path.name}:missing_manus_policy_import")
            if "authorize_manus_route" not in source:
                violations.append(f"{path.name}:missing_pre_dispatch_profile_authorization")
            if "verify_manus_profile" not in source:
                violations.append(f"{path.name}:missing_post_dispatch_profile_verification")

            if "manus_governance" not in lowered:
                violations.append(f"{path.name}:missing_manus_governance_import")
            if "authorize_manus_action" not in source:
                violations.append(f"{path.name}:missing_behaviour_authorization")
            if "assert_approved_manus_connectors" not in source:
                violations.append(f"{path.name}:missing_connector_allowlist_guard")

        self.assertEqual(
            violations,
            [],
            "Manus routing bypasses fail-closed profile/governance policy: "
            + ", ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
