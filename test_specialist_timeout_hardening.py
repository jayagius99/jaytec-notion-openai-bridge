import unittest

from circuit_breaker import CircuitBreaker
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    build_codex_dispatch,
    build_gemini_dispatch,
)


class FakeTimeoutError(RuntimeError):
    pass


class FakeCodexResponses:
    def __init__(self):
        self.timeout = None

    def create(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        raise FakeTimeoutError("request timed out")


class FakeCodexClient:
    def __init__(self):
        self.responses = FakeCodexResponses()


class FakeCompletions:
    def __init__(self):
        self.timeout = None

    def create(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        raise FakeTimeoutError("provider timeout")


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeGeminiClient:
    def __init__(self):
        self.chat = FakeChat()


class TestSpecialistTimeoutHardening(unittest.TestCase):
    def test_codex_timeout_is_bounded_and_normalized(self):
        client = FakeCodexClient()
        dispatch = build_codex_dispatch(
            openai_client=client,
            codex_model=EXPECTED_CODEX_MODEL,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
            codex_timeout_s=7.5,
        )
        with self.assertRaises(TimeoutError):
            dispatch({"task_id": "t", "subtask_id": "s", "allowed_operations": []})
        self.assertEqual(client.responses.timeout, 7.5)

    def test_gemini_timeout_is_bounded_and_normalized(self):
        client = FakeGeminiClient()
        dispatch = build_gemini_dispatch(
            openrouter_client=client,
            gemini_model=EXPECTED_GEMINI_MODEL,
            gemini_timeout_s=8.5,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
        )
        with self.assertRaises(TimeoutError):
            dispatch(
                {
                    "task_id": "t",
                    "subtask_id": "s",
                    "allowed_operations": [],
                    "max_retries": 0,
                }
            )
        self.assertEqual(client.chat.completions.timeout, 8.5)


if __name__ == "__main__":
    unittest.main()
