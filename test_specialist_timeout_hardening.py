import unittest

from circuit_breaker import CircuitBreaker
from orchestration import ProviderUnavailableError, RateLimitError
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    build_codex_dispatch,
    build_gemini_dispatch,
)


class FakeProviderError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class FakeCodexResponses:
    def __init__(self, exc=None):
        self.timeout = None
        self.exc = exc or FakeProviderError("request timed out")

    def create(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        raise self.exc


class FakeCodexClient:
    def __init__(self, exc=None):
        self.responses = FakeCodexResponses(exc)


class FakeCompletions:
    def __init__(self, exc=None):
        self.timeout = None
        self.exc = exc or FakeProviderError("provider timeout")

    def create(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        raise self.exc


class FakeChat:
    def __init__(self, exc=None):
        self.completions = FakeCompletions(exc)


class FakeGeminiClient:
    def __init__(self, exc=None):
        self.chat = FakeChat(exc)


def sol_packet():
    return {
        "task_id": "t",
        "subtask_id": "s",
        "workflow_id": "JAYTEC_ENGINEERING_TEST",
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
        },
        "allowed_operations": [],
    }


class TestSpecialistTimeoutHardening(unittest.TestCase):
    def test_codex_timeout_is_bounded_and_normalized(self):
        client = FakeCodexClient()
        dispatch = build_codex_dispatch(
            openai_client=client,
            codex_model=EXPECTED_CODEX_MODEL,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
            codex_timeout_s=7.5,
            provider_mode="BOUNDED_SOL_ONLY",
        )
        with self.assertRaises(TimeoutError):
            dispatch(sol_packet())
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

    def test_429_is_normalized_to_jaytec_rate_limit(self):
        client = FakeCodexClient(FakeProviderError("too many requests", status_code=429))
        dispatch = build_codex_dispatch(
            openai_client=client,
            codex_model=EXPECTED_CODEX_MODEL,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
            codex_timeout_s=5,
            provider_mode="BOUNDED_SOL_ONLY",
        )
        with self.assertRaises(RateLimitError):
            dispatch(sol_packet())

    def test_5xx_is_normalized_to_provider_unavailable(self):
        client = FakeGeminiClient(FakeProviderError("upstream unavailable", status_code=503))
        dispatch = build_gemini_dispatch(
            openrouter_client=client,
            gemini_model=EXPECTED_GEMINI_MODEL,
            gemini_timeout_s=5,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
        )
        with self.assertRaises(ProviderUnavailableError):
            dispatch(
                {
                    "task_id": "t",
                    "subtask_id": "s",
                    "allowed_operations": [],
                    "max_retries": 0,
                }
            )


if __name__ == "__main__":
    unittest.main()
