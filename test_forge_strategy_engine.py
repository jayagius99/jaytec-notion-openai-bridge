from __future__ import annotations
import unittest

from forge_strategic_drives import CapabilityGap, ValueOpportunity
from forge_strategy_engine import synthesize_strategic_goals, should_run_metacognitive_review


def gap(name,cur,target,confidence):
    return CapabilityGap.parse({
        "capability":name,"current_score":cur,"target_score":target,
        "evidence_confidence":confidence,"blocking_dependencies":[]
    })


def opp(oid,confidence=0.8,spend=False):
    return ValueOpportunity.parse({
        "opportunity_id":oid,"mechanism":"build a useful product","expected_value_score":80,
        "capability_synergy":85,"capital_efficiency":90,"time_to_value_score":75,
        "evidence_confidence":confidence,"downside_risk":20,"ongoing_burden":20,
        "lawful":True,"sustainable":True,"deceptive":False,"unauthorized_access":False,
        "regulated_or_licensed_activity":False,"required_scopes":["forge:product"],
        "requires_external_spend":spend,"requires_new_legal_entity_or_account":False,
        "known_obligations_covered":True,"funds_or_resources_available":True,
    })


class StrategyEngineTests(unittest.TestCase):
    def test_interleaves_both_permanent_drives(self):
        goals=synthesize_strategic_goals(
            [gap("metacognition",20,80,0.9),gap("transfer",30,70,0.8)],
            [opp("product-a"),opp("product-b")],
            max_goals=4,
        )
        self.assertEqual(goals[0].drive,"GENERAL_CAPABILITY_GROWTH")
        self.assertEqual(goals[1].drive,"LAWFUL_SUSTAINABLE_VALUE_GROWTH")
        self.assertEqual({g.drive for g in goals},{"GENERAL_CAPABILITY_GROWTH","LAWFUL_SUSTAINABLE_VALUE_GROWTH"})

    def test_low_confidence_value_work_is_research_only(self):
        goals=synthesize_strategic_goals([gap("learning",20,60,0.8)],[opp("idea",confidence=0.2)],max_goals=2)
        value=[g for g in goals if g.drive=="LAWFUL_SUSTAINABLE_VALUE_GROWTH"][0]
        self.assertEqual(value.authority_state,"RESEARCH_ONLY")

    def test_spend_path_requires_owner_review(self):
        goals=synthesize_strategic_goals([gap("learning",20,60,0.8)],[opp("paid",spend=True)],max_goals=2)
        value=[g for g in goals if g.drive=="LAWFUL_SUSTAINABLE_VALUE_GROWTH"][0]
        self.assertEqual(value.authority_state,"OWNER_REVIEW_REQUIRED")

    def test_metacognition_triggers_on_failure_or_age(self):
        self.assertTrue(should_run_metacognitive_review(cycles_since_review=25,consecutive_failures=0,uncertainty_drift=0,strategy_switches=0))
        self.assertTrue(should_run_metacognitive_review(cycles_since_review=1,consecutive_failures=2,uncertainty_drift=0,strategy_switches=0))
        self.assertFalse(should_run_metacognitive_review(cycles_since_review=5,consecutive_failures=0,uncertainty_drift=2,strategy_switches=1))

if __name__=="__main__":
    unittest.main()
