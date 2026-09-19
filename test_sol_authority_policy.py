import json
import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_CODEX_MODEL, build_codex_dispatch


class FakeCompletions:
    def __init__(self):
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        payload = {
            "status": "SUCCESS",
            "model": EXPECTED_CODEX_MODEL,
            "findings": ["completed requested engineering analysis"],
            "evidence": [],
            "confidence": "HIGH",
            "conclusion": {"ok": True},
            "unresolved_items": [],
            "files_or_artifacts": [],
            "architecture_changes_required": [],
            "knowledge_writeback_proposal": [],
            "side_effects_attempted": [],
            "requested_operations": [],
        }
        return SimpleNamespace(
            model=EXPECTED_CODEX_MODEL,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(payload)),
                )
            ],
        )


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


def packet(workflow_id="JAYTEC_V2_EXECUTION_DISPATCH_CONTRACT"):
    return {
        "task_id": "T",
        "subtask_id": "S",
        "workflow_id": workflow_id,
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
        },
        "allowed_operations": ["analyze", "validate"],
        "max_retries": 0,
    }


class EngineeringAuthorityPolicyTests(unittest.TestCase):
    def build(self, mode="OPENROUTER_FREE_PRIMARY"):
        client = FakeClient()
        dispatch = build_codex_dispatch(
            openai_client=client,
            codex_model=EXPECTED_CODEX_MODEL,
            circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
            provider_mode=mode,
        )
        return client, dispatch

    def test_exact_model_is_free_nemotron(self):
        self.assertEqual(EXPECTED_CODEX_MODEL, "nvidia/nemotron-3-ultra-550b-a55b:free")

    def test_wrong_provider_mode_fails_at_construction(self):
        with self.assertRaisesRegex(ValueError, "unsupported engineering provider mode"):
            self.build("BOUNDED_SOL_ONLY")

    def test_non_authorized_workflow_blocks_provider_call(self):
        client, dispatch = self.build()
        with self.assertRaisesRegex(RuntimeError, "engineering_workflow_not_authorized"):
            dispatch(packet("UNRELATED_WORKFLOW"))
        self.assertEqual(client.chat.completions.calls, 0)

    def test_missing_chatgpt_authority_blocks_provider_call(self):
        client, dispatch = self.build()
        p = packet()
        p["required_context"] = {"specialist_authority": "SUBORDINATE"}
        with self.assertRaisesRegex(RuntimeError, "engineering_chatgpt_authority_required"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_specialist_cannot_be_authority(self):
        client, dispatch = self.build()
        p = packet()
        p["required_context"]["specialist_authority"] = "AUTONOMOUS"
        with self.assertRaisesRegex(RuntimeError, "engineering_specialist_must_be_subordinate"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_v2_engineering_allowed_under_chatgpt_authority(self):
        client, dispatch = self.build()
        result = dispatch(packet("JAYTEC_V2_EXECUTION_DISPATCH_CONTRACT"))
        self.assertEqual(result["model"], EXPECTED_CODEX_MODEL)
        self.assertEqual(client.chat.completions.calls, 1)

    def test_general_engineering_task_allowed_when_chatgpt_assigns_it(self):
        client, dispatch = self.build()
        result = dispatch(packet("JAYTEC_ENGINEERING_CODE_REVIEW"))
        self.assertEqual(result["model"], EXPECTED_CODEX_MODEL)
        self.assertEqual(client.chat.completions.calls, 1)

    def test_owner_sol_wire_does_not_leak_into_free_engineer(self):
        client, dispatch = self.build()
        with self.assertRaisesRegex(RuntimeError, "engineering_workflow_not_authorized"):
            dispatch(packet("JAYTEC_OWNER_SOL_EXPLICIT_REVIEW"))
        self.assertEqual(client.chat.completions.calls, 0)

    def test_output_tokens_are_hard_capped(self):
        client, dispatch = self.build()
        result = dispatch(packet("JAYTEC_ENGINEERING_CODE_REVIEW"))
        self.assertEqual(result["model"], EXPECTED_CODEX_MODEL)
        self.assertEqual(client.chat.completions.last_kwargs["max_tokens"], 2000)
        self.assertFalse(
            client.chat.completions.last_kwargs["extra_body"]["provider"]["allow_fallbacks"]
        )

    def test_retry_budget_above_one_is_rejected_before_provider_call(self):
        client, dispatch = self.build()
        p = packet("JAYTEC_ENGINEERING_CODE_REVIEW")
        p["max_retries"] = 2
        with self.assertRaisesRegex(RuntimeError, "engineering_retry_budget_exceeded"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)


if __name__ == "__main__":
    unittest.main()
