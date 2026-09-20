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
        decision = authorize_manus_route(route_supports_profile_selector=True)
        self.assertEqual(decision.requested_profile, ManusProfile.LITE)
        self.assertFalse(decision.paid_profile)
        self.assertFalse(decision.explicit_paid_override)

    def test_lite_aliases_normalize(self):
        for value in ("lite", "Manus Lite", "manus-1.6-lite", "1.6-lite"):
            with self.subTest(value=value):
                self.assertEqual(
                    canonicalize_manus_profile(value),
                    ManusProfile.LITE,
                )

    def test_non_lite_aliases_parse_but_are_never_authorized(self):
        for profile in (
            "1.6",
            "manus-1.6",
            "standard",
            "Manus Standard",
            "max",
            "Manus 1.6 Max",
            "2.0-max",
        ):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(
                    ManusProfilePolicyError,
                    "MANUS_NON_LITE_PROFILE_PERMANENTLY_BLOCKED",
                ):
                    authorize_manus_route(
                        requested_profile=profile,
                        route_supports_profile_selector=True,
                    )

    def test_route_without_profile_selector_fails_closed(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_PROFILE_SELECTOR_UNAVAILABLE",
        ):
            authorize_manus_route(route_supports_profile_selector=False)

    def test_paid_override_is_permanently_disabled_even_from_current_user(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_PAID_OVERRIDE_PERMANENTLY_DISABLED",
        ):
            authorize_manus_route(
                requested_profile="standard",
                route_supports_profile_selector=True,
                explicit_paid_override=True,
                paid_override_authority=PaidOverrideAuthority.CURRENT_USER_MESSAGE,
            )

    def test_override_shaped_input_is_blocked_even_for_lite(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_PAID_OVERRIDE_PERMANENTLY_DISABLED",
        ):
            authorize_manus_route(
                requested_profile="lite",
                route_supports_profile_selector=True,
                paid_override_authority="current_user_message",
            )

    def test_truthy_non_boolean_flags_fail_closed(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_SELECTOR_CAPABILITY_INVALID",
        ):
            authorize_manus_route(route_supports_profile_selector="true")

        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_OVERRIDE_FLAG_INVALID",
        ):
            authorize_manus_route(
                requested_profile="lite",
                route_supports_profile_selector=True,
                explicit_paid_override="false",
            )

    def test_observed_lite_is_verified(self):
        decision = authorize_manus_route(
            requested_profile="lite",
            route_supports_profile_selector=True,
        )
        self.assertIs(
            verify_manus_profile(decision, observed_profile="manus-1.6-lite"),
            decision,
        )

    def test_missing_observed_profile_is_not_verified(self):
        decision = authorize_manus_route(route_supports_profile_selector=True)
        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_PROFILE_UNOBSERVABLE",
        ):
            verify_manus_profile(decision, observed_profile=None)

    def test_profile_mismatch_fails_closed(self):
        decision = authorize_manus_route(route_supports_profile_selector=True)
        for observed in ("1.6", "standard", "max"):
            with self.subTest(observed=observed):
                with self.assertRaisesRegex(
                    ManusProfilePolicyError,
                    "MANUS_PROFILE_MISMATCH",
                ):
                    verify_manus_profile(
                        decision,
                        observed_profile=observed,
                    )

    def test_no_automatic_profile_fallback(self):
        self.assertFalse(allow_manus_fallback(from_profile="lite", to_profile="1.6"))
        self.assertFalse(allow_manus_fallback(from_profile="lite", to_profile="max"))


if __name__ == "__main__":
    unittest.main()
