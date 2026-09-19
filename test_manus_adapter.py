import io
import json
import unittest
import urllib.error
from unittest import mock

import manus_adapter as ma
from manus_dispatch_contract import authorize_manus_dispatch
from manus_policy import ManusProfilePolicyError


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _Response:
    status = 200
    headers = _Headers({"x-request-id": "req-safe"})

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return self.payload


def _route():
    auth = authorize_manus_dispatch(
        project_id="project-manus",
        expected_project_id="project-manus",
        project_name="MANUS",
        connectors=["github", "neon", "render"],
        connectors_explicit=True,
        scope="jaytec_delegated_task",
        authority_source="chatgpt",
        current_task_authorized=True,
        requested_profile="lite",
        route_supports_profile_selector=True,
    )
    return ma.BoundManusRoute(
        authorization=auth,
        connector_ids=("gh-id", "neon-id", "render-id"),
        connector_permissions=(
            ("github", "read"),
            ("neon", "inspect"),
            ("render", "diagnose"),
        ),
    )


class ManusAdapterTests(unittest.TestCase):
    def test_key_required(self):
        with self.assertRaises(ma.ManusError):
            ma.ManusClient(api_key="")

    def test_secret_only_in_request_header_not_result(self):
        seen = {}

        def fake(req, timeout):
            seen["key"] = req.get_header("X-manus-api-key")
            return _Response({"ok": True, "data": {"id": "u1", "credit_usage": 2}})

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            client = ma.ManusClient(api_key="secret-test-key")
            result = client._request("GET", "user.me")
        self.assertEqual(seen["key"], "secret-test-key")
        self.assertNotIn("secret-test-key", repr(result))
        self.assertEqual(result.credit_usage, 2)

    def test_credit_error_10091_fails_closed_without_retry(self):
        payload = json.dumps(
            {
                "ok": False,
                "error": {
                    "code": 10091,
                    "message": "You don't have enough credits",
                },
            }
        ).encode()
        error = urllib.error.HTTPError(
            "https://example.invalid",
            400,
            "bad",
            _Headers(),
            io.BytesIO(payload),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error) as call:
            with self.assertRaises(ma.ManusInsufficientCredits):
                ma.ManusClient(api_key="x").task_detail("t1")
        self.assertEqual(call.call_count, 1)

    def test_message_size_is_bounded_before_network(self):
        with mock.patch("urllib.request.urlopen") as call:
            with self.assertRaises(ma.ManusError) as ctx:
                ma.ManusClient(api_key="x")._governed_message(
                    _route(),
                    "x" * (ma.MANUS_MAX_MESSAGE_CHARS + 1),
                )
        self.assertEqual(str(ctx.exception), "MESSAGE_TOO_LARGE")
        call.assert_not_called()

    def test_invalid_json_is_rejected(self):
        class Bad(_Response):
            def read(self, _limit=-1):
                return b"not-json"

        with mock.patch("urllib.request.urlopen", return_value=Bad({})):
            with self.assertRaises(ma.ManusError) as ctx:
                ma.ManusClient(api_key="x").user_me()
        self.assertEqual(str(ctx.exception), "MANUS_RESPONSE_INVALID_JSON")

    def test_project_resolution_requires_exact_single_manus_project(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client,
            "list_projects",
            return_value={"data": [{"id": "p1", "name": "MANUS"}]},
        ):
            self.assertEqual(client.resolve_manus_project(), ("p1", "MANUS"))

        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client, "list_projects", return_value={"data": []}
        ):
            with self.assertRaisesRegex(ma.ManusError, "MANUS_PROJECT_NOT_FOUND"):
                client.resolve_manus_project()

        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client,
            "list_projects",
            return_value={
                "data": [
                    {"id": "p1", "name": "MANUS"},
                    {"id": "p2", "name": "manus"},
                ]
            },
        ):
            with self.assertRaisesRegex(ma.ManusError, "MANUS_PROJECT_AMBIGUOUS"):
                client.resolve_manus_project()

    def test_connector_resolution_allowlists_only_github_neon_render(self):
        client = ma.ManusClient(api_key="x")
        installed = {
            "data": [
                {"id": "gh", "name": "GitHub"},
                {"id": "neon", "name": "Neon Connect"},
                {"id": "render", "name": "Render Connect"},
                {"id": "notion", "name": "Notion"},
                {"id": "openrouter", "name": "OpenRouter API Connect"},
            ]
        }
        with mock.patch.object(client, "list_connectors", return_value=installed):
            names, ids = client.resolve_approved_connector_ids(["github", "neon", "render"])
        self.assertEqual(names, ("github", "neon", "render"))
        self.assertEqual(ids, ("gh", "neon", "render"))
        self.assertNotIn("notion", ids)
        self.assertNotIn("openrouter", ids)

    def test_missing_approved_connector_fails_closed(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(
            client,
            "list_connectors",
            return_value={
                "data": [
                    {"id": "gh", "name": "GitHub"},
                    {"id": "neon", "name": "Neon Connect"},
                ]
            },
        ):
            with self.assertRaisesRegex(
                ma.ManusError, "MANUS_APPROVED_CONNECTOR_MISSING:render"
            ):
                client.resolve_approved_connector_ids(["github", "neon", "render"])

    def test_zero_connector_route_does_not_resolve_account_defaults(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client, "resolve_manus_project", return_value=("project-manus", "MANUS")
        ), mock.patch.object(
            client, "list_connectors"
        ) as connector_list:
            route = client.prepare_route(
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                requested_profile="lite",
            )
        self.assertEqual(route.connector_ids, ())
        self.assertEqual(route.connector_permissions, ())
        self.assertEqual(route.authorization.connectors, ())
        connector_list.assert_not_called()

    def test_connector_mutation_requires_separate_current_authority(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client, "resolve_manus_project", return_value=("project-manus", "MANUS")
        ):
            with self.assertRaisesRegex(
                ma.ManusError, "MANUS_CONNECTOR_MUTATION_AUTH_REQUIRED"
            ):
                client.prepare_route(
                    scope="jaytec_delegated_task",
                    authority_source="chatgpt",
                    current_task_authorized=True,
                    requested_profile="lite",
                    requested_connector_purposes={"github": "write"},
                )

    def test_connector_mutation_cannot_be_authorized_by_jaytec_itself(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client, "resolve_manus_project", return_value=("project-manus", "MANUS")
        ):
            with self.assertRaisesRegex(
                ma.ManusError, "MANUS_CONNECTOR_MUTATION_AUTH_REQUIRED"
            ):
                client.prepare_route(
                    scope="jaytec_delegated_task",
                    authority_source="jaytec",
                    current_task_authorized=True,
                    connector_mutation_authorized=True,
                    requested_profile="lite",
                    requested_connector_purposes={"github": "write"},
                )

    def test_connector_mutation_with_fresh_chatgpt_authority_is_allowed(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client, "resolve_manus_project", return_value=("project-manus", "MANUS")
        ), mock.patch.object(
            client,
            "resolve_approved_connector_ids",
            return_value=(("github",), ("gh",)),
        ):
            route = client.prepare_route(
                scope="jaytec_delegated_task",
                authority_source="chatgpt",
                current_task_authorized=True,
                connector_mutation_authorized=True,
                requested_profile="lite",
                requested_connector_purposes={"github": "write"},
            )
        self.assertEqual(route.connector_permissions, (("github", "write"),))
        self.assertEqual(route.connector_ids, ("gh",))

    def test_prepare_route_cannot_select_paid_profile(self):
        client = ma.ManusClient(api_key="x")
        with mock.patch.object(ma, "JAYTEC_MANUS_PROJECT_ID", ""), mock.patch.object(
            client, "resolve_manus_project", return_value=("project-manus", "MANUS")
        ), mock.patch.object(
            client,
            "resolve_approved_connector_ids",
            return_value=(("github", "neon", "render"), ("gh", "neon", "render")),
        ):
            with self.assertRaisesRegex(
                ManusProfilePolicyError, "MANUS_PAID_PROFILE_BLOCKED"
            ):
                client.prepare_route(
                    scope="jaytec_delegated_task",
                    authority_source="chatgpt",
                    current_task_authorized=True,
                    requested_profile="standard",
                )

    def test_governed_message_always_injects_role_and_directive(self):
        message = ma.ManusClient._governed_message(_route(), "Return a test result.")
        self.assertIn("BOUNDED AUTOMATION SPECIALIST", message)
        self.assertIn("JAYTEC_MANUS_GOVERNANCE_V1", message)
        self.assertIn("CURRENT DELEGATED TASK", message)
        self.assertIn("Return a test result.", message)

    def test_create_task_explicitly_pins_lite_project_and_connector_ids(self):
        client = ma.ManusClient(api_key="x")
        route = _route()
        captured = {}

        def fake_request(method, endpoint, *, params=None, payload=None):
            if endpoint == "task.create":
                captured["payload"] = payload
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={"ok": True, "task_id": "task-1"},
                )
            if endpoint == "task.detail":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={
                        "ok": True,
                        "task": {
                            "id": "task-1",
                            "project_id": "project-manus",
                            "agent_profile": "manus-1.6-lite",
                        },
                    },
                )
            self.fail(f"unexpected endpoint {endpoint}")

        with mock.patch.object(client, "_request", side_effect=fake_request):
            created = client.create_task(route, "Do a harmless contract check.")

        self.assertEqual(created["task_id"], "task-1")
        payload = captured["payload"]
        self.assertEqual(payload["project_id"], "project-manus")
        self.assertEqual(payload["agent_profile"], "lite")
        self.assertEqual(
            payload["message"]["connectors"],
            ["gh-id", "neon-id", "render-id"],
        )
        self.assertIn("JAYTEC_MANUS_GOVERNANCE_V1", payload["message"]["content"])

    def test_create_task_profile_mismatch_stops_and_fails(self):
        client = ma.ManusClient(api_key="x")
        route = _route()
        calls = []

        def fake_request(method, endpoint, *, params=None, payload=None):
            calls.append(endpoint)
            if endpoint == "task.create":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={"ok": True, "task_id": "task-2"},
                )
            if endpoint == "task.detail":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={
                        "ok": True,
                        "task": {
                            "id": "task-2",
                            "project_id": "project-manus",
                            "agent_profile": "manus-1.6",
                        },
                    },
                )
            if endpoint == "task.stop":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={"ok": True},
                )
            self.fail(f"unexpected endpoint {endpoint}")

        with mock.patch.object(client, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(
                ManusProfilePolicyError, "MANUS_PROFILE_MISMATCH"
            ):
                client.create_task(route, "Harmless.")

        self.assertIn("task.stop", calls)

    def test_send_message_reasserts_lite_and_connector_override(self):
        client = ma.ManusClient(api_key="x")
        route = _route()
        captured = {}

        def fake_request(method, endpoint, *, params=None, payload=None):
            if endpoint == "task.detail":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={
                        "ok": True,
                        "task": {
                            "id": "task-3",
                            "project_id": "project-manus",
                            "agent_profile": "manus-1.6-lite",
                        },
                    },
                )
            if endpoint == "task.sendMessage":
                captured["payload"] = payload
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={"ok": True, "task_id": "task-3"},
                )
            self.fail(f"unexpected endpoint {endpoint}")

        with mock.patch.object(client, "_request", side_effect=fake_request):
            client.send_message(route, "task-3", "Continue safely.")

        payload = captured["payload"]
        self.assertEqual(payload["agent_profile"], "lite")
        self.assertEqual(
            payload["message"]["connectors"],
            ["gh-id", "neon-id", "render-id"],
        )
        self.assertIn("JAYTEC_MANUS_GOVERNANCE_V1", payload["message"]["content"])

    def test_send_message_with_no_connectors_clears_existing_set(self):
        auth = authorize_manus_dispatch(
            project_id="project-manus",
            expected_project_id="project-manus",
            project_name="MANUS",
            connectors=[],
            connectors_explicit=True,
            scope="jaytec_delegated_task",
            authority_source="chatgpt",
            current_task_authorized=True,
            requested_profile="lite",
            route_supports_profile_selector=True,
        )
        route = ma.BoundManusRoute(
            authorization=auth,
            connector_ids=(),
            connector_permissions=(),
        )
        client = ma.ManusClient(api_key="x")
        captured = {}

        def fake_request(method, endpoint, *, params=None, payload=None):
            if endpoint == "task.detail":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={
                        "ok": True,
                        "task": {
                            "id": "task-clear",
                            "project_id": "project-manus",
                            "agent_profile": "manus-1.6-lite",
                        },
                    },
                )
            if endpoint == "task.sendMessage":
                captured["payload"] = payload
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={"ok": True, "task_id": "task-clear"},
                )
            self.fail(f"unexpected endpoint {endpoint}")

        with mock.patch.object(client, "_request", side_effect=fake_request):
            client.send_message(route, "task-clear", "No connector use.")

        self.assertTrue(captured["payload"]["clear_connectors"])
        self.assertNotIn("connectors", captured["payload"]["message"])

    def test_old_non_lite_task_is_never_continued(self):
        client = ma.ManusClient(api_key="x")
        route = _route()

        def fake_request(method, endpoint, *, params=None, payload=None):
            if endpoint == "task.detail":
                return ma.ManusResponse(
                    endpoint=endpoint,
                    status_code=200,
                    body={
                        "ok": True,
                        "task": {
                            "id": "old",
                            "project_id": "project-manus",
                            "agent_profile": "manus-1.6",
                        },
                    },
                )
            self.fail("sendMessage must not be called for non-Lite task")

        with mock.patch.object(client, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(
                ManusProfilePolicyError, "MANUS_PROFILE_MISMATCH"
            ):
                client.send_message(route, "old", "Do not run.")


if __name__ == "__main__":
    unittest.main()
