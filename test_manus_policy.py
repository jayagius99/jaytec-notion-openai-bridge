import unittest

from manus_policy import (
    ManusProfile,
    ManusProfilePolicyError,
    PaidOverrideAuthority,
    allow_manus_fallback,
    authorize_manus_route,
    canonicalize_manus_profile,
    verify_manus_profile,
)


class ManusPolicyTests(unittest.TestCase):
    def test_default_is_lite(self):
        decision = authorize_manus_route(
            route_supports_profile_selector=True,
        )
        self.assertEqual(decision.requested_profile, ManusProfile.LITE)
        self.assertFalse(decision.paid_profile)

    def test_ui_aliases_normalize(self):
        self.assertEqual(canonicalize_manus_profile("Manus Lite"), ManusProfile.LITE)
        self.assertEqual(canonicalize_manus_profile("1.6"), ManusProfile.STANDARD)
        self.assertEqual(canonicalize_manus_profile("Manus 1.6 Max"), ManusProfile.MAX)

    def test_route_without_profile_selector_fails_closed(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PROFILE_SELECTOR_UNAVAILABLE"
        ):
            authorize_manus_route(route_supports_profile_selector=False)

    def test_paid_profiles_block_without_explicit_jay_override(self):
        for profile in ("1.6", "standard", "max", "Manus 1.6 Max"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(
                    ManusProfilePolicyError, "MANUS_PAID_PROFILE_BLOCKED"
                ):
                    authorize_manus_route(
                        requested_profile=profile,
                        route_supports_profile_selector=True,
                    )

    def test_explicit_paid_override_is_scoped_to_that_route_decision(self):
        decision = authorize_manus_route(
            requested_profile="1.6",
            route_supports_profile_selector=True,
            explicit_paid_override=True,
            paid_override_authority=PaidOverrideAuthority.CURRENT_USER_MESSAGE,
        )
        self.assertEqual(decision.requested_profile, ManusProfile.STANDARD)
        self.assertTrue(decision.paid_profile)

    def test_truthy_non_boolean_flags_fail_closed(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_SELECTOR_CAPABILITY_INVALID"
        ):
            authorize_manus_route(route_supports_profile_selector="true")

        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_OVERRIDE_FLAG_INVALID"
        ):
            authorize_manus_route(
                requested_profile="1.6",
                route_supports_profile_selector=True,
                explicit_paid_override="false",
                paid_override_authority="current_user_message",
            )

    def test_paid_override_requires_current_message_authority(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PAID_OVERRIDE_NOT_CURRENT"
        ):
            authorize_manus_route(
                requested_profile="max",
                route_supports_profile_selector=True,
                explicit_paid_override=True,
            )

        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_OVERRIDE_AUTHORITY_INVALID"
        ):
            authorize_manus_route(
                requested_profile="max",
                route_supports_profile_selector=True,
                explicit_paid_override=True,
                paid_override_authority="old_chat",
            )

    def test_case_and_whitespace_cannot_bypass_profile_policy(self):
        self.assertEqual(
            canonicalize_manus_profile("  MaNuS   LiTe  "),
            ManusProfile.LITE,
        )
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PAID_PROFILE_BLOCKED"
        ):
            authorize_manus_route(
                requested_profile="  MANUS   1.6 MAX ",
                route_supports_profile_selector=True,
            )

    def test_observed_lite_is_verified(self):
        decision = authorize_manus_route(
            requested_profile="lite",
            route_supports_profile_selector=True,
        )
        self.assertIs(
            verify_manus_profile(decision, observed_profile="Manus Lite"),
            decision,
        )

    def test_missing_observed_profile_is_not_verified(self):
        decision = authorize_manus_route(route_supports_profile_selector=True)
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PROFILE_UNOBSERVABLE"
        ):
            verify_manus_profile(decision, observed_profile=None)

    def test_profile_mismatch_fails_closed(self):
        decision = authorize_manus_route(route_supports_profile_selector=True)
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PROFILE_MISMATCH"
        ):
            verify_manus_profile(decision, observed_profile="1.6")

    def test_no_automatic_profile_fallback(self):
        self.assertFalse(allow_manus_fallback(from_profile="lite", to_profile="1.6"))
        self.assertFalse(allow_manus_fallback(from_profile="lite", to_profile="max"))


if __name__ == "__main__":
    unittest.main()
