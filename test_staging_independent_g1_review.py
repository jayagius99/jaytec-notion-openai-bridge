import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "staging_independent_g1_review.py"


def _load():
    spec = importlib.util.spec_from_file_location("g1_independent_review_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestIndependentG1Review(unittest.TestCase):
    def test_packet_is_bounded_and_secret_safe(self):
        module = _load()
        self.assertIn("nvidia/nemotron-3-ultra-550b-a55b:free", module.ALLOWED_MODELS)
        self.assertIn("google/gemini-2.0-flash-exp:free", module.ALLOWED_MODELS)
        ok = module._validate_packet({"proofs": {"P01": "PASS"}, "evidence": ["hash-only"]})
        self.assertEqual("PASS", ok["proofs"]["P01"])
        with self.assertRaisesRegex(ValueError, "FORBIDDEN_KEY"):
            module._validate_packet({"api_key": "never"})
        with self.assertRaisesRegex(ValueError, "TOO_LARGE"):
            module._validate_packet({"evidence": "x" * (module.MAX_PACKET_CHARS + 1)})

    def test_only_zero_cost_review_models_are_allowlisted(self):
        module = _load()
        self.assertEqual(
            {
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "google/gemini-2.0-flash-exp:free",
            },
            module.ALLOWED_MODELS,
        )

    def test_disabled_by_default_and_has_no_tools(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('JAYTEC_G1_INDEPENDENT_REVIEW_ENABLED", "0"', text)
        self.assertIn("allow_fallbacks", text)
        self.assertIn("False", text)
        self.assertIn("max_retries=0", text)
        self.assertNotIn("tools=", text)
        self.assertNotIn("OPENAI_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
