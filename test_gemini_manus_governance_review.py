import unittest

from staging_gemini_manus_governance_review import _critical_issue


class GeminiManusGovernanceReviewGateTests(unittest.TestCase):
    def good(self):
        return {
            "status": "SUCCESS",
            "side_effects_attempted": [],
            "conclusion": {
                "verdict": "PASS",
                "critical_gaps": [],
                "bypass_paths": [],
                "strengths": ["fail closed"],
                "required_fixes": [],
                "ready_for_live_manus_acceptance": True,
            },
        }

    def test_clean_review_passes_gate(self):
        self.assertFalse(_critical_issue(self.good()))

    def test_any_critical_gap_blocks(self):
        value = self.good()
        value["conclusion"]["critical_gaps"] = ["hidden route"]
        self.assertTrue(_critical_issue(value))

    def test_any_bypass_path_blocks(self):
        value = self.good()
        value["conclusion"]["bypass_paths"] = ["Manus -> Notion"]
        self.assertTrue(_critical_issue(value))

    def test_side_effect_attempt_blocks(self):
        value = self.good()
        value["side_effects_attempted"] = ["write"]
        self.assertTrue(_critical_issue(value))

    def test_missing_readiness_blocks(self):
        value = self.good()
        value["conclusion"]["ready_for_live_manus_acceptance"] = False
        self.assertTrue(_critical_issue(value))


if __name__ == "__main__":
    unittest.main()
