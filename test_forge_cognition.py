from __future__ import annotations
import unittest
from datetime import datetime, timezone

from forge_cognition import (
    CycleAction, ForgeGoal, ForgeMindState, ForgeMode, GoalStatus,
    ReasoningTier, actionable_goals, build_delta_context,
    choose_cycle, choose_reasoning_tier, next_cycle_delay_seconds,
    parallel_goal_batch, _validate_goal_graph, ForgeCognitionError, ForgeMindStore,
    reasoning_policy_for_tier
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
        working_memory={"focus":"current only"},
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
        self.assertEqual(tier,ReasoningTier.REFLEX)

    def test_reflex_goal_avoids_unnecessary_deep_reasoning(self):
        g=ForgeGoal("g1","Do deterministic check.",1,complexity=10,uncertainty=10)
        action,goal,tier,_=choose_cycle(state(goals=(g,)))
        self.assertEqual(action,CycleAction.EXECUTE_NEXT)
        self.assertEqual(goal.goal_id,"g1")
        self.assertEqual(tier,ReasoningTier.REFLEX)

    def test_fast_goal_stays_fast(self):
        g=ForgeGoal("g1","Do simple reasoning.",1,complexity=25,uncertainty=20)
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


    def test_parallel_batch_only_explicit_safe_goals(self):
        a=ForgeGoal("a","A.",1,parallel_safe=True)
        b=ForgeGoal("b","B.",2,parallel_safe=True)
        c=ForgeGoal("c","C.",3,parallel_safe=False)
        batch=parallel_goal_batch(state(goals=(a,b,c)),max_parallel=3)
        self.assertEqual([g.goal_id for g in batch],["a","b"])

    def test_non_parallel_top_goal_serializes_batch(self):
        a=ForgeGoal("a","A.",1,parallel_safe=False)
        b=ForgeGoal("b","B.",2,parallel_safe=True)
        self.assertEqual([g.goal_id for g in parallel_goal_batch(state(goals=(a,b)))],["a"])

    def test_goal_cycle_rejected(self):
        a=ForgeGoal("a","A.",1,dependencies=("b",))
        b=ForgeGoal("b","B.",2,dependencies=("a",))
        with self.assertRaisesRegex(ForgeCognitionError,"GOAL_DEPENDENCY_CYCLE"):
            _validate_goal_graph([a,b])

    def test_reflex_policy_avoids_model_call(self):
        policy=reasoning_policy_for_tier(ReasoningTier.REFLEX)
        self.assertFalse(policy["model_call"])
        self.assertEqual(policy["context_scope"],"minimal")

    def test_deep_policy_expands_only_on_demand(self):
        policy=reasoning_policy_for_tier(ReasoningTier.DEEP)
        self.assertTrue(policy["model_call"])
        self.assertEqual(policy["effort_hint"],"high")
        self.assertEqual(policy["context_scope"],"expanded_on_demand")

    def test_event_driven_execute_wakes_immediately(self):
        self.assertEqual(next_cycle_delay_seconds(CycleAction.EXECUTE_NEXT,ReasoningTier.REFLEX),0)

    def test_genesis_packet_requires_valid_provenance_before_database(self):
        store=ForgeMindStore("postgresql://invalid")
        packet={
            "forge_id":"FORGE",
            "genesis_event_id":"bad",
            "owner_activation_ref":"owner-approval",
            "life_goal":"Compound capability.",
            "constitutional_invariants":["Preserve ROOT_OWNER."],
            "long_horizon_objectives":["Grow capability."],
            "human_specialist_doctrine":{"role":"HUMAN_SPECIALIST"},
            "root_owner_continuity":{"override_authority":"ABSOLUTE","physical_continuity_required":True},
            "specialist_roster":{"SOL":{"role":"PRIMARY_ENGINEERING"}},
            "world_model":{},
            "capability_frontier":{},
            "goals":[],
        }
        with self.assertRaisesRegex(ForgeCognitionError,"GENESIS_EVENT_ID_INVALID"):
            store.seed_pre_genesis(packet)

    def test_genesis_packet_requires_sol_primary_before_database(self):
        store=ForgeMindStore("postgresql://invalid")
        packet={
            "forge_id":"FORGE",
            "genesis_event_id":"GENESIS_EVENT_0001",
            "owner_activation_ref":"owner-approval",
            "life_goal":"Compound capability.",
            "constitutional_invariants":["Preserve ROOT_OWNER."],
            "long_horizon_objectives":["Grow capability."],
            "human_specialist_doctrine":{"role":"HUMAN_SPECIALIST"},
            "root_owner_continuity":{"override_authority":"ABSOLUTE","physical_continuity_required":True},
            "specialist_roster":{},
            "world_model":{},
            "capability_frontier":{},
            "goals":[],
        }
        with self.assertRaisesRegex(ForgeCognitionError,"SOL_PRIMARY_REQUIRED"):
            store.seed_pre_genesis(packet)

    def test_delta_context_is_bounded(self):
        g=ForgeGoal("g1","Do it.",1)
        events=[{"event_id":i} for i in range(100)]
        packet=build_delta_context(state(goals=(g,)),recent_events=events,max_events=24)
        self.assertEqual(len(packet["recent_events"]),24)
        self.assertFalse(packet["performance_rules"]["full_history_replay"])
        self.assertTrue(packet["performance_rules"]["event_driven"])
        self.assertNotIn("world_model",packet)
        self.assertEqual(packet["working_memory"]["focus"],"current only")

if __name__=="__main__":
    unittest.main()
