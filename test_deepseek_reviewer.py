import json
import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from deepseek_reviewer import (
    EXPECTED_DEEPSEEK_REVIEWER_MODEL,
    build_deepseek_security_review_dispatch,
)
from worker_json import WorkerJsonError


def _valid_result():
    return {
        "status": "SUCCESS",
        "model": EXPECTED_DEEPSEEK_REVIEWER_MODEL,
        "findings": ["No bypass observed in supplied evidence."],
        "evidence": ["Reviewed exact supplied packet."],
        "confidence": "HIGH",
        "conclusion": {
            "verdict": "PASS",
            "blockers": [],
            "required_changes": [],
            "weaknesses": [],
            "rationale": "No material weakness found in supplied scope.",
        },
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "requested_operations": [],
    }


class _NotFound(Exception):
    status_code = 404


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            model=item.get("provider_model", EXPECTED_DEEPSEEK_REVIEWER_MODEL),
            choices=[
                SimpleNamespace(
                    finish_reason=item.get("finish_reason", "stop"),
                    message=SimpleNamespace(content=item.get("content", "")),
                )
            ],
        )


class _FakeClient:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _dispatch(responses):
    client = _FakeClient(responses)
    dispatch = build_deepseek_security_review_dispatch(
        openrouter_client=client,
        model=EXPECTED_DEEPSEEK_REVIEWER_MODEL,
        timeout_s=30,
        circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
    )
    return client, dispatch


class TestDeepSeekSecurityReviewer(unittest.TestCase):
    def test_exact_model_and_strict_schema_are_forced(self):
        client, dispatch = _dispatch([
            {"content": json.dumps(_valid_result())}
        ])
        result = dispatch({"max_retries": 1, "objective": "review"})

        self.assertEqual("SUCCESS", result["status"])
        call = client.completions.calls[0]
        self.assertEqual(EXPECTED_DEEPSEEK_REVIEWER_MODEL, call["model"])
        self.assertEqual(0, call["temperature"])
        self.assertEqual(4096, call["max_tokens"])
        self.assertFalse(call["stream"])
        self.assertEqual("json_schema", call["response_format"]["type"])
        self.assertTrue(call["response_format"]["json_schema"]["strict"])
        self.assertFalse(call["extra_body"]["provider"]["allow_fallbacks"])
        self.assertTrue(call["extra_body"]["provider"]["require_parameters"])
        self.assertNotIn("reasoning", call["extra_body"])

    def test_empty_first_response_gets_exactly_one_format_retry(self):
        client, dispatch = _dispatch([
            {"content": ""},
            {"content": json.dumps(_valid_result())},
        ])
        result = dispatch({"max_retries": 1, "objective": "review"})

        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(2, len(client.completions.calls))
        self.assertNotIn(
            "previous transport result",
            client.completions.calls[0]["messages"][0]["content"],
        )
        self.assertIn(
            "previous transport result",
            client.completions.calls[1]["messages"][0]["content"],
        )
        self.assertEqual(2, result["bridge_diagnostics"]["attempt"])
        self.assertTrue(result["bridge_diagnostics"]["provider_fallbacks"])
        self.assertFalse(
            client.completions.calls[0]["extra_body"]["provider"]["allow_fallbacks"]
        )
        self.assertTrue(
            client.completions.calls[1]["extra_body"]["provider"]["allow_fallbacks"]
        )

    def test_second_empty_response_fails_closed(self):
        _, dispatch = _dispatch([
            {"content": ""},
            {"content": ""},
        ])
        with self.assertRaises(WorkerJsonError):
            dispatch({"max_retries": 1, "objective": "review"})

    def test_no_retry_when_budget_zero(self):
        client, dispatch = _dispatch([{"content": ""}])
        with self.assertRaises(WorkerJsonError):
            dispatch({"max_retries": 0, "objective": "review"})
        self.assertEqual(1, len(client.completions.calls))


    def test_provider_route_404_gets_one_same_model_fallback_attempt(self):
        client, dispatch = _dispatch([
            _NotFound("provider route unavailable"),
            {"content": json.dumps(_valid_result())},
        ])
        result = dispatch({"max_retries": 1, "objective": "review"})

        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(2, len(client.completions.calls))
        self.assertEqual(
            EXPECTED_DEEPSEEK_REVIEWER_MODEL,
            client.completions.calls[0]["model"],
        )
        self.assertEqual(
            EXPECTED_DEEPSEEK_REVIEWER_MODEL,
            client.completions.calls[1]["model"],
        )
        self.assertFalse(
            client.completions.calls[0]["extra_body"]["provider"]["allow_fallbacks"]
        )
        self.assertTrue(
            client.completions.calls[1]["extra_body"]["provider"]["allow_fallbacks"]
        )
        self.assertTrue(result["bridge_diagnostics"]["provider_fallbacks"])

    def test_non_404_provider_error_does_not_retry(self):
        client, dispatch = _dispatch([RuntimeError("boom")])
        with self.assertRaisesRegex(RuntimeError, "boom"):
            dispatch({"max_retries": 1, "objective": "review"})
        self.assertEqual(1, len(client.completions.calls))

    def test_wrong_provider_model_fails_closed(self):
        _, dispatch = _dispatch([
            {
                "provider_model": "some-other-model",
                "content": json.dumps(_valid_result()),
            }
        ])
        with self.assertRaisesRegex(RuntimeError, "provider_model_mismatch"):
            dispatch({"max_retries": 1, "objective": "review"})


    def test_missing_provider_model_fails_closed(self):
        class MissingModelCompletions(_FakeCompletions):
            def create(self, **kwargs):
                self.calls.append(kwargs)
                item = self.responses.pop(0)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason=item.get("finish_reason", "stop"),
                            message=SimpleNamespace(
                                content=item.get("content", "")
                            ),
                        )
                    ],
                )

        client = _FakeClient([])
        client.completions = MissingModelCompletions(
            [{"content": json.dumps(_valid_result())}]
        )
        client.chat = SimpleNamespace(completions=client.completions)
        dispatch = build_deepseek_security_review_dispatch(
            openrouter_client=client,
            model=EXPECTED_DEEPSEEK_REVIEWER_MODEL,
            timeout_s=30,
            circuit=CircuitBreaker(
                failure_threshold=3,
                reset_after_seconds=60,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "provider_model_unobservable"):
            dispatch({"max_retries": 0, "objective": "review"})

    def test_side_effect_claim_is_rejected(self):
        payload = _valid_result()
        payload["side_effects_attempted"] = ["write"]
        _, dispatch = _dispatch([{"content": json.dumps(payload)}])
        with self.assertRaisesRegex(RuntimeError, "side_effect_violation"):
            dispatch({"max_retries": 1, "objective": "review"})

    def test_model_configuration_cannot_silently_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "model_mismatch"):
            build_deepseek_security_review_dispatch(
                openrouter_client=_FakeClient([]),
                model="deepseek/deepseek-r1",
                timeout_s=30,
                circuit=CircuitBreaker(
                    failure_threshold=3,
                    reset_after_seconds=60,
                ),
            )


if __name__ == "__main__":
    unittest.main()
