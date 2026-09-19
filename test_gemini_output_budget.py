import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import (
    EXPECTED_GEMINI_MODEL,
    GEMINI_MAX_OUTPUT_TOKENS,
    build_gemini_dispatch,
)


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"status":"SUCCESS","model":"google/gemini-3.1-pro-preview","findings":[],"evidence":[],"confidence":"high","conclusion":{"verdict":"PASS"},"unresolved_items":[],"files_or_artifacts":[],"architecture_changes_required":[],"knowledge_writeback_proposal":[],"side_effects_attempted":[],"requested_operations":[]}'
                    )
                )
            ]
        )


class GeminiOutputBudgetTests(unittest.TestCase):
    def test_dispatch_has_hard_bounded_output_budget(self):
        completions = _Completions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        dispatch = build_gemini_dispatch(
            openrouter_client=client,
            gemini_model=EXPECTED_GEMINI_MODEL,
            gemini_timeout_s=30,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
        )
        dispatch({"task_id": "T", "subtask_id": "S"})
        self.assertEqual(4096, GEMINI_MAX_OUTPUT_TOKENS)
        self.assertEqual(
            GEMINI_MAX_OUTPUT_TOKENS,
            completions.kwargs["max_tokens"],
        )
        self.assertLessEqual(completions.kwargs["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
