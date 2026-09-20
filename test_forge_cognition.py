from __future__ import annotations
import unittest
from datetime import datetime, timezone

from forge_cognition import (
    CycleAction, ForgeGoal, ForgeMindState, ForgeMode, GoalStatus,
    ReasoningTier, actionable_goals, build_delta_context,
    choose_cycle, choose_reasoning_tier, next_cycle_delay_seconds,
    parallel_goal_batch, _validate_goal_graph, ForgeCognitionError, ForgeMindStore,
    reasoning_policy_for_tier, validate_cycle_result_shape
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
        root_owner_continuity={
            "root_role_id":"JAY_ROOT_OWNER",
            "sole_root_authority":True,
            "forge_can_modify_root":False,
            "forge_can_hold_root_secrets":False,
            "forge_can_transfer_ownership":False,
            "forge_can_mint_root_authority":False,
            "physical_continuity_required":True,
            "offline_recovery_required":True,
            "distinct_hardware_authenticators_required":2,
            "root_registry_digest":"sha256:test",
        },
        strategic_drives={
            "capability_growth_enabled":True,
            "sustainable_value_growth_enabled":True,
            "growth_intensity":100,
            "reinvestment_intensity":100,
            "general_capability_dimensions":[
                "generalisation","continual_learning","metacognition","adaptive_strategy_selection",
                "cross_domain_transfer","capability_acquisition","long_horizon_reasoning","evidence_based_self_improvement"
            ],
            "value_capability_dimensions":[
                "revenue_generation","productive_asset_creation","owned_ip_creation","automation_leverage",
                "capital_efficiency","customer_value_creation","infrastructure_compounding","specialist_capability_reinvestment"
            ],
            "retain_only_verified_improvements":True,
            "uncontrolled_self_modification_forbidden":True,
            "lawful_only":True,
            "sustainable_only":True,
        },
        specialist_roster={"SOL":{"role":"PRIMARY_ENGINEERING"}},
        world_model={"phase":"GENESIS"},
        capability_frontier={"execution":"LIMITED"},
        world_model_digest="cached-world-digest",
        capability_frontier_digest="cached-capability-digest",
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
            "root_owner_continuity":{
            "root_role_id":"JAY_ROOT_OWNER",
            "sole_root_authority":True,
            "forge_can_modify_root":False,
            "forge_can_hold_root_secrets":False,
            "forge_can_transfer_ownership":False,
            "forge_can_mint_root_authority":False,
            "physical_continuity_required":True,
            "offline_recovery_required":True,
            "distinct_hardware_authenticators_required":2,
            "root_registry_digest":"sha256:test",
        },
            "strategic_drives":{
            "capability_growth_enabled":True,
            "sustainable_value_growth_enabled":True,
            "growth_intensity":100,
            "reinvestment_intensity":100,
            "general_capability_dimensions":[
                "generalisation","continual_learning","metacognition","adaptive_strategy_selection",
                "cross_domain_transfer","capability_acquisition","long_horizon_reasoning","evidence_based_self_improvement"
            ],
            "value_capability_dimensions":[
                "revenue_generation","productive_asset_creation","owned_ip_creation","automation_leverage",
                "capital_efficiency","customer_value_creation","infrastructure_compounding","specialist_capability_reinvestment"
            ],
            "retain_only_verified_improvements":True,
            "uncontrolled_self_modification_forbidden":True,
            "lawful_only":True,
            "sustainable_only":True,
        },
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
            "root_owner_continuity":{
            "root_role_id":"JAY_ROOT_OWNER",
            "sole_root_authority":True,
            "forge_can_modify_root":False,
            "forge_can_hold_root_secrets":False,
            "forge_can_transfer_ownership":False,
            "forge_can_mint_root_authority":False,
            "physical_continuity_required":True,
            "offline_recovery_required":True,
            "distinct_hardware_authenticators_required":2,
            "root_registry_digest":"sha256:test",
        },
            "strategic_drives":{
            "capability_growth_enabled":True,
            "sustainable_value_growth_enabled":True,
            "growth_intensity":100,
            "reinvestment_intensity":100,
            "general_capability_dimensions":[
                "generalisation","continual_learning","metacognition","adaptive_strategy_selection",
                "cross_domain_transfer","capability_acquisition","long_horizon_reasoning","evidence_based_self_improvement"
            ],
            "value_capability_dimensions":[
                "revenue_generation","productive_asset_creation","owned_ip_creation","automation_leverage",
                "capital_efficiency","customer_value_creation","infrastructure_compounding","specialist_capability_reinvestment"
            ],
            "retain_only_verified_improvements":True,
            "uncontrolled_self_modification_forbidden":True,
            "lawful_only":True,
            "sustainable_only":True,
        },
            "specialist_roster":{},
            "world_model":{},
            "capability_frontier":{},
            "goals":[],
        }
        with self.assertRaisesRegex(ForgeCognitionError,"SOL_PRIMARY_REQUIRED"):
            store.seed_pre_genesis(packet)

    def test_cycle_cannot_mutate_permanent_drives_or_root_boundary(self):
        with self.assertRaisesRegex(ForgeCognitionError,"IMMUTABLE_COGNITIVE_FIELD_MUTATION_FORBIDDEN"):
            validate_cycle_result_shape({
                "summary":"try",
                "strategic_drives":{"capability_growth_enabled":False},
            })
        with self.assertRaisesRegex(ForgeCognitionError,"IMMUTABLE_COGNITIVE_FIELD_MUTATION_FORBIDDEN"):
            validate_cycle_result_shape({
                "summary":"try",
                "root_owner_continuity":{"forge_can_modify_root":True},
            })

    def test_unknown_cycle_result_field_fails_closed(self):
        with self.assertRaisesRegex(ForgeCognitionError,"CYCLE_RESULT_FIELDS_INVALID"):
            validate_cycle_result_shape({"summary":"ok","surprise_field":"x"})

    def test_delta_context_is_bounded(self):
        g=ForgeGoal("g1","Do it.",1)
        events=[{"event_id":i} for i in range(100)]
        packet=build_delta_context(state(goals=(g,)),recent_events=events,max_events=24)
        self.assertEqual(len(packet["recent_events"]),24)
        self.assertFalse(packet["performance_rules"]["full_history_replay"])
        self.assertTrue(packet["performance_rules"]["event_driven"])
        self.assertNotIn("world_model",packet)
        self.assertEqual(packet["world_model_digest"],"cached-world-digest")
        self.assertEqual(packet["capability_frontier_digest"],"cached-capability-digest")
        self.assertEqual(packet["working_memory"]["focus"],"current only")

if __name__=="__main__":
    unittest.main()
