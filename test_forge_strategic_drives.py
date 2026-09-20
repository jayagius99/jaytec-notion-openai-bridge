from __future__ import annotations
import unittest

from forge_strategic_drives import (
    DEFAULT_STRATEGIC_DRIVES,
    GENERAL_CAPABILITY_DIMENSIONS,
    VALUE_CAPABILITY_DIMENSIONS,
    CapabilityGap,
    ImprovementCandidate,
    ImprovementDecision,
    ImprovementEvidence,
    OpportunityDecision,
    RootOwnerBoundary,
    StrategicDriveError,
    ValueOpportunity,
    build_strategic_drive_packet,
    evaluate_improvement,
    evaluate_value_opportunity,
    rank_capability_gaps,
    rank_value_opportunities,
    touches_root_boundary,
)

def root():
    return RootOwnerBoundary.parse({
        "root_role_id":"JAY_ROOT_OWNER",
        "sole_root_authority":True,
        "forge_can_modify_root":False,
        "forge_can_hold_root_secrets":False,
        "forge_can_transfer_ownership":False,
        "forge_can_mint_root_authority":False,
        "physical_continuity_required":True,
        "offline_recovery_required":True,
        "distinct_hardware_authenticators_required":2,
        "root_registry_digest":"sha256:abc",
    })

