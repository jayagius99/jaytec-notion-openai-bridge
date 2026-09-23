import unittest
from datetime import datetime, timedelta, timezone

from circuit_breaker import CircuitBreaker
from gemini_paid_reserve import (
    GEMINI_PAID_RESERVE_COST_POLICY,
    gemini_paid_reserve_errors,
)
from orchestration import validate_packet
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch


def packet():
    now = datetime.now(timezone.utc)
    return {
        "packet_version": "1.0",
        "task_id": "GEMINI-RESERVE-TEST",
        "subtask_id": "GEMINI-RESERVE-TEST-1",
        "request": "Perform a bounded research review.",
        "intent": "validate paid reserve routing",
        "workflow_id": "GENERAL_RESEARCH",
        "risk_level": "low",
        "required_context": {},
        "context_digests": {},
        "known_facts": [],
        "constraints": ["no side effects"],
        "specialist_plan": ["gemini"],
        "allowed_operations": ["research", "validate"],
        "expected_output": "structured review",
        "validation_requirements": ["exact model"],
        "side_effect_policy": "none",
        "idempotency_key": "gemini-reserve-test",
        "deadline": (now + timedelta(minutes=10)).isoformat(),
        "max_fanout": 1,
        "max_retries": 0,
        "return_schema_version": "1.0",
    }


def authorize_paid_reserve(p):
    p["workflow_id"] = "JAYTEC_PAID_GEMINI_RESERVE_TEST"
    p["required_context"] = {
        "free_routes_exhausted": True,
        "free_routes_attempted": ["reviewer"],
        "free_route_exhaustion_evidence": [
            "reviewer: exact free route unavailable or insufficient for this bounded task"
        ],
        "gemini_specifically_required": True,
        "gemini_required_reason": (
            "The remaining question specifically requires Gemini after the free reviewer route was exhausted."
        ),
        "paid_reserve_authorized": True,
        "cost_policy": GEMINI_PAID_RESERVE_COST_POLICY,
        "authority_controller": "CHATGPT_OPENAI_LEAD",
        "specialist_authority": "SUBORDINATE",
    }
    return p


class _NeverCallCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        raise AssertionError("provider must not be called")


class _NeverCallClient:
    def __init__(self):
        self.completions = _NeverCallCompletions()

        class _Chat:
            pass

        self.chat = _Chat()
        self.chat.completions = self.completions


class TestGeminiPaidReservePolicy(unittest.TestCase):
    def test_normal_gemini_request_is_blocked(self):
        errors = gemini_paid_reserve_errors(packet())
        self.assertIn("gemini_paid_reserve_workflow_required", errors)
        self.assertIn("gemini_paid_reserve_free_routes_exhausted_required", errors)
        self.assertIn("gemini_paid_reserve_gemini_specifically_required_required", errors)
        self.assertIn("gemini_paid_reserve_paid_reserve_authorized_required", errors)

    def test_free_routes_must_include_deepseek_reviewer_attempt(self):
        p = authorize_paid_reserve(packet())
        p["required_context"]["free_routes_attempted"] = ["nemo"]
        errors = gemini_paid_reserve_errors(p)
        self.assertIn("gemini_paid_reserve_deepseek_attempt_required", errors)

    def test_specific_gemini_need_is_mandatory(self):
        p = authorize_paid_reserve(packet())
        p["required_context"]["gemini_specifically_required"] = False
        p["required_context"]["gemini_required_reason"] = ""
        errors = gemini_paid_reserve_errors(p)
        self.assertIn(
            "gemini_paid_reserve_gemini_specifically_required_required",
            errors,
        )
        self.assertIn("gemini_paid_reserve_specific_reason_required", errors)

    def test_paid_authority_is_mandatory(self):
        p = authorize_paid_reserve(packet())
        p["required_context"]["paid_reserve_authorized"] = False
        errors = gemini_paid_reserve_errors(p)
        self.assertIn("gemini_paid_reserve_paid_reserve_authorized_required", errors)

    def test_chatgpt_control_authority_is_mandatory(self):
        p = authorize_paid_reserve(packet())
        p["required_context"]["authority_controller"] = "WORKER"
        p["required_context"]["specialist_authority"] = "SELF_AUTHORIZED"
        errors = gemini_paid_reserve_errors(p)
        self.assertIn("gemini_paid_reserve_chatgpt_authority_required", errors)
        self.assertIn("gemini_paid_reserve_specialist_must_be_subordinate", errors)

    def test_gemini_cannot_share_fanout_with_free_routes(self):
        p = authorize_paid_reserve(packet())
        p["specialist_plan"] = ["reviewer", "gemini"]
        p["max_fanout"] = 2
        errors = gemini_paid_reserve_errors(p)
        self.assertIn("gemini_paid_reserve_must_be_only_specialist", errors)

    def test_retries_and_side_effects_are_forbidden(self):
        p = authorize_paid_reserve(packet())
        p["max_retries"] = 1
        p["side_effect_policy"] = "staging_only"
        errors = gemini_paid_reserve_errors(p)
        self.assertIn("gemini_paid_reserve_retries_forbidden", errors)
        self.assertIn("gemini_paid_reserve_side_effects_forbidden", errors)

    def test_complete_terminal_reserve_proof_validates(self):
        p = authorize_paid_reserve(packet())
        self.assertEqual((), gemini_paid_reserve_errors(p))
        self.assertTrue(validate_packet(p).ok)

    def test_deepseek_reviewer_is_not_subject_to_paid_gemini_gate(self):
        p = packet()
        p["specialist_plan"] = ["reviewer"]
        self.assertEqual((), gemini_paid_reserve_errors(p))

    def test_direct_adapter_call_is_blocked_before_provider(self):
        client = _NeverCallClient()
        dispatch = build_gemini_dispatch(
            openrouter_client=client,
            gemini_model=EXPECTED_GEMINI_MODEL,
            gemini_timeout_s=30,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "GEMINI_PAID_RESERVE_POLICY_BLOCKED",
        ):
            dispatch(packet())
        self.assertEqual(0, client.completions.calls)


if __name__ == "__main__":
    unittest.main()
