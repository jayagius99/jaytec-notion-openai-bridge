import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from manus_policy import ManusProfile, ManusProfilePolicyError
from orchestration import ExecutionRegistry
from manus_governance import specialist_request
from manus_runtime import (
    MANUS_RESULT_JSON_SCHEMA,
    ManusLiteRuntime,
    ManusRuntimeError,
    parse_start_request,
    start_request_identity,
    validate_manus_structured_output_schema,
)


def start_payload(**overrides):
    value = {
        "task_id": "task-1",
        "objective": "Inspect the supplied bounded evidence and report findings.",
        "scope": "jaytec_delegated_task",
        "authority_source": "jaytec",
        "current_task_authorized": True,
        "allowed_actions": ["inspect", "report"],
        "connector_purposes": {},
        "connector_mutation_authorized": False,
        "required_context": {"checkpoint": "cp-1"},
        "constraints": ["no external side effects"],
        "reference_ids": ["cp-1"],
        "title": "Bounded Manus task",
    }
    value.update(overrides)
    return json.dumps(value)


class FakeClient:
    def __init__(self, *, observed_profile="lite", task_status="running", result=None):
        self.observed_profile = observed_profile
        self.task_status = task_status
        self.result = result
        self.prepare_calls = []
        self.stopped = []
        self.created_prompt = None
        self.create_count = 0

    def prepare_route(self, **kwargs):
        self.prepare_calls.append(dict(kwargs))
        profile = SimpleNamespace(requested_profile=ManusProfile.LITE)
        auth = SimpleNamespace(
            project_id="project-manus",
            connectors=tuple(kwargs.get("requested_connector_purposes", {}).keys()),
            profile=profile,
        )
        return SimpleNamespace(
            authorization=auth,
            connector_permissions=tuple(
                (k, v) for k, v in kwargs.get("requested_connector_purposes", {}).items()
            ),
        )

    def create_task(self, route, content, *, title=None, structured_output_schema=None):
        self.create_count += 1
        self.created_prompt = content
        self.created_schema = structured_output_schema
        self.created_title = title
        return {"task_id": "provider-123"}

    def task_detail(self, task_id):
        return {
            "task": {
                "id": task_id,
                "status": self.task_status,
                "project_id": "project-manus",
                "agent_profile": self.observed_profile,
                "title": "test",
            }
        }

    def resolve_manus_project(self):
        return ("project-manus", "MANUS")

    def list_messages(self, task_id, *, limit=100):
        if self.result is None:
            return {"messages": []}
        return {
            "messages": [
                {
                    "type": "structured_output_result",
                    "structured_output_result": {
                        "success": True,
                        "value": self.result,
                    },
                }
            ]
        }

    def stop_task(self, task_id):
        self.stopped.append(task_id)
        return {"ok": True}


def verified_success():
    facets = [
        "instruction_match_verified",
        "scope_verified",
        "evidence_verified",
        "no_unauthorized_side_effects",
        "duplicate_work_check_passed",
    ]
    return {
        "status": "SUCCESS",
        "summary": "Completed bounded inspection.",
        "evidence": [
            {
                "kind": "test",
                "source": "unit-test",
                "reference": "test:verified_success",
                "observed_at": "2026-09-20T13:55:00Z",
                "claim": "All completion facets were checked.",
                "supports": facets,
            }
        ],
        "changes_made": [],
        "unresolved_items": [],
        "specialist_requests": [],
        "verification": {name: True for name in facets},
    }


ROOT = Path(__file__).resolve().parent


