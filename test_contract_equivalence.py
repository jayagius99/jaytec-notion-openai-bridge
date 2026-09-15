import unittest

from specialist_adapters import CODEX_CONTRACT, GEMINI_RESEARCH_MODE_V1_1


class TestContractEquivalence(unittest.TestCase):
    def test_contract_strings_are_nonempty_and_stable(self):
        self.assertIn("Return ONLY one JSON object", CODEX_CONTRACT)
        self.assertIn("JAYTEC_GEMINI_RESEARCH_MODE v1.1.0", GEMINI_RESEARCH_MODE_V1_1)


if __name__ == "__main__":
    unittest.main()