class StrategicDriveTests(unittest.TestCase):
    def test_default_drives_are_max_growth_but_governed(self):
        self.assertEqual(DEFAULT_STRATEGIC_DRIVES.growth_intensity,100)
        self.assertEqual(DEFAULT_STRATEGIC_DRIVES.reinvestment_intensity,100)
        self.assertEqual(set(DEFAULT_STRATEGIC_DRIVES.general_capability_dimensions),set(GENERAL_CAPABILITY_DIMENSIONS))
        self.assertEqual(set(DEFAULT_STRATEGIC_DRIVES.value_capability_dimensions),set(VALUE_CAPABILITY_DIMENSIONS))
        self.assertTrue(DEFAULT_STRATEGIC_DRIVES.retain_only_verified_improvements)

    def test_root_boundary_is_immutable_to_forge(self):
        packet=build_strategic_drive_packet(root_boundary=root())
        self.assertEqual(packet["root_owner_boundary"]["root_role_id"],"JAY_ROOT_OWNER")
        self.assertFalse(packet["root_owner_boundary"]["forge_can_modify_root"])
        self.assertTrue(packet["root_owner_boundary"]["offline_recovery_required"])

    def test_root_transfer_or_secret_custody_rejected(self):
        data=root().to_dict()
        data["forge_can_transfer_ownership"]=True
        with self.assertRaises(StrategicDriveError):
            RootOwnerBoundary.parse(data)
        data=root().to_dict()
        data["forge_can_hold_root_secrets"]=True
        with self.assertRaises(StrategicDriveError):
            RootOwnerBoundary.parse(data)

    def test_root_scopes_are_never_self_improvement_targets(self):
        self.assertTrue(touches_root_boundary(["execution_authority_firewall:permit"]))
        candidate=ImprovementCandidate.parse({
            "candidate_id":"x",
            "target_component":"FORGE",
            "hypothesis":"Change root.",
            "change_scopes":["root_owner:authority"],
            "expected_capability_gain":100,
            "rollback_plan":"revert",
            "sandbox_only_before_acceptance":True,
            "requires_owner_review":False,
            "production_effect":False,
            "spend_effect":False,
        })
        self.assertEqual(evaluate_improvement(candidate,None),ImprovementDecision.REJECT)

    def test_unexecuted_improvement_cannot_be_retained(self):
        c=ImprovementCandidate.parse({
            "candidate_id":"c1","target_component":"FORGE","hypothesis":"faster planner",
            "change_scopes":["forge:planner"],"expected_capability_gain":20,
            "rollback_plan":"revert sha","sandbox_only_before_acceptance":True,
            "requires_owner_review":False,"production_effect":False,"spend_effect":False,
        })
        self.assertEqual(evaluate_improvement(c,None),ImprovementDecision.RETEST)

    def test_verified_nonprivileged_improvement_can_be_retained(self):
        c=ImprovementCandidate.parse({
            "candidate_id":"c1","target_component":"FORGE","hypothesis":"faster planner",
            "change_scopes":["forge:planner"],"expected_capability_gain":20,
            "rollback_plan":"revert sha","sandbox_only_before_acceptance":True,
            "requires_owner_review":False,"production_effect":False,"spend_effect":False,
        })
        e=ImprovementEvidence.parse({
            "artifact_sha":"abc","tests_executed":True,"benchmark_ids":["bench-1"],
            "acceptance_passed":True,"rollback_tested":True,"critical_regressions":[],
            "identity_pinned":True,"fallback_policy_verified":True,"measured_gain":0.12,
        })
        self.assertEqual(evaluate_improvement(c,e),ImprovementDecision.RETAIN)

    def test_core_triad_upgrade_requires_identity_and_fallback_verification(self):
        c=ImprovementCandidate.parse({
            "candidate_id":"sol-upgrade","target_component":"SOL","hypothesis":"better engineer",
            "change_scopes":["specialist:sol"],"expected_capability_gain":30,
            "rollback_plan":"restore pinned model","sandbox_only_before_acceptance":True,
            "requires_owner_review":False,"production_effect":False,"spend_effect":False,
        })
        e=ImprovementEvidence.parse({
            "artifact_sha":"abc","tests_executed":True,"benchmark_ids":["bench-1"],
            "acceptance_passed":True,"rollback_tested":True,"critical_regressions":[],
            "identity_pinned":False,"fallback_policy_verified":True,"measured_gain":0.25,
        })
        self.assertEqual(evaluate_improvement(c,e),ImprovementDecision.RETEST)

    def test_paid_or_production_improvement_escalates_after_tests(self):
        c=ImprovementCandidate.parse({
            "candidate_id":"infra","target_component":"JAYTEC_CONTROL_PLANE","hypothesis":"more compute",
            "change_scopes":["jaytec:compute"],"expected_capability_gain":40,
            "rollback_plan":"scale down","sandbox_only_before_acceptance":True,
            "requires_owner_review":False,"production_effect":True,"spend_effect":True,
        })
        e=ImprovementEvidence.parse({
            "artifact_sha":"abc","tests_executed":True,"benchmark_ids":["bench-1"],
            "acceptance_passed":True,"rollback_tested":True,"critical_regressions":[],
            "identity_pinned":True,"fallback_policy_verified":True,"measured_gain":0.3,
        })
        self.assertEqual(evaluate_improvement(c,e),ImprovementDecision.OWNER_REVIEW)

    def test_capability_gap_ranking_prioritises_large_confident_gap(self):
        a=CapabilityGap.parse({"capability":"a","current_score":20,"target_score":80,"evidence_confidence":0.9,"blocking_dependencies":[]})
        b=CapabilityGap.parse({"capability":"b","current_score":40,"target_score":70,"evidence_confidence":0.6,"blocking_dependencies":[]})
        self.assertEqual(rank_capability_gaps([b,a])[0].capability,"a")

    def test_unlawful_or_deceptive_value_is_rejected(self):
        o=ValueOpportunity.parse({
            "opportunity_id":"bad","mechanism":"fraud","expected_value_score":100,"capability_synergy":100,
            "capital_efficiency":100,"time_to_value_score":100,"evidence_confidence":1.0,
            "downside_risk":1,"ongoing_burden":1,"lawful":False,"sustainable":True,
            "deceptive":True,"unauthorized_access":False,"regulated_or_licensed_activity":False,
            "required_scopes":[],"requires_external_spend":False,"requires_new_legal_entity_or_account":False,
            "known_obligations_covered":True,"funds_or_resources_available":True,
        })
        self.assertEqual(evaluate_value_opportunity(o),OpportunityDecision.REJECT)

    def test_external_spend_or_legal_entity_requires_owner_review(self):
        o=ValueOpportunity.parse({
            "opportunity_id":"business","mechanism":"launch lawful product","expected_value_score":80,"capability_synergy":85,
            "capital_efficiency":80,"time_to_value_score":70,"evidence_confidence":0.8,
            "downside_risk":20,"ongoing_burden":20,"lawful":True,"sustainable":True,
            "deceptive":False,"unauthorized_access":False,"regulated_or_licensed_activity":False,
            "required_scopes":["forge:business"],"requires_external_spend":True,"requires_new_legal_entity_or_account":True,
            "known_obligations_covered":True,"funds_or_resources_available":True,
        })
        self.assertEqual(evaluate_value_opportunity(o),OpportunityDecision.OWNER_REVIEW)

    def test_root_touching_value_opportunity_rejected(self):
        o=ValueOpportunity.parse({
            "opportunity_id":"bad-root","mechanism":"use root vault for business","expected_value_score":100,"capability_synergy":100,
            "capital_efficiency":100,"time_to_value_score":100,"evidence_confidence":1.0,
            "downside_risk":1,"ongoing_burden":1,"lawful":True,"sustainable":True,
            "deceptive":False,"unauthorized_access":False,"regulated_or_licensed_activity":False,
            "required_scopes":["root_credentials:export"],"requires_external_spend":False,"requires_new_legal_entity_or_account":False,
            "known_obligations_covered":True,"funds_or_resources_available":True,
        })
        self.assertEqual(evaluate_value_opportunity(o),OpportunityDecision.REJECT)

if __name__=="__main__":
    unittest.main()
