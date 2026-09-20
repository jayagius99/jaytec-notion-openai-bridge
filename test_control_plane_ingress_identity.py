import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class ControlPlaneIngressIdentityTests(unittest.TestCase):
    def test_production_server_does_not_identify_notion_as_execution_principal(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('"sub": "notion-agent"', source)
        self.assertIn('EXPECTED_PRODUCTION_AUTH_SUBJECT = "jaytec-control-plane"', source)
        self.assertIn('EXPECTED_PRODUCTION_AUTH_CLIENT_ID = "jaytec-control-plane"', source)

    def test_notion_instructions_do_not_teach_retired_direct_tools(self):
        instructions = (ROOT / "NOTION_AGENT_INSTRUCTIONS.md").read_text(
            encoding="utf-8"
        )
        positive_directives = (
            "For difficult factual or technical questions, call `ask_openai`",
            "When you have already drafted an important answer, call `review_notion_answer`",
            "For complex project work where two-agent collaboration is useful, call `collaborate`",
        )
        for retired in positive_directives:
            with self.subTest(retired=retired):
                self.assertNotIn(retired, instructions)
        self.assertIn(
            "- call `ask_openai`, `review_notion_answer`, or free-form `collaborate`;",
            instructions,
        )
        self.assertIn("strict pass-through", instructions.casefold())
        self.assertIn("not an executor", instructions.casefold())

    def test_notion_bootstrap_file_forbids_production_execution_credentials(self):
        instructions = (ROOT / "GIVE_THIS_TO_NOTION_AI.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Never place provider API keys or JAYTEC production execution credentials in Notion.",
            instructions,
        )

    def test_readme_scopes_bridge_readiness_below_system_readiness(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("this bridge instance only", readme)
        self.assertIn("whole JAYTEC/GOD Mode system", readme)


if __name__ == "__main__":
    unittest.main()
