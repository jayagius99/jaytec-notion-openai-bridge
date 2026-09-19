import unittest

from participant_contracts import (
    CONTRACT_VERSION,
    actor_contract_sha256,
    render_actor_contract,
)
from relationship_policy import Actor


class ParticipantContractTests(unittest.TestCase):
    def test_all_operational_actors_have_explicit_contracts(self):
        for actor in (
            Actor.CHATGPT,
            Actor.JAYTEC,
            Actor.MANUS,
            Actor.GEMINI,
            Actor.ENGINEERING,
            Actor.NOTION,
            Actor.GITHUB,
            Actor.NEON,
            Actor.RENDER,
        ):
            with self.subTest(actor=actor):
                text = render_actor_contract(actor)
                self.assertIn(CONTRACT_VERSION, text)
                self.assertEqual(len(actor_contract_sha256(actor)), 64)

    def test_manus_understands_boundary(self):
        text = render_actor_contract(Actor.MANUS)
        normalized = " ".join(text.split())
        self.assertIn("Improve Manus-side workflows", normalized)
        self.assertIn("Never independently change JAYTEC", normalized)
        self.assertIn("inherit implicit connectors", normalized)

    def test_notion_is_strict_agent_transport_only(self):
        text = render_actor_contract(Actor.NOTION)
        self.assertIn("NOTION AGENT — STRICT PASS-THROUGH TRANSPORT ONLY", text)
        self.assertIn("not JAYTEC", text)
        self.assertIn("Do not rewrite, expand, research, solve", text)
        self.assertIn("exact failure evidence", text)

    def test_chatgpt_contract_never_substitutes_notion_for_jaytec(self):
        text = render_actor_contract(Actor.CHATGPT)
        self.assertIn("JAYTEC ALWAYS means the JAYTEC system/control plane", text)
        self.assertIn("Never substitute Notion Agent", text)

    def test_chatgpt_retains_final_coordination_authority(self):
        text = render_actor_contract(Actor.CHATGPT)
        self.assertIn("ChatGPT alone accepts/rejects", text)
        self.assertIn("assigns resulting work", text)

    def test_specialist_planning_input_is_advisory_only(self):
        gemini = render_actor_contract(Actor.GEMINI)
        engineering = render_actor_contract(Actor.ENGINEERING)
        self.assertIn("advisory only", gemini)
        self.assertIn("do not create/assign tasks", gemini)
        self.assertIn("ChatGPT remains final coordinator", engineering)
        self.assertIn("Do not self-assign", engineering)

    def test_specialists_cannot_delegate(self):
        for actor in (Actor.GEMINI, Actor.ENGINEERING):
            text = render_actor_contract(actor)
            self.assertIn("delegate", text.casefold())
            self.assertIn("JAYTEC", text)

    def test_unknown_actor_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "JAYTEC_ACTOR_UNKNOWN"):
            render_actor_contract("mystery")


if __name__ == "__main__":
    unittest.main()
