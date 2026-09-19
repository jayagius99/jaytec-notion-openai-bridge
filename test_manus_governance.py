import unittest

from manus_governance import (
    APPROVED_DIRECT_CONNECTORS,
    AuthoritySource,
    DIRECTIVE_VERSION,
    ManusGovernanceError,
    ManusScope,
    assert_approved_manus_connectors,
    authorize_manus_action,
    build_minimal_task_packet,
    directive_sha256,
    render_directive,
    specialist_request,
    verify_manus_completion,
)


class ManusGovernanceTests(unittest.TestCase):
    def test_manus_can_evolve_own_house(self):
        d = authorize_manus_action(
            scope=ManusScope.MANUS_INTERNAL,
            authority_source=AuthoritySource.MANUS,
        )
        self.assertTrue(d.allowed)
        self.assertFalse(d.must_escalate)

    def test_manus_cannot_self_authorize_jaytec_change(self):
        d = authorize_manus_action(
            scope=ManusScope.JAYTEC_CORE_CHANGE,
            authority_source=AuthoritySource.MANUS,
            explicit_current_task_authorization=True,
        )
        self.assertFalse(d.allowed)
        self.assertTrue(d.must_escalate)

    def test_jay_or_chatgpt_can_authorize_current_jaytec_change(self):
        for source in (AuthoritySource.JAY, AuthoritySource.CHATGPT):
            with self.subTest(source=source):
                d = authorize_manus_action(
                    scope=ManusScope.JAYTEC_CORE_CHANGE,
                    authority_source=source,
                    explicit_current_task_authorization=True,
                )
                self.assertTrue(d.allowed)

    def test_old_or_non_current_authority_is_not_enough(self):
        d = authorize_manus_action(
            scope=ManusScope.JAYTEC_DELEGATED_TASK,
            authority_source=AuthoritySource.JAYTEC,
            explicit_current_task_authorization=False,
        )
        self.assertFalse(d.allowed)
        self.assertTrue(d.must_escalate)

    def test_notion_requires_jay_through_chatgpt_for_current_task(self):
        for source in (
            AuthoritySource.MANUS,
            AuthoritySource.JAYTEC,
            AuthoritySource.JAY,
        ):
            with self.subTest(source=source):
                d = authorize_manus_action(
                    scope=ManusScope.NOTION_AGENT_WORK,
                    authority_source=source,
                    explicit_current_task_authorization=True,
                    notion_authorized_by_jay_via_chatgpt=True,
                )
                self.assertFalse(d.allowed)
        allowed = authorize_manus_action(
            scope=ManusScope.NOTION_AGENT_WORK,
            authority_source=AuthoritySource.CHATGPT,
            explicit_current_task_authorization=True,
            notion_authorized_by_jay_via_chatgpt=True,
        )
        self.assertTrue(allowed.allowed)

    def test_direct_model_provider_access_is_always_escalated(self):
        for source in AuthoritySource:
            d = authorize_manus_action(
                scope=ManusScope.MODEL_PROVIDER_DIRECT,
                authority_source=source,
                explicit_current_task_authorization=True,
            )
            self.assertFalse(d.allowed)
            self.assertTrue(d.must_escalate)

    def test_connector_allowlist(self):
        self.assertEqual(
            assert_approved_manus_connectors(["GitHub", "Neon", "Render"]),
            ("github", "neon", "render"),
        )
        self.assertEqual(
            APPROVED_DIRECT_CONNECTORS,
            frozenset({"github", "neon", "render"}),
        )

    def test_blocked_connector_fails_closed(self):
        for connector in ("Notion", "OpenAI", "OpenRouter", "OpenRouter API"):
            with self.subTest(connector=connector):
                with self.assertRaises(ManusGovernanceError):
                    assert_approved_manus_connectors(["GitHub", connector])

    def test_unknown_connector_fails_closed(self):
        with self.assertRaisesRegex(
            ManusGovernanceError, "MANUS_DIRECT_CONNECTOR_NOT_ALLOWLISTED"
        ):
            assert_approved_manus_connectors(["github", "mystery"])

    def test_minimal_packet_has_no_arbitrary_extra_fields(self):
        p = build_minimal_task_packet(
            task_id="T-1",
            objective="Inspect a JAYTEC runtime symptom and report evidence.",
            scope=ManusScope.JAYTEC_DELEGATED_TASK,
            authority_source=AuthoritySource.CHATGPT,
            allowed_actions=["inspect", "diagnose", "report"],
            required_context={"service": "jaytec"},
        )
        self.assertEqual(p["directive_version"], DIRECTIVE_VERSION)
        self.assertNotIn("full_chat_history", p)
        self.assertIn("claim_unverified_completion", p["forbidden_actions"])

    def test_minimal_packet_redacts_secret_shaped_values(self):
        p = build_minimal_task_packet(
            task_id="T-2",
            objective="Review configuration safely.",
            scope=ManusScope.JAYTEC_DELEGATED_TASK,
            authority_source=AuthoritySource.CHATGPT,
            allowed_actions=["inspect"],
            required_context={"api_key": "super-secret", "note": "token=abc123456789"},
        )
        self.assertEqual(p["required_context"]["api_key"], "[REDACTED]")
        self.assertIn("[REDACTED]", p["required_context"]["note"])

    def test_success_requires_verification_and_evidence(self):
        with self.assertRaisesRegex(
            ManusGovernanceError, "MANUS_SUCCESS_NOT_VERIFIED"
        ):
            verify_manus_completion(
                {
                    "status": "SUCCESS",
                    "evidence": ["test"],
                    "verification": {
                        "instruction_match_verified": True,
                        "scope_verified": True,
                        "evidence_verified": False,
                        "no_unauthorized_side_effects": True,
                        "duplicate_work_check_passed": True,
                    },
                }
            )

        verify_manus_completion(
            {
                "status": "SUCCESS",
                "evidence": [
                    {
                        "kind": "test",
                        "source": "test_manus_governance",
                        "reference": "structured-success-evidence",
                        "observed_at": "2026-09-19T00:00:00Z",
                        "claim": "The bounded test result satisfies every required completion-verification facet.",
                        "supports": [
                            "instruction_match_verified",
                            "scope_verified",
                            "evidence_verified",
                            "no_unauthorized_side_effects",
                            "duplicate_work_check_passed",
                        ],
                    }
                ],
                "verification": {
                    "instruction_match_verified": True,
                    "scope_verified": True,
                    "evidence_verified": True,
                    "no_unauthorized_side_effects": True,
                    "duplicate_work_check_passed": True,
                },
            }
        )

    def test_specialist_request_is_request_only(self):
        r = specialist_request(
            parent_task_id="T-1",
            specialist="gemini",
            objective="review architecture",
            reason="independent review needed",
            required_context={"policy_version": DIRECTIVE_VERSION},
        )
        self.assertEqual(r["authority"], "REQUEST_ONLY_NO_SELF_DISPATCH")

    def test_directive_contains_nonnegotiable_boundaries(self):
        text = render_directive()
        for phrase in (
            "Manus may evolve Manus",
            "MUST NOT edit",
            "The Notion Agent is NOT JAYTEC",
            '"JAYTEC" always means the JAYTEC system/control',
            "strict pass-through transport only",
            "GitHub, Neon, and Render only",
            "Manus Lite is the default and required profile",
            "Never claim success because an action merely ran",
            "you may not alter, bypass, reinterpret, or supersede it yourself",
        ):
            self.assertIn(phrase, text)
        self.assertEqual(len(directive_sha256()), 64)


if __name__ == "__main__":
    unittest.main()
