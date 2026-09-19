import unittest

from manus_dispatch_contract import (
    ManusDispatchContractError,
    authorize_manus_dispatch,
    verify_manus_dispatch_result,
)
from manus_governance import ManusGovernanceError
from manus_policy import ManusProfilePolicyError


PROJECT = "project-manus-123"


class ManusDispatchContractTests(unittest.TestCase):
    def base(self, **overrides):
        value = dict(
            project_id=PROJECT,
            expected_project_id=PROJECT,
            project_name="MANUS",
            connectors=["github", "neon", "render"],
            connectors_explicit=True,
            scope="jaytec_delegated_task",
            authority_source="chatgpt",
            current_task_authorized=True,
            requested_profile="lite",
            route_supports_profile_selector=True,
        )
        value.update(overrides)
        return value

    def test_exact_manus_project_is_required(self):
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_PROJECT_ID_MISMATCH"
        ):
            authorize_manus_dispatch(**self.base(project_id="other-project"))
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_PROJECT_NAME_MISMATCH"
        ):
            authorize_manus_dispatch(**self.base(project_name="something else"))

    def test_implicit_connector_inheritance_is_blocked(self):
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_CONNECTORS_MUST_BE_EXPLICIT"
        ):
            authorize_manus_dispatch(**self.base(connectors_explicit=False))

    def test_account_level_hidden_connector_is_blocked(self):
        with self.assertRaises(ManusGovernanceError):
            authorize_manus_dispatch(
                **self.base(connectors=["github", "notion"])
            )

    def test_paid_profile_is_blocked_without_current_jay_override(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PAID_PROFILE_BLOCKED"
        ):
            authorize_manus_dispatch(**self.base(requested_profile="standard"))

    def test_route_that_cannot_pin_profile_is_blocked(self):
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PROFILE_SELECTOR_UNAVAILABLE"
        ):
            authorize_manus_dispatch(
                **self.base(route_supports_profile_selector=False)
            )

    def test_delegated_task_needs_current_authority(self):
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_ACTION_NOT_AUTHORIZED"
        ):
            authorize_manus_dispatch(
                **self.base(current_task_authorized=False)
            )

    def test_jaytec_core_change_cannot_be_self_authorized_by_manus(self):
        with self.assertRaisesRegex(
            ManusDispatchContractError, "MANUS_ACTION_NOT_AUTHORIZED"
        ):
            authorize_manus_dispatch(
                **self.base(
                    scope="jaytec_core_change",
                    authority_source="manus",
                )
            )

    def test_lite_dispatch_with_explicit_connectors_passes(self):
        auth = authorize_manus_dispatch(**self.base())
        self.assertEqual(auth.project_name, "MANUS")
        self.assertEqual(auth.profile.requested_profile.value, "lite")
        self.assertEqual(auth.connectors, ("github", "neon", "render"))

    def test_post_dispatch_profile_mismatch_fails(self):
        auth = authorize_manus_dispatch(**self.base())
        result = {
            "status": "SUCCESS",
            "evidence": ["verified output"],
            "verification": {
                "instruction_match_verified": True,
                "scope_verified": True,
                "evidence_verified": True,
                "no_unauthorized_side_effects": True,
                "duplicate_work_check_passed": True,
            },
        }
        with self.assertRaisesRegex(
            ManusProfilePolicyError, "MANUS_PROFILE_MISMATCH"
        ):
            verify_manus_dispatch_result(
                auth,
                observed_profile="standard",
                result=result,
            )

    def test_post_dispatch_unverified_success_fails(self):
        auth = authorize_manus_dispatch(**self.base())
        result = {
            "status": "SUCCESS",
            "evidence": ["claim only"],
            "verification": {
                "instruction_match_verified": True,
                "scope_verified": True,
                "evidence_verified": False,
                "no_unauthorized_side_effects": True,
                "duplicate_work_check_passed": True,
            },
        }
        with self.assertRaises(ManusGovernanceError):
            verify_manus_dispatch_result(
                auth,
                observed_profile="lite",
                result=result,
            )


if __name__ == "__main__":
    unittest.main()
