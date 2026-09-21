from __future__ import annotations

import json
import unittest

from manus_system_gateway import (
    ALLOWED_GATEWAY_CALLERS,
    ManusSystemGateway,
    ManusSystemGatewayError,
)


class FakeRuntime:
    def __init__(self):
        self.started = []
        self.continued = []
        self.readonly = []
        self.mutating_status_calls = []

    def start_task_idempotent(self, request_json, registry):
        payload = json.loads(request_json)
        self.started.append((payload, registry))
        return {
            "status": "STARTED",
            "requested_profile": "lite",
            "observed_profile_verified": True,
            "provider_task_id": "lite-task-1",
        }

    def continue_task_handoff(self, provider_task_id, **kwargs):
        self.continued.append((provider_task_id, kwargs))
        return {
            "status": "CONTINUED",
            "requested_profile": "lite",
            "observed_profile_verified": True,
        }

    def task_status_readonly(self, provider_task_id):
        self.readonly.append(provider_task_id)
        return {
            "status": "PENDING",
            "provider_task_id": provider_task_id,
            "requested_profile": "lite",
            "observed_profile": "lite",
            "read_only": True,
        }

    def task_status(self, provider_task_id):
        self.mutating_status_calls.append(provider_task_id)
        raise AssertionError("system gateway must not use mutating task_status")


def start_packet(caller="watch", *, mutation=False, authority="jaytec"):
    return json.dumps(
        {
            "caller": caller,
            "task": {
                "task_id": "SYS-MANUS-1",
                "objective": "Review one bounded JAYTEC item.",
                "scope": "jaytec_delegated_task",
                "authority_source": authority,
                "current_task_authorized": True,
                "allowed_actions": ["inspect", "diagnose", "report"],
                "connector_purposes": {},
                "connector_mutation_authorized": mutation,
                "required_context": {"gate": "G03"},
            },
        }
    )


class ManusSystemGatewayTests(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.registry = object()
        self.gateway = ManusSystemGateway(self.runtime, self.registry)

    def test_system_callers_share_one_lite_runtime_path(self):
        for caller in sorted(ALLOWED_GATEWAY_CALLERS):
            with self.subTest(caller=caller):
                out = self.gateway.start_task(start_packet(caller))
                self.assertEqual(out["manus_route"], "JAYTEC_MANUS_LITE_RUNTIME_V1")
                self.assertEqual(out["gateway_caller"], caller)
                self.assertEqual(out["result"]["requested_profile"], "lite")
        for payload, registry in self.runtime.started:
            self.assertEqual(payload["authority_source"], "jaytec")
            self.assertEqual(payload["required_context"]["manus_gateway_boundary"], "JAYTEC_MANUS_SYSTEM_GATEWAY_V1")
            self.assertIs(registry, self.registry)

    def test_manus_cannot_call_gateway_to_spawn_itself(self):
        with self.assertRaisesRegex(
            ManusSystemGatewayError, "MANUS_GATEWAY_CALLER_NOT_ALLOWLISTED"
        ):
            self.gateway.start_task(start_packet("manus"))

    def test_resources_cannot_become_callers(self):
        for caller in ("github", "neon", "render"):
            with self.subTest(caller=caller):
                with self.assertRaises(ManusSystemGatewayError):
                    self.gateway.start_task(start_packet(caller))

    def test_specialist_cannot_grant_connector_mutation_authority(self):
        for caller in ("deepseek", "nemo", "engineering_specialist", "research_specialist"):
            with self.subTest(caller=caller):
                with self.assertRaisesRegex(
                    ManusSystemGatewayError,
                    "MANUS_GATEWAY_MUTATION_AUTH_CALLER_BLOCKED",
                ):
                    self.gateway.start_task(start_packet(caller, mutation=True))

    def test_non_control_caller_cannot_self_authorize_unsafe_actions(self):
        packet = json.loads(start_packet("nemo"))
        packet["task"]["allowed_actions"] = ["inspect", "deploy"]
        with self.assertRaisesRegex(
            ManusSystemGatewayError,
            "MANUS_GATEWAY_NON_CONTROL_ACTION_BLOCKED",
        ):
            self.gateway.start_task(json.dumps(packet))

    def test_non_control_caller_cannot_request_write_connector_purpose(self):
        packet = json.loads(start_packet("deepseek"))
        packet["task"]["connector_purposes"] = {"github": "write"}
        with self.assertRaisesRegex(
            ManusSystemGatewayError,
            "MANUS_GATEWAY_NON_CONTROL_CONNECTOR_PURPOSE_BLOCKED",
        ):
            self.gateway.start_task(json.dumps(packet))

    def test_non_control_continue_cannot_expand_connector_scope_to_write(self):
        raw = json.dumps(
            {
                "caller": "nemo",
                "provider_task_id": "provider-task-1",
                "handoff_id": "handoff-write-blocked",
                "handoff_context": {"finding": "bounded"},
                "connector_purposes": {"github": "write"},
                "connector_mutation_authorized": False,
            }
        )
        with self.assertRaisesRegex(
            ManusSystemGatewayError,
            "MANUS_GATEWAY_NON_CONTROL_CONNECTOR_PURPOSE_BLOCKED",
        ):
            self.gateway.continue_task(raw)

    def test_supervising_control_plane_can_carry_existing_mutation_grant(self):
        for caller in ("chatgpt", "jaytec"):
            out = self.gateway.start_task(start_packet(caller, mutation=True))
            self.assertEqual(out["result"]["status"], "STARTED")
            payload = self.runtime.started[-1][0]
            self.assertTrue(payload["connector_mutation_authorized"])

    def test_non_jaytec_authority_source_is_rejected_not_rewritten(self):
        with self.assertRaisesRegex(
            ManusSystemGatewayError, "MANUS_GATEWAY_AUTHORITY_SOURCE_BLOCKED"
        ):
            self.gateway.start_task(start_packet("watch", authority="watch"))

    def test_system_wide_status_is_read_only(self):
        out = self.gateway.task_status("forge", "provider-task-1")
        self.assertTrue(out["read_only"])
        self.assertEqual(self.runtime.readonly, ["provider-task-1"])
        self.assertEqual(self.runtime.mutating_status_calls, [])

    def test_continue_returns_to_same_manus_task_through_jaytec(self):
        raw = json.dumps(
            {
                "caller": "nemo",
                "provider_task_id": "provider-task-1",
                "handoff_id": "handoff-1",
                "handoff_context": {"finding": "bounded"},
                "connector_purposes": {},
                "connector_mutation_authorized": False,
            }
        )
        out = self.gateway.continue_task(raw)
        self.assertEqual(out["result"]["status"], "CONTINUED")
        provider_id, kwargs = self.runtime.continued[0]
        self.assertEqual(provider_id, "provider-task-1")
        self.assertEqual(kwargs["authority_source"], "jaytec")
        self.assertTrue(kwargs["current_task_authorized"])
        self.assertEqual(
            kwargs["handoff_context"]["manus_gateway_caller"],
            "nemo",
        )


if __name__ == "__main__":
    unittest.main()
