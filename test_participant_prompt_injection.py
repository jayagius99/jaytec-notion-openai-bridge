import inspect
import unittest

import specialist_adapters
from participant_contracts import render_actor_contract
from relationship_policy import Actor


class ParticipantPromptInjectionTests(unittest.TestCase):
    def test_gemini_dispatch_injects_role_contract(self):
        source = inspect.getsource(specialist_adapters.build_gemini_dispatch)
        self.assertIn("render_actor_contract(Actor.GEMINI)", source)
        self.assertIn("RESEARCH / REVIEW SPECIALIST", render_actor_contract(Actor.GEMINI))

    def test_engineering_dispatch_injects_role_contract(self):
        source = inspect.getsource(specialist_adapters.build_codex_dispatch)
        self.assertIn("render_actor_contract(Actor.ENGINEERING)", source)
        self.assertIn("ENGINEERING SPECIALIST", render_actor_contract(Actor.ENGINEERING))


if __name__ == "__main__":
    unittest.main()
