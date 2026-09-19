import unittest

from manus_dispatch_contract import ManusDispatchContractError, authorize_manus_dispatch
from manus_governance import (
    APPROVED_DIRECT_CONNECTORS,
    BLOCKED_DIRECT_CONNECTORS,
    AuthoritySource,
    ManusGovernanceError,
    ManusScope,
    assert_approved_manus_connectors,
    authorize_manus_action,
)
from manus_policy import (
    ManusProfile,
    ManusProfilePolicyError,
    PaidOverrideAuthority,
    allow_manus_fallback,
    authorize_manus_route,
)
from relationship_policy import (
    Actor,
    EDGE_RULES,
    Purpose,
    RelationshipPolicyError,
    authorize_relationship,
)


class ManusAdversarialMatrixTests(unittest.TestCase):
    def test_relationship_graph_is_exhaustively_fail_closed(self):
        rule_map = {(r.source, r.destination): r for r in EDGE_RULES}
        for src in Actor:
            for dst in Actor:
                for purpose in Purpose:
                    rule = rule_map.get((src, dst))
                    should_allow = rule is not None and purpose in rule.purposes
                    with self.subTest(src=src, dst=dst, purpose=purpose):
                        if should_allow:
                            authorize_relationship(
                                source=src,
                                destination=dst,
                                purpose=purpose,
                                current_task_authorized=True,
                                jay_authorized_notion_via_chatgpt=True,
                            )
                        else:
                            with self.assertRaises(RelationshipPolicyError):
                                authorize_relationship(
                                    source=src,
                                    destination=dst,
                                    purpose=purpose,
                                    current_task_authorized=True,
                                    jay_authorized_notion_via_chatgpt=True,
                                )

    def test_manus_never_gets_direct_specialist_or_notion_edge(self):
        blocked = {
            Actor.CHATGPT,
            Actor.GEMINI,
            Actor.ENGINEERING,
            Actor.NOTION,
        }
        for dst in blocked:
            for purpose in Purpose:
                with self.subTest(dst=dst, purpose=purpose):
                    with self.assertRaises(RelationshipPolicyError):
                        authorize_relationship(
                            source=Actor.MANUS,
                            destination=dst,
                            purpose=purpose,
                            current_task_authorized=True,
                            jay_authorized_notion_via_chatgpt=True,
                        )

    def test_notion_never_becomes_executor_or_router(self):
        for dst in Actor:
            if dst is Actor.JAYTEC:
                continue
            for purpose in Purpose:
                with self.subTest(dst=dst, purpose=purpose):
                    with self.assertRaises(RelationshipPolicyError):
                        authorize_relationship(
                            source=Actor.NOTION,
                            destination=dst,
                            purpose=purpose,
                            current_task_authorized=True,
                            jay_authorized_notion_via_chatgpt=True,
                        )

    def test_connector_allowlist_has_no_shadow_names(self):
        self.assertEqual(
            APPROVED_DIRECT_CONNECTORS,
            frozenset({"github", "neon", "render"}),
        )
        for name in BLOCKED_DIRECT_CONNECTORS:
            with self.subTest(name=name):
                with self.assertRaises(ManusGovernanceError):
                    assert_approved_manus_connectors(["github", name])
        for variant in (" Notion ", "OPENAI", " OpenRouter  API ", "openrouter"):
            with self.subTest(variant=variant):
                with self.assertRaises(ManusGovernanceError):
                    assert_approved_manus_connectors(["github", variant])

    def test_manus_self_authority_cannot_escape_own_house(self):
        own = authorize_manus_action(
            scope=ManusScope.MANUS_INTERNAL,
            authority_source=AuthoritySource.MANUS,
        )
        self.assertTrue(own.allowed)

        for scope in (
            ManusScope.JAYTEC_DELEGATED_TASK,
            ManusScope.JAYTEC_CORE_CHANGE,
            ManusScope.NOTION_AGENT_WORK,
            ManusScope.MODEL_PROVIDER_DIRECT,
            ManusScope.EXTERNAL_SPEND,
        ):
            with self.subTest(scope=scope):
                decision = authorize_manus_action(
                    scope=scope,
                    authority_source=AuthoritySource.MANUS,
                    explicit_current_task_authorization=True,
                    notion_authorized_by_jay_via_chatgpt=True,
                )
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.must_escalate)

    def test_lite_is_only_automatic_profile(self):
        lite = authorize_manus_route(
            requested_profile=None,
            route_supports_profile_selector=True,
        )
        self.assertEqual(lite.requested_profile, ManusProfile.LITE)

        for profile in ("standard", "max", "1.6", "2.0-max"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(
                    ManusProfilePolicyError, "MANUS_PAID_PROFILE_BLOCKED"
                ):
                    authorize_manus_route(
                        requested_profile=profile,
                        route_supports_profile_selector=True,
                    )

    def test_paid_override_requires_current_user_message(self):
        for profile in ("standard", "max"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(
                    ManusProfilePolicyError, "MANUS_PAID_OVERRIDE_NOT_CURRENT"
                ):
                    authorize_manus_route(
                        requested_profile=profile,
                        route_supports_profile_selector=True,
                        explicit_paid_override=True,
                        paid_override_authority=None,
                    )
                decision = authorize_manus_route(
                    requested_profile=profile,
                    route_supports_profile_selector=True,
                    explicit_paid_override=True,
                    paid_override_authority=PaidOverrideAuthority.CURRENT_USER_MESSAGE,
                )
                self.assertTrue(decision.paid_profile)

    def test_no_profile_fallback_exists_between_any_tiers(self):
        for source in ManusProfile:
            for target in ManusProfile:
                with self.subTest(source=source, target=target):
                    self.assertFalse(
                        allow_manus_fallback(
                            from_profile=source,
                            to_profile=target,
                        )
                    )

    def test_dispatch_rejects_implicit_connectors_even_with_valid_authority(self):
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_CONNECTORS_MUST_BE_EXPLICIT"
        ):
            authorize_manus_dispatch(
                project_id="p",
                expected_project_id="p",
                project_name="MANUS",
                connectors=["github", "neon", "render"],
                connectors_explicit=False,
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                requested_profile="lite",
                route_supports_profile_selector=True,
            )

    def test_dispatch_rejects_wrong_project_and_blocked_connector(self):
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_PROJECT_ID_MISMATCH"
        ):
            authorize_manus_dispatch(
                project_id="p2",
                expected_project_id="p1",
                project_name="MANUS",
                connectors=["github", "neon", "render"],
                connectors_explicit=True,
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                requested_profile="lite",
                route_supports_profile_selector=True,
            )

        with self.assertRaises(ManusGovernanceError):
            authorize_manus_dispatch(
                project_id="p",
                expected_project_id="p",
                project_name="MANUS",
                connectors=["github", "neon", "render", "notion"],
                connectors_explicit=True,
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                requested_profile="lite",
                route_supports_profile_selector=True,
            )


if __name__ == "__main__":
    unittest.main()
