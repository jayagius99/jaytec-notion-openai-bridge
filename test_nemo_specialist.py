from __future__ import annotations

import json
import types
import unittest

from circuit_breaker import CircuitBreaker
from nemo_specialist import EXPECTED_NEMO_MODEL, build_nemo_dispatch


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(response))


def response(payload, *, model=EXPECTED_NEMO_MODEL):
    message = types.SimpleNamespace(content=json.dumps(payload))
    choice = types.SimpleNamespace(message=message, finish_reason="stop")
    return types.SimpleNamespace(model=model, choices=[choice])


def packet():
    return {
        "task_id": "FORGE-GENESIS-ACTIVATION-001",
        "subtask_id": "sr-test",
        "workflow_id": "JAYTEC_WATCH_SPECIALIST_NEMO_V1",
        "allowed_operations": [],
        "max_retries": 0,
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
        },
    }


def success_payload():
    return {
        "status": "SUCCESS",
        "model": EXPECTED_NEMO_MODEL,
        "findings": [],
        "evidence": [],
        "confidence": "high",
        "conclusion": {"ok": True},
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "requested_operations": [],
    }


class NemoSpecialistTests(unittest.TestCase):
    def dispatch(self, provider_response):
        client = FakeClient(provider_response)
        dispatch = build_nemo_dispatch(
            openrouter_client=client,
            model=EXPECTED_NEMO_MODEL,
            timeout_s=30,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
        )
        return client, dispatch

    def test_exact_model_and_no_provider_fallback(self):
        client, dispatch = self.dispatch(response(success_payload()))
        out = dispatch(packet())
        self.assertEqual(out["model"], EXPECTED_NEMO_MODEL)
        call = client.chat.completions.calls[0]
        self.assertEqual(call["model"], EXPECTED_NEMO_MODEL)
        self.assertFalse(call["extra_body"]["provider"]["allow_fallbacks"])
        self.assertTrue(call["extra_body"]["provider"]["require_parameters"])

    def test_wrong_provider_model_fails_closed(self):
        _, dispatch = self.dispatch(response(success_payload(), model="other/model"))
        with self.assertRaisesRegex(RuntimeError, "provider_model_mismatch"):
            dispatch(packet())

    def test_missing_authority_context_fails_before_provider(self):
        client, dispatch = self.dispatch(response(success_payload()))
        bad = packet()
        bad["required_context"] = {}
        with self.assertRaisesRegex(RuntimeError, "controller_authority"):
            dispatch(bad)
        self.assertEqual(client.chat.completions.calls, [])

    def test_side_effect_result_fails_closed(self):
        bad = success_payload()
        bad["side_effects_attempted"] = ["write"]
        _, dispatch = self.dispatch(response(bad))
        with self.assertRaisesRegex(RuntimeError, "side_effect"):
            dispatch(packet())

    def test_model_configuration_cannot_silently_substitute(self):
        with self.assertRaisesRegex(RuntimeError, "nemo_model_mismatch"):
            build_nemo_dispatch(
                openrouter_client=FakeClient(response(success_payload())),
                model="nvidia/other",
                timeout_s=30,
                circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
            )


if __name__ == "__main__":
    unittest.main()
