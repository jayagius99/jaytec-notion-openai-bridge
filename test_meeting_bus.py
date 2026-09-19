import json
import unittest
from types import SimpleNamespace

import meeting_bus


def valid_request(participant="sol"):
    return {
        "schema_version": "1.0",
        "scope": "JAYTEC_MEETING_ONLY",
        "meeting_id": "MEETING-20260920-001",
        "request_id": "REQ-" + participant + "-001",
        "participant": participant,
        "brief": "Review current JAYTEC state for a scheduled meeting. Advisory only.",
        "role_question": "Identify one material risk and one next focus.",
        "allowed_operations": ["analyze", "review", "challenge"],
        "side_effect_policy": "none",
        "max_output_tokens": 512,
        "notion_allowed": False,
        "work_allowed": False,
    }


def meeting_output():
    return {
        "status": "SUCCESS",
        "current_state_observations": ["state observed"],
        "evidence": ["evidence"],
        "risks": ["risk"],
        "disagreements_or_challenges": ["challenge"],
        "recommended_next_focus": ["focus"],
        "cost_efficiency_observations": ["cheap route"],
        "unresolved_questions": [],
        "confidence": "HIGH",
        "side_effects_attempted": [],
    }


class FakeRegistry:
    def __init__(self):
        self.values = {}

    def lookup(self, key, digest, now=None):
        existing = self.values.get(key)
        if existing is None:
            return None
        old_digest, value = existing
        if old_digest != digest:
            raise ValueError("CONFLICTING_DUPLICATE")
        return json.loads(json.dumps(value))

    def store(self, key, digest, value, now=None):
        existing = self.values.get(key)
        if existing is not None and existing[0] != digest:
            raise ValueError("CONFLICTING_DUPLICATE")
        self.values[key] = (digest, json.loads(json.dumps(value)))
        return value


class FakeSolResponses:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            model="gpt-5.6-sol",
            output_text=json.dumps(meeting_output()),
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150),
        )


class FakeSolClient:
    def __init__(self):
        self.responses = FakeSolResponses()


class FakeGeminiCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            model="google/gemini-3.1-pro-preview",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(meeting_output()))
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
            ),
        )


class FakeGeminiClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeGeminiCompletions())


class MeetingBusTests(unittest.TestCase):
    def test_non_meeting_scope_rejected(self):
        req = valid_request()
        req["scope"] = "GENERAL_JAYTEC"
        with self.assertRaises(meeting_bus.MeetingPolicyError):
            meeting_bus.validate_request(req)

    def test_side_effect_policy_rejected(self):
        req = valid_request()
        req["side_effect_policy"] = "staging_only"
        with self.assertRaises(meeting_bus.MeetingPolicyError):
            meeting_bus.validate_request(req)

    def test_non_meeting_operation_rejected(self):
        req = valid_request()
        req["allowed_operations"] = ["write"]
        with self.assertRaises(meeting_bus.MeetingPolicyError):
            meeting_bus.validate_request(req)

    def test_bus_disabled_by_default(self):
        with self.assertRaises(meeting_bus.MeetingPolicyError):
            meeting_bus.dispatch_request(
                valid_request(),
                registry=FakeRegistry(),
                sol_client=FakeSolClient(),
                enabled=False,
            )

    def test_sol_cost_lane_requires_enable_even_for_mock(self):
        original = meeting_bus.SOL_ENABLED
        meeting_bus.SOL_ENABLED = False
        try:
            with self.assertRaises(meeting_bus.MeetingPolicyError):
                meeting_bus.dispatch_request(
                    valid_request("sol"),
                    registry=FakeRegistry(),
                    sol_client=FakeSolClient(),
                    enabled=True,
                )
        finally:
            meeting_bus.SOL_ENABLED = original

    def test_sol_dispatch_and_idempotent_replay(self):
        original = meeting_bus.SOL_ENABLED
        meeting_bus.SOL_ENABLED = True
        try:
            registry = FakeRegistry()
            client = FakeSolClient()
            req = valid_request("sol")
            first = meeting_bus.dispatch_request(
                req,
                registry=registry,
                sol_client=client,
                enabled=True,
            )
            second = meeting_bus.dispatch_request(
                req,
                registry=registry,
                sol_client=client,
                enabled=True,
            )
            self.assertEqual(first["route"], "DIRECT_GITHUB_OIDC_MEETING_BUS")
            self.assertFalse(first["notion_used"])
            self.assertFalse(first["chatgpt_work_used"])
            self.assertEqual(first["provider_call_count"], 1)
            self.assertEqual(first["result"]["model"], "gpt-5.6-sol")
            self.assertTrue(second["idempotent_replay"])
            self.assertEqual(client.responses.calls, 1)
        finally:
            meeting_bus.SOL_ENABLED = original

    def test_conflicting_second_request_same_meeting_fails(self):
        original = meeting_bus.SOL_ENABLED
        meeting_bus.SOL_ENABLED = True
        try:
            registry = FakeRegistry()
            client = FakeSolClient()
            req = valid_request("sol")
            meeting_bus.dispatch_request(
                req,
                registry=registry,
                sol_client=client,
                enabled=True,
            )
            changed = dict(req)
            changed["brief"] = "Changed brief must not create a second Sol call."
            with self.assertRaises(meeting_bus.MeetingPolicyError):
                meeting_bus.dispatch_request(
                    changed,
                    registry=registry,
                    sol_client=client,
                    enabled=True,
                )
            self.assertEqual(client.responses.calls, 1)
        finally:
            meeting_bus.SOL_ENABLED = original

    def test_gemini_dispatch(self):
        registry = FakeRegistry()
        client = FakeGeminiClient()
        result = meeting_bus.dispatch_request(
            valid_request("gemini"),
            registry=registry,
            gemini_client=client,
            enabled=True,
        )
        self.assertEqual(
            result["result"]["model"],
            "google/gemini-3.1-pro-preview",
        )
        self.assertFalse(result["notion_used"])
        self.assertEqual(client.chat.completions.calls, 1)

    def test_specialist_side_effect_claim_rejected(self):
        bad = meeting_output()
        bad["side_effects_attempted"] = ["write"]
        client = FakeSolClient()
        client.responses.create = lambda **kwargs: SimpleNamespace(
            model="gpt-5.6-sol",
            output_text=json.dumps(bad),
            usage=None,
        )
        original = meeting_bus.SOL_ENABLED
        meeting_bus.SOL_ENABLED = True
        try:
            with self.assertRaises(meeting_bus.MeetingPolicyError):
                meeting_bus.dispatch_request(
                    valid_request("sol"),
                    registry=FakeRegistry(),
                    sol_client=client,
                    enabled=True,
                )
        finally:
            meeting_bus.SOL_ENABLED = original


if __name__ == "__main__":
    unittest.main()
