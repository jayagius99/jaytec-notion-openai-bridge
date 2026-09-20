import unittest
from unittest import mock

import manus_adapter as ma
from manus_dispatch_contract import authorize_manus_preflight, ManusDispatchContractError
from manus_policy import ManusProfilePolicyError


class ManusNoNetworkPreflightTests(unittest.TestCase):
    def test_authorized_lite_preflight(self):
        auth = authorize_manus_preflight(
            connectors=["github"],
            connectors_explicit=True,
            scope="jaytec_delegated_task",
            authority_source="chatgpt",
            current_task_authorized=True,
            requested_profile="lite",
            route_supports_profile_selector=True,
        )
        self.assertEqual(auth.connectors, ("github",))
        self.assertTrue(auth.authority.allowed)
        self.assertEqual(auth.profile.requested_profile.value, "lite")

    def test_unauthorized_task_fails_before_network(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(client, "resolve_manus_project") as project_lookup,              mock.patch.object(client, "resolve_approved_connector_ids") as connector_lookup:
            with self.assertRaisesRegex(Exception, "MANUS_ACTION_NOT_AUTHORIZED"):
                client.prepare_route(
                    scope="jaytec_delegated_task",
                    authority_source="chatgpt",
                    current_task_authorized=False,
                    requested_profile="lite",
                )
        project_lookup.assert_not_called()
        connector_lookup.assert_not_called()

    def test_paid_profile_fails_before_network(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(client, "resolve_manus_project") as project_lookup,              mock.patch.object(client, "resolve_approved_connector_ids") as connector_lookup:
            with self.assertRaises(ManusProfilePolicyError):
                client.prepare_route(
                    scope="jaytec_delegated_task",
                    authority_source="chatgpt",
                    current_task_authorized=True,
                    requested_profile="standard",
                )
        project_lookup.assert_not_called()
        connector_lookup.assert_not_called()

    def test_blocked_connector_fails_before_network(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(client, "resolve_manus_project") as project_lookup,              mock.patch.object(client, "resolve_approved_connector_ids") as connector_lookup:
            with self.assertRaisesRegex(Exception, "MANUS_CONNECTOR_NOT_ALLOWLISTED"):
                client.prepare_route(
                    scope="jaytec_delegated_task",
                    authority_source="chatgpt",
                    current_task_authorized=True,
                    requested_profile="lite",
                    requested_connector_purposes={"notion": "read"},
                )
        project_lookup.assert_not_called()
        connector_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
