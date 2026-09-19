import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXCLUDED = {
    "manus_policy.py",
    "test_manus_policy.py",
    "test_manus_policy_integration_guard.py",
}
ROUTING_MARKERS = re.compile(r"\b(dispatch|adapter|client|provider|profile)\b", re.I)


class ManusPolicyIntegrationGuardTests(unittest.TestCase):
    def test_any_manus_routing_code_must_use_fail_closed_guard(self):
        violations = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name in EXCLUDED or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            lowered = text.casefold()
            if "manus" not in lowered or not ROUTING_MARKERS.search(text):
                continue

            if "manus_policy" not in lowered:
                violations.append(f"{path.name}:missing_manus_policy_import")
                continue
            if "authorize_manus_route" not in text:
                violations.append(f"{path.name}:missing_pre_dispatch_authorization")
            if "verify_manus_profile" not in text:
                violations.append(f"{path.name}:missing_post_dispatch_profile_verification")

        self.assertEqual(
            violations,
            [],
            "Manus routing bypasses fail-closed Lite policy: " + ", ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
