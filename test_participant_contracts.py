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
        self.assertIn("Improve Manus-side workflows", text)
        self.assertIn("Never independently change JAYTEC", text)
        self.assertIn("inherit implicit connectors", text)

    def test_notion_is_transfer_only(self):
        text = render_actor_contract(Actor.NOTION)
        self.assertIn("TRANSFER GATEWAY ONLY", text)
        self.assertIn("Do not research, execute, repair, analyze", text)

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
