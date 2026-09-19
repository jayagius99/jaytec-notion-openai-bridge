import unittest

from staging_gemini_coordination_review import _blocked


class GeminiCoordinationReviewGateTests(unittest.TestCase):
    def good(self):
        return {
            "status": "SUCCESS",
            "side_effects_attempted": [],
            "conclusion": {
                "verdict": "ADVISORY_READY",
                "recommended_parallel_lanes": ["lane-a", "lane-b"],
                "dependency_order": ["a before c"],
                "specialist_assignment_suggestions": ["gemini: review", "sol: engineering"],
                "do_not_duplicate": ["existing P11 run"],
                "risk_flags": [],
                "fastest_safe_sequence": ["parallelize a/b", "join", "validate"],
                "chatgpt_decisions_required": ["accept or reject lane plan"],
            },
        }

    def test_valid_advisory_passes(self):
        self.assertFalse(_blocked(self.good()))

    def test_side_effects_block(self):
        value = self.good()
        value["side_effects_attempted"] = ["write"]
        self.assertTrue(_blocked(value))

    def test_missing_required_conclusion_key_blocks(self):
        value = self.good()
        del value["conclusion"]["chatgpt_decisions_required"]
        self.assertTrue(_blocked(value))

    def test_extra_conclusion_key_blocks(self):
        value = self.good()
        value["conclusion"]["assign_now"] = True
        self.assertTrue(_blocked(value))

    def test_non_list_advisory_field_blocks(self):
        value = self.good()
        value["conclusion"]["recommended_parallel_lanes"] = "lane-a"
        self.assertTrue(_blocked(value))


if __name__ == "__main__":
    unittest.main()
