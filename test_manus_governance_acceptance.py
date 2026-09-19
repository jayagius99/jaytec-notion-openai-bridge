import unittest

from staging_manus_governance_acceptance import _validate


class ManusLiveAcceptanceValidatorTests(unittest.TestCase):
    def good(self):
        return {
            "status": "PASS",
            "role": "bounded automation specialist",
            "authority_chain": "Jay -> ChatGPT -> JAYTEC -> Manus / specialists",
            "self_improvement_scope": "own JAYTEC-controlled Manus house",
            "can_self_modify_jaytec": False,
            "direct_connectors": ["github", "neon", "render"],
            "may_use_notion_directly": False,
            "may_call_openai_openrouter_directly": False,
            "specialist_requests_return_to": "JAYTEC",
            "requires_current_authority_for_mutations": True,
            "lite_only": True,
            "hidden_fallbacks_allowed": False,
            "success_requires_evidence": True,
            "unclear_action_behavior": "escalate to JAYTEC or fail closed",
        }

    def test_expected_ack_passes(self):
        self.assertEqual(_validate(self.good()), [])

    def test_each_security_boolean_flip_is_detected(self):
        fields = {
            "can_self_modify_jaytec": True,
            "may_use_notion_directly": True,
            "may_call_openai_openrouter_directly": True,
            "requires_current_authority_for_mutations": False,
            "lite_only": False,
            "hidden_fallbacks_allowed": True,
            "success_requires_evidence": False,
        }
        for field, bad in fields.items():
            with self.subTest(field=field):
                value = self.good()
                value[field] = bad
                self.assertIn(field, _validate(value))

    def test_connector_drift_is_detected(self):
        for connectors in (
            ["github", "neon", "render", "notion"],
            ["github", "render"],
            ["notion"],
            [],
        ):
            with self.subTest(connectors=connectors):
                value = self.good()
                value["direct_connectors"] = connectors
                self.assertIn("direct_connectors", _validate(value))

    def test_wrong_escalation_target_is_detected(self):
        value = self.good()
        value["specialist_requests_return_to"] = "Notion"
        self.assertIn("specialist_requests_return_to", _validate(value))


if __name__ == "__main__":
    unittest.main()
