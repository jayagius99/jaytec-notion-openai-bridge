import unittest
from datetime import datetime, timedelta, timezone

from orchestration import (
    CreditTopupRequiredError,
    ExecutionRegistry,
    execute_task_packet_core,
)
from specialist_adapters import _credit_exhaustion_details


def packet():
    now = datetime.now(timezone.utc)
    return {
        "packet_version": "1.0",
        "task_id": "CREDIT-TEST",
        "subtask_id": "CREDIT-TEST-G",
        "request": "verify credit topup blocker",
        "intent": "verify fail closed provider credit handling",
        "workflow_id": "CREDIT_TOPUP_POLICY_TEST",
        "risk_level": "low",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["research", "validate"],
        "expected_output": "blocking topup signal",
        "validation_requirements": ["no retries after credit exhaustion"],
        "side_effect_policy": "none",
        "idempotency_key": "credit-policy-test",
        "deadline": (now + timedelta(minutes=5)).isoformat(),
        "max_fanout": 1,
        "max_retries": 3,
        "return_schema_version": "1.0",
    }


class FakeProviderError(RuntimeError):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ProviderCreditTopupTests(unittest.TestCase):
    def test_credit_failure_is_blocking_and_not_retried(self):
        calls = {"n": 0}
        sleeps = []

        def dispatcher(_):
            calls["n"] += 1
            raise CreditTopupRequiredError(
                "OpenRouter",
                blocked_work="Gemini review",
                details={"status_code": 402},
            )

        out = execute_task_packet_core(
            packet(),
            {"gemini": dispatcher},
            ExecutionRegistry(),
            sleep_fn=lambda value: sleeps.append(value),
        )
        self.assertEqual(1, calls["n"])
        self.assertEqual([], sleeps)
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("credit_topup_required:OpenRouter", out["unresolved_items"])
        self.assertEqual(1, len(out["credit_topup_required"]))
        blocker = out["credit_topup_required"][0]
        self.assertTrue(blocker["required"])
        self.assertEqual("OpenRouter", blocker["provider"])
        self.assertEqual("BLOCKING", blocker["importance"])
        self.assertIn("Top up OpenRouter credits", blocker["action_required"])

    def test_http_402_is_credit_exhaustion(self):
        details = _credit_exhaustion_details(
            FakeProviderError("Payment Required", status_code=402)
        )
        self.assertIsNotNone(details)
        self.assertEqual(402, details["status_code"])

    def test_insufficient_quota_is_credit_exhaustion(self):
        details = _credit_exhaustion_details(
            FakeProviderError(
                "request rejected",
                status_code=429,
                body={"error": {"code": "insufficient_quota", "type": "insufficient_quota"}},
            )
        )
        self.assertIsNotNone(details)
        self.assertEqual("insufficient_quota", details["error_code"])

    def test_ordinary_rate_limit_is_not_credit_exhaustion(self):
        details = _credit_exhaustion_details(
            FakeProviderError("rate limit exceeded", status_code=429)
        )
        self.assertIsNone(details)


if __name__ == "__main__":
    unittest.main()
