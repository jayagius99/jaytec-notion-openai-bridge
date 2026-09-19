import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import (
    EXPECTED_GEMINI_MODEL,
    GEMINI_MAX_OUTPUT_TOKENS,
    build_gemini_dispatch,
)


GOOD = '{"status":"SUCCESS","model":"google/gemini-3.1-pro-preview","findings":[],"evidence":[],"confidence":"high","conclusion":{"verdict":"PASS"},"unresolved_items":[],"files_or_artifacts":[],"architecture_changes_required":[],"knowledge_writeback_proposal":[],"side_effects_attempted":[],"requested_operations":[]}'


class _Completions:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = list(outputs or [GOOD])

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.outputs.pop(0)
        return SimpleNamespace(
            model=EXPECTED_GEMINI_MODEL,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ],
        )


def _dispatch(completions):
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return build_gemini_dispatch(
        openrouter_client=client,
        gemini_model=EXPECTED_GEMINI_MODEL,
        gemini_timeout_s=30,
        circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
    )


class GeminiOutputBudgetTests(unittest.TestCase):
    def test_dispatch_has_hard_bounded_output_budget_and_json_mode(self):
        completions = _Completions()
        result = _dispatch(completions)({"task_id": "T", "subtask_id": "S"})
        kwargs = completions.calls[0]
        self.assertEqual(4096, GEMINI_MAX_OUTPUT_TOKENS)
        self.assertEqual(GEMINI_MAX_OUTPUT_TOKENS, kwargs["max_tokens"])
        self.assertLessEqual(kwargs["max_tokens"], 4096)
        self.assertEqual({"type": "json_object"}, kwargs["response_format"])
        self.assertFalse(kwargs["extra_body"]["provider"]["allow_fallbacks"])
        self.assertEqual("SUCCESS", result["status"])
        self.assertFalse(result["bridge_diagnostics"]["provider_fallbacks"])

    def test_malformed_json_gets_exactly_one_format_retry_when_authorized(self):
        completions = _Completions(["not-json", GOOD])
        result = _dispatch(completions)(
            {"task_id": "T", "subtask_id": "S", "max_retries": 1}
        )
        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(2, len(completions.calls))
        retry_prompt = completions.calls[1]["messages"][0]["content"]
        self.assertIn("previous transport attempt", retry_prompt)
        self.assertIn("Return one compact valid JSON object only", retry_prompt)

    def test_malformed_json_does_not_retry_without_budget(self):
        completions = _Completions(["not-json"])
        with self.assertRaises(ValueError):
            _dispatch(completions)(
                {"task_id": "T", "subtask_id": "S", "max_retries": 0}
            )
        self.assertEqual(1, len(completions.calls))

    def test_provider_model_mismatch_fails_closed(self):
        class WrongModel(_Completions):
            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    model="other/model",
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(content=GOOD),
                        )
                    ],
                )

        with self.assertRaises(RuntimeError):
            _dispatch(WrongModel())(
                {"task_id": "T", "subtask_id": "S", "max_retries": 0}
            )


if __name__ == "__main__":
    unittest.main()
