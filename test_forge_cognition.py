from __future__ import annotations
import unittest
from datetime import datetime, timezone

from forge_cognition import (
    CycleAction, ForgeGoal, ForgeMindState, ForgeMode, GoalStatus,
    ReasoningTier, actionable_goals, build_delta_context,
    choose_cycle, choose_reasoning_tier
)

NOW=datetime.now(timezone.utc)

def state(mode=ForgeMode.RUNNING, goals=()):
    return ForgeMindState(
        forge_id="FORGE",
        mode=mode,
        genesis_event_id="GENESIS_EVENT_0001",
        life_goal="Compound verified capability.",
        constitutional_invariants=("Preserve ROOT_OWNER override.",),
        long_horizon_objectives=("Improve Forge, Sol, and Human Specialist.",),
        human_specialist_doctrine={"role":"HUMAN_SPECIALIST"},
        root_owner_continuity={"override_authority":"ABSOLUTE","physical_continuity_required":True},
        specialist_roster={"SOL":{"role":"PRIMARY_ENGINEERING"}},
        world_model={"phase":"GENESIS"},
        capability_frontier={"execution":"LIMITED"},
        goals=tuple(goals),
        unresolved_questions=(),
        current_focus=None,
        last_cycle_summary=None,
        cycle_number=0,
        fencing_token=1,
        state_version=1,
        updated_at=NOW,
    )

class ForgeCognitionTests(unittest.TestCase):
    def test_owner_pause_holds(self):
        action,goal,tier,_=choose_cycle(state(mode=ForgeMode.PAUSED_BY_OWNER))
        self.assertEqual(action,CycleAction.HOLD)
        self.assertIsNone(goal)
        self.assertEqual(tier,ReasoningTier.FAST)

    def test_fast_goal_stays_fast(self):
        g=ForgeGoal("g1","Do deterministic check.",1,complexity=10,uncertainty=10)
        action,goal,tier,_=choose_cycle(state(goals=(g,)))
        self.assertEqual(action,CycleAction.EXECUTE_NEXT)
        self.assertEqual(goal.goal_id,"g1")
        self.assertEqual(tier,ReasoningTier.FAST)

    def test_complex_goal_escalates_deep(self):
        g=ForgeGoal("g1","Design hard subsystem.",1,complexity=90,uncertainty=40)
        self.assertEqual(choose_reasoning_tier(g),ReasoningTier.DEEP)

    def test_dependency_blocks_until_complete(self):
        a=ForgeGoal("a","First.",2,status=GoalStatus.ACTIVE)
        b=ForgeGoal("b","Second.",1,status=GoalStatus.ACTIVE,dependencies=("a",))
        self.assertEqual([g.goal_id for g in actionable_goals(state(goals=(a,b)))],["a"])
        a2=ForgeGoal("a","First.",2,status=GoalStatus.COMPLETE)
        self.assertEqual([g.goal_id for g in actionable_goals(state(goals=(a2,b)))],["b"])

    def test_no_goal_reflects_instead_of_terminating(self):
        action,goal,tier,_=choose_cycle(state())
        self.assertEqual(action,CycleAction.REFLECT_AND_PLAN)
        self.assertIsNone(goal)
        self.assertEqual(tier,ReasoningTier.STANDARD)

    def test_delta_context_is_bounded(self):
        g=ForgeGoal("g1","Do it.",1)
        events=[{"event_id":i} for i in range(100)]
        packet=build_delta_context(state(goals=(g,)),recent_events=events,max_events=24)
        self.assertEqual(len(packet["recent_events"]),24)
        self.assertFalse(packet["performance_rules"]["full_history_replay"])
        self.assertTrue(packet["performance_rules"]["event_driven"])

if __name__=="__main__":
    unittest.main()
