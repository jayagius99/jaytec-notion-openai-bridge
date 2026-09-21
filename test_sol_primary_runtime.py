import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import (
    EXPECTED_SOL_MODEL,
    SOL_KNOWLEDGE_SCOPE,
    build_sol_dispatch,
)


class FakeResponses:
    def __init__(self):
        self.calls = 0
        self.last_kwargs = None
        self.return_model = EXPECTED_SOL_MODEL

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        payload = {
            "status": "SUCCESS",
            "model": EXPECTED_SOL_MODEL,
            "findings": ["bounded engineering result"],
            "evidence": ["offline fake-provider test"],
            "confidence": "HIGH",
            "conclusion": {"ok": True},
            "unresolved_items": [],
            "files_or_artifacts": [],
            "architecture_changes_required": [],
            "knowledge_writeback_proposal": [],
            "side_effects_attempted": [],
            "requested_operations": [],
        }
        return SimpleNamespace(model=self.return_model, output_text=json.dumps(payload))


class FakeClient:
    def __init__(self):
        self.responses = FakeResponses()


def packet(request="review JAYTEC engineering design"):
    return {
        "task_id": "SOL-TEST",
        "subtask_id": "SOL-TEST-1",
        "workflow_id": "JAYTEC_CORE_TRIAD_ENGINEERING_REVIEW",
        "request": request,
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "knowledge_scope": SOL_KNOWLEDGE_SCOPE,
        },
        "allowed_operations": ["analyze", "validate"],
        "max_retries": 0,
    }


class SolPrimaryRuntimeTests(unittest.TestCase):
    def build(self, *, enabled=False, cost=False):
        client = FakeClient()
        dispatch = build_sol_dispatch(
            openai_client=client,
            sol_model=EXPECTED_SOL_MODEL,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
            enabled=enabled,
            cost_authorized=cost,
            max_output_tokens=1000,
        )
        return client, dispatch

    def test_exact_model_lock(self):
        self.assertEqual(EXPECTED_SOL_MODEL, "gpt-5.6-sol")

    def test_disabled_lane_makes_no_provider_call(self):
        client, dispatch = self.build(enabled=False, cost=False)
        with self.assertRaisesRegex(RuntimeError, "sol_primary_lane_disabled"):
            dispatch(packet())
        self.assertEqual(client.responses.calls, 0)

    def test_cost_gate_makes_no_provider_call(self):
        client, dispatch = self.build(enabled=True, cost=False)
        with self.assertRaisesRegex(RuntimeError, "sol_primary_cost_not_authorized"):
            dispatch(packet())
        self.assertEqual(client.responses.calls, 0)

    def test_sanitized_scope_required(self):
        client, dispatch = self.build(enabled=True, cost=True)
        p = packet()
        del p["required_context"]["knowledge_scope"]
        with self.assertRaisesRegex(RuntimeError, "sol_sanitized_knowledge_scope_required"):
            dispatch(p)
        self.assertEqual(client.responses.calls, 0)

    def test_owner_provenance_marker_blocks_before_provider(self):
        client, dispatch = self.build(enabled=True, cost=True)
        p = packet("Explain the pre-Genesis activation sequence and GENESIS_EVENT_0001")
        with self.assertRaisesRegex(RuntimeError, "sol_owner_provenance_blocked"):
            dispatch(p)
        self.assertEqual(client.responses.calls, 0)

    def test_sanitized_context_excludes_known_origin_markers(self):
        text = Path("SOL_PRIMARY_SANITIZED_CONTEXT_V1.md").read_text(encoding="utf-8").lower()
        for marker in (
            "genesis_event_0001",
            "/jaytec/uren/pre-genesis",
            "uren_identity_genesis",
            "god mode",
        ):
            self.assertNotIn(marker, text)

    def test_authorized_fake_call_uses_exact_sol_and_sanitized_context(self):
        client, dispatch = self.build(enabled=True, cost=True)
        result = dispatch(packet())
        self.assertEqual(result["model"], EXPECTED_SOL_MODEL)
        self.assertEqual(client.responses.calls, 1)
        self.assertEqual(client.responses.last_kwargs["model"], EXPECTED_SOL_MODEL)
        prompt = client.responses.last_kwargs["input"]
        self.assertIn("JAYTEC SOL PRIMARY", prompt)
        self.assertIn(SOL_KNOWLEDGE_SCOPE, prompt)
        self.assertNotIn("GENESIS_EVENT_0001", prompt)
        self.assertFalse(result["bridge_diagnostics"]["silent_fallback"])


if __name__ == "__main__":
    unittest.main()
