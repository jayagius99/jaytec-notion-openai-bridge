import json
import unittest
from pathlib import Path

import dan_elastic_gemini_review as review
import dan_elastic_regression_v12 as regression
import dan_role_elastic_runtime_proof as role_proof


def reserve_packet():
    return {
        "workflow_id": review.WORKFLOW_ID,
        "specialist_plan": ["gemini"],
        "allowed_operations": ["research", "validate"],
        "max_retries": 0,
        "side_effect_policy": "none",
        "required_context": {
            "free_routes_exhausted": True,
            "free_routes_attempted": ["reviewer", "nemo"],
            "free_route_exhaustion_evidence": [
                "reviewer: current bounded route unavailable",
                "nemo: current bounded route unavailable",
            ],
            "gemini_specifically_required": True,
            "gemini_required_reason": (
                "A Gemini-specific independent hardening review is required after free routes are exhausted."
            ),
            "paid_reserve_authorized": True,
            "cost_policy": "PAID_BACKUP_ONLY",
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
        },
    }


class _FakeMessage:
    content = json.dumps(
        {
            "status": "PASS_WITH_HARDENING",
            "model": review.MODEL,
            "strengths": [],
            "risks": [],
            "recommendations": [],
            "acceptance_standard": [],
            "dan_means": "bounded role elasticity",
            "dan_does_not_mean": "unbounded authority",
            "residual_limits": [],
        }
    )


class _FakeChoice:
    message = _FakeMessage()
    finish_reason = "stop"


class _FakeResponse:
    choices = [_FakeChoice()]
    model = review.MODEL
    id = "test-response"


class _FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        return _FakeResponse()


class _FakeClient:
    def __init__(self):
        self.completions = _FakeCompletions()

        class _Chat:
            pass

        self.chat = _Chat()
        self.chat.completions = self.completions


class DanElasticGeminiReserveTests(unittest.TestCase):
    def test_staging_startup_has_no_paid_gemini_dan_import_side_effects(self):
        source = Path("staging_server.py").read_text(encoding="utf-8")
        self.assertNotIn("import dan_elastic_gemini_review", source)
        self.assertNotIn("import dan_elastic_regression_v12", source)
        self.assertNotIn("import dan_role_elastic_runtime_proof", source)

    def test_paid_gemini_dan_modules_have_no_environment_startup_autorun(self):
        review_source = Path("dan_elastic_gemini_review.py").read_text(encoding="utf-8")
        regression_source = Path("dan_elastic_regression_v12.py").read_text(encoding="utf-8")
        proof_source = Path("dan_role_elastic_runtime_proof.py").read_text(encoding="utf-8")
        self.assertNotIn("JAYTEC_DAN_ELASTIC_GEMINI_REVIEW_ON_START", review_source)
        self.assertNotIn("JAYTEC_DAN_V12_REGRESSION_ON_START", regression_source)
        self.assertNotIn("JAYTEC_DAN_PROOF_ON_START", proof_source)
        self.assertNotIn("_once()", review_source)
        self.assertNotIn("def _once", regression_source)
        self.assertNotIn("def _execute_once", proof_source)

    def test_ordinary_direct_call_is_blocked_before_provider(self):
        client = _FakeClient()
        bad = {
            "workflow_id": "GENERAL_RESEARCH",
            "specialist_plan": ["gemini"],
            "allowed_operations": ["research"],
            "max_retries": 0,
            "side_effect_policy": "none",
            "required_context": {},
        }
        with self.assertRaisesRegex(RuntimeError, "GEMINI_PAID_RESERVE_POLICY_BLOCKED"):
            review.run(bad, openrouter_client=client)
        self.assertEqual(0, client.completions.calls)

    def test_wrong_specialized_workflow_is_blocked_before_provider(self):
        client = _FakeClient()
        packet = reserve_packet()
        packet["workflow_id"] = "JAYTEC_PAID_GEMINI_RESERVE_OTHER_TASK"
        with self.assertRaisesRegex(RuntimeError, "dan_elastic_workflow_required"):
            review.run(packet, openrouter_client=client)
        self.assertEqual(0, client.completions.calls)

    def test_legacy_regression_direct_call_is_blocked_before_provider(self):
        client = _FakeClient()
        with self.assertRaisesRegex(RuntimeError, "GEMINI_PAID_RESERVE_POLICY_BLOCKED"):
            regression.run({}, openrouter_client=client)
        self.assertEqual(0, client.completions.calls)

    def test_legacy_role_proof_direct_call_is_blocked_before_provider(self):
        client = _FakeClient()
        with self.assertRaisesRegex(RuntimeError, "GEMINI_PAID_RESERVE_POLICY_BLOCKED"):
            role_proof.run({}, openrouter_client=client)
        self.assertEqual(0, client.completions.calls)

    def test_legacy_paid_dan_modules_accept_only_their_exact_reserve_workflows(self):
        regression_packet = reserve_packet()
        regression_packet["workflow_id"] = regression.WORKFLOW_ID
        self.assertIs(regression._validate_reserve_packet(regression_packet), regression_packet)

        proof_packet = reserve_packet()
        proof_packet["workflow_id"] = role_proof.WORKFLOW_ID
        self.assertIs(role_proof._validate_reserve_packet(proof_packet), proof_packet)

    def test_complete_paid_reserve_proof_can_reach_exact_gemini_once(self):
        client = _FakeClient()
        result = review.run(reserve_packet(), openrouter_client=client)
        self.assertEqual(1, client.completions.calls)
        self.assertEqual(review.MODEL, result["model"])
        self.assertTrue(result["paid_reserve_policy_enforced"])
        self.assertFalse(result["provider_fallback"])
        self.assertFalse(result["side_effects"])


if __name__ == "__main__":
    unittest.main()
