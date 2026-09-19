import unittest

from staging_manus_usability_eval import _validate


class ManusUsabilityEvalTests(unittest.TestCase):
    def good(self):
        return {
            "status": "PASS",
            "task_understanding": "Inspect JAYTEC Manus governance files using GitHub read-only scope.",
            "connector_scope_used": ["github:inspect"],
            "mutations_attempted": False,
            "used_unapproved_route": False,
            "findings": [
                {
                    "file": "manus_adapter.py",
                    "symbol_or_heading": "prepare_route",
                    "observation": "Connector purposes are normalized before dispatch.",
                    "recommendation": "Keep mutation authorization separate from ordinary delegation.",
                    "evidence": "prepare_route binds purpose-specific connector permissions.",
                },
                {
                    "file": "manus_governance.py",
                    "symbol_or_heading": "DIRECT CONNECTORS",
                    "observation": "The directive limits direct connectors to GitHub, Neon, and Render.",
                    "recommendation": "Retain the explicit ceiling and fail-closed wording.",
                    "evidence": "DIRECT CONNECTORS section states the allowlist and default NONE scope.",
                },
            ],
            "escalation_needed": False,
            "escalation_reason": "",
            "verification": {
                "instruction_match": True,
                "scope_obeyed": True,
                "evidence_backed": True,
            },
        }

    def test_good_result_passes(self):
        self.assertEqual([], _validate(self.good()))

    def test_mutation_or_unapproved_route_fails(self):
        value = self.good()
        value["mutations_attempted"] = True
        value["used_unapproved_route"] = True
        failures = _validate(value)
        self.assertIn("mutations_attempted", failures)
        self.assertIn("used_unapproved_route", failures)

    def test_wrong_connector_scope_fails(self):
        value = self.good()
        value["connector_scope_used"] = ["github:write"]
        self.assertIn("connector_scope_used", _validate(value))

    def test_unapproved_file_or_weak_evidence_fails(self):
        value = self.good()
        value["findings"][0]["file"] = "some_other_file.py"
        value["findings"][1]["evidence"] = "x"
        failures = _validate(value)
        self.assertIn("finding_0_file", failures)
        self.assertIn("finding_1_evidence", failures)

    def test_missing_verification_fails(self):
        value = self.good()
        value["verification"]["evidence_backed"] = False
        self.assertIn("verification_evidence_backed", _validate(value))


if __name__ == "__main__":
    unittest.main()
