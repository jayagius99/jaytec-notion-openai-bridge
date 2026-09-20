import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import (
    EXPECTED_ENGINEERING_MODEL,
    build_engineering_dispatch,
)


GOOD = (
    '{"status":"SUCCESS","model":"gpt-5.6-sol",'
    '"findings":[],"evidence":[],"requested_operations":[],'
    '"side_effects_attempted":[]}'
)


class _Responses:
    def __init__(self, provider_model=EXPECTED_ENGINEERING_MODEL, include_model=True):
        self.provider_model = provider_model
        self.include_model = include_model
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        data = {"output_text": GOOD}
        if self.include_model:
            data["model"] = self.provider_model
        return SimpleNamespace(**data)


class _Client:
    def __init__(self, responses):
        self.responses = responses


def _dispatch(responses):
    return build_engineering_dispatch(
        openai_client=_Client(responses),
        engineering_model=EXPECTED_ENGINEERING_MODEL,
        circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
    )


class ObservedEngineeringModelIdentityTests(unittest.TestCase):
    def test_exact_observed_model_passes_and_is_recorded(self):
        responses = _Responses()
        result = _dispatch(responses)({"task_id": "T", "subtask_id": "S"})
        self.assertEqual(EXPECTED_ENGINEERING_MODEL, result["model"])
        self.assertEqual(
            EXPECTED_ENGINEERING_MODEL,
            result["bridge_diagnostics"]["provider_model"],
        )
        self.assertTrue(
            result["bridge_diagnostics"]["model_identity_observed"]
        )
        self.assertFalse(
            result["bridge_diagnostics"]["provider_fallbacks"]
        )

    def test_wrong_observed_model_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "model mismatch"):
            _dispatch(_Responses(provider_model="gpt-5.3-codex"))(
                {"task_id": "T", "subtask_id": "S"}
            )

    def test_missing_observed_model_fails_closed(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "provider model identity unavailable",
        ):
            _dispatch(_Responses(include_model=False))(
                {"task_id": "T", "subtask_id": "S"}
            )


if __name__ == "__main__":
    unittest.main()
