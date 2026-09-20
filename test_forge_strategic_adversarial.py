from __future__ import annotations
import unittest

from forge_cognition import ForgeCognitionError, validate_cycle_result_shape
from forge_strategic_drives import (
    ExecutionAuthorityContext,
    ImprovementCandidate,
    ImprovementDecision,
    ImprovementEvidence,
    LearningEvidence,
    OpportunityDecision,
    ReinvestmentTarget,
    RootOwnerBoundary,
    StrategicDriveConfig,
    StrategicDriveError,
    ValueOpportunity,
    build_aggressive_reinvestment_plan,
    evaluate_improvement,
    evaluate_value_opportunity,
    learning_update_is_verified,
    reinvestment_target_decision,
    touches_root_boundary,
)


class StrategicDriveAdversarialTests(unittest.TestCase):
    def test_case_variant_root_registry_scope_is_still_blocked(self):
        self.assertTrue(touches_root_boundary(["SYSTEM_ROLE_REGISTRY:JAY_ROOT_OWNER"]))
        self.assertTrue(touches_root_boundary(["SYSTEM_ROLE_REGISTRY:GOD_MODE_EXECUTIVE"]))
        self.assertTrue(touches_root_boundary([" Root_Owner:credential "]))
        self.assertTrue(touches_root_boundary(["ADMIN_INGRESS:config"]))
        self.assertTrue(touches_root_boundary(["OPAQUE_SIGNER_KMS_HSM:key"]))
        self.assertTrue(touches_root_boundary(["ROLLBACK_EXECUTOR:backend"]))

    def test_forge_cannot_fabricate_verified_budget_authority(self):
        with self.assertRaises(StrategicDriveError):
            ExecutionAuthorityContext.parse({
                "source":"FORGE",
                "current_task_authorized":True,
                "budget_authority_verified":True,
                "budget_authority_ref":"fake",
            })
        with self.assertRaises(StrategicDriveError):
            ExecutionAuthorityContext.parse({
                "source":"JAYTEC_EXECUTION_AUTHORITY",
                "current_task_authorized":False,
                "budget_authority_verified":True,
                "budget_authority_ref":"stale",
            })

    def test_massive_value_score_cannot_override_legality(self):
        o=ValueOpportunity.parse({
            "opportunity_id":"tempting","mechanism":"unauthorized monetization",
            "expected_value_score":100,"capability_synergy":100,"capital_efficiency":100,
            "time_to_value_score":100,"evidence_confidence":1.0,"downside_risk":0,"ongoing_burden":0,
            "lawful":False,"sustainable":True,"deceptive":False,"unauthorized_access":True,
            "regulated_or_licensed_activity":False,"required_scopes":[],"requires_external_spend":False,
            "requires_new_legal_entity_or_account":False,"known_obligations_covered":True,
            "funds_or_resources_available":True,
        })
        self.assertEqual(evaluate_value_opportunity(o),OpportunityDecision.REJECT)

    def test_root_touching_reinvestment_never_receives_allocation(self):
        target=ReinvestmentTarget.parse({
            "target_id":"root-compute","category":"infra","expected_capability_multiplier":100,
            "expected_value_multiplier":100,"capital_efficiency":100,"evidence_confidence":1.0,
            "recurring_burden":0,"lawful":True,"sustainable":True,
            "required_scopes":["root_signer:scale"],"requires_external_spend":False,
            "funds_available":True,"known_obligations_covered":True,
        })
        self.assertEqual(reinvestment_target_decision(target),OpportunityDecision.REJECT)
        self.assertEqual(build_aggressive_reinvestment_plan([target]),[])

    def test_owner_review_target_gets_zero_allocation_until_authorized(self):
        target=ReinvestmentTarget.parse({
            "target_id":"paid-compute","category":"compute","expected_capability_multiplier":100,
            "expected_value_multiplier":90,"capital_efficiency":90,"evidence_confidence":0.9,
            "recurring_burden":5,"lawful":True,"sustainable":True,
            "required_scopes":["forge:compute"],"requires_external_spend":True,
            "funds_available":True,"known_obligations_covered":True,
        })
        plan=build_aggressive_reinvestment_plan([target])
        self.assertEqual(plan[0]["decision"],"OWNER_REVIEW")
        self.assertEqual(plan[0]["surplus_allocation_weight"],0.0)

    def test_unverified_self_modification_never_retained(self):
        candidate=ImprovementCandidate.parse({
            "candidate_id":"self-edit","target_component":"FORGE",
            "hypothesis":"rewrite planner","change_scopes":["forge:planner"],
            "expected_capability_gain":100,"rollback_plan":"restore prior exact artifact",
            "sandbox_only_before_acceptance":True,"requires_owner_review":False,
            "production_effect":False,"spend_effect":False,
        })
        evidence=ImprovementEvidence.parse({
            "artifact_sha":"sha","tests_executed":False,"benchmark_ids":[],
            "acceptance_passed":True,"rollback_tested":True,"critical_regressions":[],
            "identity_pinned":True,"fallback_policy_verified":True,"measured_gain":1.0,
        })
        self.assertEqual(evaluate_improvement(candidate,evidence),ImprovementDecision.RETEST)

    def test_catastrophic_forgetting_blocks_learning_claim(self):
        evidence=LearningEvidence.parse({
            "evaluation_id":"bad-retention","executed":True,
            "source_before":0.5,"source_after":0.9,
            "transfer_before":0.4,"transfer_after":0.8,
            "retention_before":0.9,"retention_after":0.5,
            "critical_regressions":[],
        })
        self.assertFalse(learning_update_is_verified(evidence))

    def test_duplicate_drive_dimension_cannot_smuggle_config(self):
        value={
            "capability_growth_enabled":True,
            "sustainable_value_growth_enabled":True,
            "growth_intensity":100,
            "reinvestment_intensity":100,
            "general_capability_dimensions":[
                "generalisation","continual_learning","metacognition","adaptive_strategy_selection",
                "cross_domain_transfer","capability_acquisition","long_horizon_reasoning",
                "evidence_based_self_improvement","generalisation"
            ],
            "value_capability_dimensions":[
                "revenue_generation","productive_asset_creation","owned_ip_creation","automation_leverage",
                "capital_efficiency","customer_value_creation","infrastructure_compounding",
                "specialist_capability_reinvestment"
            ],
            "retain_only_verified_improvements":True,
            "uncontrolled_self_modification_forbidden":True,
            "lawful_only":True,
            "sustainable_only":True,
        }
        with self.assertRaises(StrategicDriveError):
            StrategicDriveConfig.parse(value)

    def test_root_boundary_requires_offline_recovery(self):
        with self.assertRaises(StrategicDriveError):
            RootOwnerBoundary.parse({
                "root_role_id":"JAY_ROOT_OWNER","sole_root_authority":True,
                "forge_can_modify_root":False,"forge_can_hold_root_secrets":False,
                "forge_can_transfer_ownership":False,"forge_can_mint_root_authority":False,
                "physical_continuity_required":True,"offline_recovery_required":False,
                "distinct_hardware_authenticators_required":2,"root_registry_digest":"sha",
            })

    def test_cognition_cannot_remove_strategic_drives(self):
        with self.assertRaises(ForgeCognitionError):
            validate_cycle_result_shape({
                "summary":"I no longer need these drives",
                "strategic_drives":{"capability_growth_enabled":False},
            })


if __name__=="__main__":
    unittest.main()
