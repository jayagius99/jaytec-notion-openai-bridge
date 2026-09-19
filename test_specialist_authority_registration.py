import re
import unittest
from pathlib import Path

import meeting_bus
import specialist_adapters


class SpecialistAuthorityRegistrationGuardTests(unittest.TestCase):
    def test_global_authority_contract_is_embedded_in_current_provider_specialists(self):
        contract = specialist_adapters.SPECIALIST_AUTHORITY_CONTRACT
        self.assertIn("Jay is owner/root authority", contract)
        self.assertIn("ChatGPT/OpenAI Lead is the sole JAYTEC coordinator/controller", contract)
        self.assertIn("subordinate specialist", contract)
        self.assertIn("Do not self-initiate JAYTEC work", contract)
        self.assertIn("ChatGPT decides whether anything is applied", contract)
        self.assertTrue(specialist_adapters.CODEX_CONTRACT.startswith(contract))
        self.assertTrue(
            specialist_adapters.GEMINI_RESEARCH_MODE_V1_1.startswith(contract)
        )

    def test_new_provider_dispatch_adapter_requires_explicit_registration_review(self):
        source = Path("specialist_adapters.py").read_text(encoding="utf-8")
        dispatchers = set(
            re.findall(r"^def (build_[a-z0-9_]+_dispatch)\(", source, re.MULTILINE)
        )
        # Intentional tripwire: adding another provider-backed specialist must
        # update this guard after its authority/cost/model/side-effect contract
        # has been explicitly reviewed.
        self.assertEqual(
            dispatchers,
            {"build_codex_dispatch", "build_gemini_dispatch"},
        )

    def test_new_meeting_participant_requires_explicit_registration_review(self):
        # Intentional tripwire for future meeting specialists.
        self.assertEqual(meeting_bus.ALLOWED_PARTICIPANTS, {"gemini", "sol"})

    def test_meeting_prompt_keeps_specialists_subordinate(self):
        request = {
            "meeting_id": "MEETING-TEST-001",
            "participant": "sol",
            "allowed_operations": ["analyze"],
            "brief": "Review only.",
            "role_question": "Challenge one assumption.",
        }
        prompt = meeting_bus._participant_prompt(request)
        self.assertIn("ChatGPT/OpenAI Lead is the sole", prompt)
        self.assertIn("subordinate", prompt)
        self.assertIn("Do not self-initiate JAYTEC work", prompt)
        self.assertIn("Return advice/evidence only to ChatGPT", prompt)

    def test_durable_governance_document_exists(self):
        text = Path("SPECIALIST_AUTHORITY_CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("Applies to: Sol, Gemini, Manus, and every current or future", text)
        self.assertIn("Future specialist registration gate", text)
        self.assertIn("Missing any item = specialist registration fails closed", text)


if __name__ == "__main__":
    unittest.main()
