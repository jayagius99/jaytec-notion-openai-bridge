from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
SERVER = (ROOT / "staging_server.py").read_text(encoding="utf-8")
RENDER = (ROOT / "render.yaml").read_text(encoding="utf-8")


class WatchSpecialistProviderIsolationTests(unittest.TestCase):
    def test_gemini_door_stays_locked_in_render(self):
        self.assertIn(
            "- key: OPENROUTER_PROVIDER_MODE\n        value: LOCKED_RESERVE",
            RENDER,
        )

    def test_nemo_free_route_is_explicitly_active(self):
        self.assertIn(
            "- key: NEMO_PROVIDER_MODE\n        value: ACTIVE",
            RENDER,
        )
        self.assertIn(
            'EXPECTED_NEMO_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"',
            (ROOT / "nemo_specialist.py").read_text(encoding="utf-8"),
        )

    def test_deepseek_remains_explicitly_locked_without_spend_authority(self):
        self.assertIn(
            "- key: DEEPSEEK_PROVIDER_MODE\n        value: LOCKED_RESERVE",
            RENDER,
        )

    def test_watch_specialist_openrouter_transport_is_separate_from_gemini(self):
        self.assertIn("WATCH_SPECIALIST_OPENROUTER_CLIENT = (", SERVER)
        self.assertIn("openrouter_client=WATCH_SPECIALIST_OPENROUTER_CLIENT", SERVER)
        self.assertIn("openrouter_client=OPENROUTER_CLIENT", SERVER)
        self.assertIn('"watch_specialist_team": ["sol", "deepseek", "nemo"]', SERVER)

    def test_specialist_client_requires_named_specialist_activation(self):
        block = SERVER.split("WATCH_SPECIALIST_OPENROUTER_CLIENT = (", 1)[1]
        block = block.split("# Build dispatchers ONCE", 1)[0]
        self.assertIn('DEEPSEEK_PROVIDER_MODE == "ACTIVE"', block)
        self.assertIn('NEMO_PROVIDER_MODE == "ACTIVE"', block)
        self.assertNotIn("OPENROUTER_PROVIDER_MODE == OPENROUTER_PROVIDER_ACTIVE", block)

    def test_nemo_and_deepseek_dispatches_do_not_depend_on_gemini_client(self):
        deep = SERVER.split("DEEPSEEK_REVIEW_DISPATCH = (", 1)[1]
        deep = deep.split("NEMO_DISPATCH = (", 1)[0]
        nemo = SERVER.split("NEMO_DISPATCH = (", 1)[1]
        nemo = nemo.split("def _watch_specialist_runner", 1)[0]
        self.assertIn("WATCH_SPECIALIST_OPENROUTER_CLIENT", deep)
        self.assertIn("WATCH_SPECIALIST_OPENROUTER_CLIENT", nemo)
        self.assertNotIn("if OPENROUTER_CLIENT", deep)
        self.assertNotIn("if OPENROUTER_CLIENT", nemo)


if __name__ == "__main__":
    unittest.main()