class ManusLiteRuntimeTests(unittest.TestCase):
    def test_runtime_schema_matches_manus_strict_subset(self):
        validate_manus_structured_output_schema(MANUS_RESULT_JSON_SCHEMA)
        self.assertEqual(
            MANUS_RESULT_JSON_SCHEMA["properties"]["specialist_requests"]["items"]["type"],
            "string",
        )

    def test_bare_nested_object_is_rejected_before_network(self):
        bad = {
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "items": {"type": "object"},
                }
            },
            "required": ["requests"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(
            ManusRuntimeError,
            "MANUS_STRUCTURED_SCHEMA_OBJECT_PROPERTIES_INVALID",
        ):
            validate_manus_structured_output_schema(bad)

    def test_structured_schema_adversarial_matrix_fails_locally(self):
        cases = {
            "missing_additional_properties": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            "required_mismatch": {
                "type": "object",
                "properties": {
                    "x": {"type": "string"},
                    "y": {"type": "string"},
                },
                "required": ["x"],
                "additionalProperties": False,
            },
            "unsupported_keyword": {
                "type": "object",
                "properties": {
                    "x": {"type": "string", "maxLength": 3},
                },
                "required": ["x"],
                "additionalProperties": False,
            },
            "array_without_items": {
                "type": "object",
                "properties": {"x": {"type": "array"}},
                "required": ["x"],
                "additionalProperties": False,
            },
            "non_object_root": {
                "type": "array",
                "items": {"type": "string"},
            },
        }
        for name, schema in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ManusRuntimeError):
                    validate_manus_structured_output_schema(schema)

    def test_specialist_request_json_string_is_validated_after_completion(self):
        request = specialist_request(
            parent_task_id="task-1",
            specialist="engineer",
            objective="Review one bounded software issue.",
            reason="Independent specialist input is required.",
            required_context={"reference": "issue-59"},
        )
        result = verified_success()
        result["specialist_requests"] = [json.dumps(request, sort_keys=True)]
        client = FakeClient(
            observed_profile="lite",
            task_status="stopped",
            result=result,
        )
        out = ManusLiteRuntime(client).task_status("provider-123")
        self.assertEqual(out["status"], "VERIFIED_COMPLETE")

    def test_profile_selection_fields_are_impossible_at_runtime_boundary(self):
        for field in (
            "profile",
            "requested_profile",
            "agent_profile",
            "explicit_paid_override",
            "paid_override_authority",
            "model",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ManusRuntimeError,
                    "MANUS_RUNTIME_PROFILE_SELECTION_FORBIDDEN",
                ):
                    parse_start_request(start_payload(**{field: "standard"}))

    def test_idempotency_identity_is_stable_and_scope_sensitive(self):
        first_key, first_hash = start_request_identity(start_payload())
        reordered = json.dumps(json.loads(start_payload()), sort_keys=True)
        second_key, second_hash = start_request_identity(reordered)
        self.assertEqual(first_key, "manus:task-1")
        self.assertEqual((first_key, first_hash), (second_key, second_hash))

        _, connector_hash = start_request_identity(
            start_payload(connector_purposes={"github": "inspect"})
        )
        self.assertNotEqual(first_hash, connector_hash)

    def test_current_task_authority_is_required(self):
        with self.assertRaisesRegex(
            ManusRuntimeError,
            "MANUS_RUNTIME_CURRENT_AUTH_REQUIRED",
        ):
            parse_start_request(start_payload(current_task_authorized=False))

    def test_start_hardcodes_lite_and_returns_verified_identity(self):
        client = FakeClient()
        runtime = ManusLiteRuntime(client)
        result = runtime.start_task(
            start_payload(connector_purposes={"github": "inspect"})
        )
        self.assertEqual(result["status"], "STARTED")
        self.assertEqual(result["requested_profile"], "lite")
        self.assertTrue(result["observed_profile_verified"])
        self.assertEqual(
            client.prepare_calls[0]["requested_profile"],
            "lite",
        )
        self.assertNotIn("standard", client.created_prompt.casefold())
        self.assertIn("JAYTEC MANUS TASK PACKET", client.created_prompt)

    def test_idempotent_start_replays_without_duplicate_provider_task(self):
        client = FakeClient()
        runtime = ManusLiteRuntime(client)
        registry = ExecutionRegistry()
        first = runtime.start_task_idempotent(start_payload(), registry)
        second = runtime.start_task_idempotent(start_payload(), registry)
        self.assertEqual(first["provider_task_id"], "provider-123")
        self.assertEqual(second["provider_task_id"], "provider-123")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(client.create_count, 1)

    def test_same_task_id_with_changed_request_fails_closed(self):
        client = FakeClient()
        runtime = ManusLiteRuntime(client)
        registry = ExecutionRegistry()
        runtime.start_task_idempotent(start_payload(), registry)
        with self.assertRaisesRegex(
            ManusRuntimeError,
            "MANUS_RUNTIME_CONFLICTING_DUPLICATE",
        ):
            runtime.start_task_idempotent(
                start_payload(objective="Different objective"),
                registry,
            )
        self.assertEqual(client.create_count, 1)

    def test_status_rejects_and_stops_non_lite_task(self):
        client = FakeClient(observed_profile="standard", task_status="running")
        runtime = ManusLiteRuntime(client)
        with self.assertRaisesRegex(
            ManusProfilePolicyError,
            "MANUS_PROFILE_MISMATCH",
        ):
            runtime.task_status("provider-123")
        self.assertEqual(client.stopped, ["provider-123"])

    def test_status_returns_pending_for_verified_lite_running_task(self):
        client = FakeClient(observed_profile="lite", task_status="running")
        result = ManusLiteRuntime(client).task_status("provider-123")
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(result["observed_profile"], "lite")

    def test_status_verifies_completed_structured_result(self):
        client = FakeClient(
            observed_profile="lite",
            task_status="stopped",
            result=verified_success(),
        )
        result = ManusLiteRuntime(client).task_status("provider-123")
        self.assertEqual(result["status"], "VERIFIED_COMPLETE")
        self.assertEqual(result["result"]["status"], "SUCCESS")

    def test_completed_task_without_structured_result_fails_closed(self):
        client = FakeClient(observed_profile="lite", task_status="stopped")
        result = ManusLiteRuntime(client).task_status("provider-123")
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertEqual(result["reason"], "MANUS_STRUCTURED_RESULT_MISSING")

    def test_production_surface_reuses_v2_dispatch_authority_gate(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("def _manus_dispatch_boundary()", source)
        self.assertIn(
            "evaluate_dispatch_boundary(runtime_mode=RUNTIME_MODE)",
            source,
        )
        self.assertIn("def manus_start_task(request_json: str)", source)
        self.assertIn("def manus_task_status(provider_task_id: str)", source)

    def test_staging_surface_exposes_same_lite_runtime(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        self.assertIn("ManusLiteRuntime", source)
        self.assertIn("def manus_start_task(request_json: str)", source)
        self.assertIn("def manus_task_status(provider_task_id: str)", source)
        self.assertIn('"manus_profile_policy": "lite_only_no_exceptions"', source)

    def test_persistent_staging_blueprint_remains_manus_credential_free(self):
        source = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertNotIn("MANUS_API_KEY", source)
        self.assertNotIn("JAYTEC_MANUS_PROJECT_ID", source)

    def test_policy_names_only_jaytec_owned_runtime_as_supported_execution_path(self):
        source = (ROOT / "JAYTEC_COMMAND_POLICY.md").read_text(encoding="utf-8")
        self.assertIn("JAYTEC_MANUS_LITE_RUNTIME_V1", source)
        self.assertIn("manus_start_task(request_json)", source)
        self.assertIn("manus_task_status(provider_task_id)", source)
        self.assertIn("must never substitute the generic ChatGPT Manus", source)


if __name__ == "__main__":
    unittest.main()
