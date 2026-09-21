import json
import unittest
from decimal import Decimal
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_SOL_MODEL, build_sol_reserve_dispatch


class FakeCompletions:
    def __init__(self, provider_model=EXPECTED_SOL_MODEL):
        self.calls = 0
        self.last_kwargs = None
        self.provider_model = provider_model

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        payload = {
            "status": "SUCCESS",
            "model": EXPECTED_SOL_MODEL,
            "findings": ["reserve review complete"],
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
            model=self.provider_model,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(payload)),
                )
            ],
        )


class FakeClient:
    def __init__(self, provider_model=EXPECTED_SOL_MODEL):
        self.chat = SimpleNamespace(completions=FakeCompletions(provider_model))


class PaymentRequiredError(RuntimeError):
    status_code = 402


class FailingCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise PaymentRequiredError("payment required")


class FailingClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FailingCompletions())


def packet():
    return {
        "task_id": "T",
        "subtask_id": "S",
        "workflow_id": "JAYTEC_OWNER_SOL_EXPLICIT_REVIEW",
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "owner_explicit_sol_request": True,
        },
        "allowed_operations": ["analyze", "validate"],
        "max_retries": 0,
        "side_effect_policy": "none",
    }


class SolReserveZeroSpendTests(unittest.TestCase):
    def build(self, *, balances=("5.00", "4.98"), provider_model=EXPECTED_SOL_MODEL):
        client = FakeClient(provider_model)
        values = iter(Decimal(x) for x in balances)
        dispatch = build_sol_reserve_dispatch(
            gateway_client=client,
            gateway_api_key="test-key",
            circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=300),
            credit_balance_fn=lambda: next(values),
            reserve_enabled=True,
            zero_spend_attested=True,
            max_output_tokens=1200,
            max_input_bytes=64000,
            min_credit_usd=Decimal("0.25"),
            reasoning_effort="medium",
        )
        return client, dispatch

    def test_sol_model_identity_is_exact(self):
        self.assertEqual(EXPECTED_SOL_MODEL, "openai/gpt-5.6-sol")

    def test_disabled_reserve_fails_at_construction(self):
        with self.assertRaisesRegex(RuntimeError, "sol_reserve_disabled"):
            build_sol_reserve_dispatch(
                gateway_client=FakeClient(),
                gateway_api_key="test-key",
                circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=300),
                credit_balance_fn=lambda: Decimal("5"),
                reserve_enabled=False,
                zero_spend_attested=True,
            )

    def test_zero_spend_attestation_is_mandatory(self):
        with self.assertRaisesRegex(RuntimeError, "sol_zero_spend_attestation_required"):
            build_sol_reserve_dispatch(
                gateway_client=FakeClient(),
                gateway_api_key="test-key",
                circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=300),
                credit_balance_fn=lambda: Decimal("5"),
                reserve_enabled=True,
                zero_spend_attested=False,
            )

    def test_owner_explicit_request_is_mandatory(self):
        client, dispatch = self.build()
        p = packet()
        p["required_context"]["owner_explicit_sol_request"] = False
        with self.assertRaisesRegex(RuntimeError, "sol_owner_explicit_request_required"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_owner_workflow_namespace_is_mandatory(self):
        client, dispatch = self.build()
        p = packet()
        p["workflow_id"] = "JAYTEC_ENGINEERING_CODE_REVIEW"
        with self.assertRaisesRegex(RuntimeError, "sol_owner_workflow_required"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_retries_are_forbidden(self):
        client, dispatch = self.build()
        p = packet()
        p["max_retries"] = 1
        with self.assertRaisesRegex(RuntimeError, "sol_retries_forbidden"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_side_effects_are_forbidden(self):
        client, dispatch = self.build()
        p = packet()
        p["side_effect_policy"] = "staging_only"
        with self.assertRaisesRegex(RuntimeError, "sol_side_effects_forbidden"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_low_credit_fails_before_inference(self):
        client, dispatch = self.build(balances=("0.24",))
        with self.assertRaisesRegex(RuntimeError, "sol_free_credit_reserve_too_low"):
            dispatch(packet())
        self.assertEqual(client.chat.completions.calls, 0)

    def test_input_cap_fails_before_inference(self):
        client = FakeClient()
        dispatch = build_sol_reserve_dispatch(
            gateway_client=client,
            gateway_api_key="test-key",
            circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=300),
            credit_balance_fn=lambda: Decimal("5"),
            reserve_enabled=True,
            zero_spend_attested=True,
            max_input_bytes=4096,
        )
        p = packet()
        p["required_context"]["blob"] = "x" * 10000
        with self.assertRaisesRegex(RuntimeError, "sol_input_cap_exceeded"):
            dispatch(p)
        self.assertEqual(client.chat.completions.calls, 0)

    def test_success_is_openai_only_no_model_fallback_and_accounted(self):
        client, dispatch = self.build()
        result = dispatch(packet())
        self.assertEqual(result["model"], EXPECTED_SOL_MODEL)
        self.assertEqual(client.chat.completions.calls, 1)
        kwargs = client.chat.completions.last_kwargs
        self.assertEqual(kwargs["model"], EXPECTED_SOL_MODEL)
        gateway = kwargs["extra_body"]["providerOptions"]["gateway"]
        self.assertEqual(gateway["only"], ["openai"])
        self.assertTrue(gateway["disallowPromptTraining"])
        self.assertNotIn("models", kwargs)
        self.assertNotIn("models", gateway)
        self.assertEqual(kwargs["max_completion_tokens"], 1200)
        self.assertEqual(result["bridge_diagnostics"]["credit_balance_before_usd"], "5.00")
        self.assertEqual(result["bridge_diagnostics"]["credit_balance_after_usd"], "4.98")
        self.assertEqual(result["bridge_diagnostics"]["credit_used_usd"], "0.02")

    def test_wrong_provider_model_fails_closed(self):
        client, dispatch = self.build(provider_model="openai/gpt-5.4")
        with self.assertRaisesRegex(RuntimeError, "sol_provider_response_model_mismatch"):
            dispatch(packet())
        self.assertEqual(client.chat.completions.calls, 1)


    def test_credit_preflight_failure_never_calls_model(self):
        client = FakeClient()
        dispatch = build_sol_reserve_dispatch(
            gateway_client=client,
            gateway_api_key="test-key",
            circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=300),
            credit_balance_fn=lambda: (_ for _ in ()).throw(RuntimeError("credit check unavailable")),
            reserve_enabled=True,
            zero_spend_attested=True,
        )
        with self.assertRaises(RuntimeError):
            dispatch(packet())
        self.assertEqual(client.chat.completions.calls, 0)

    def test_payment_required_is_single_attempt_and_opens_circuit(self):
        client = FailingClient()
        dispatch = build_sol_reserve_dispatch(
            gateway_client=client,
            gateway_api_key="test-key",
            circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=300),
            credit_balance_fn=lambda: Decimal("5"),
            reserve_enabled=True,
            zero_spend_attested=True,
        )
        with self.assertRaises(PaymentRequiredError):
            dispatch(packet())
        self.assertEqual(client.chat.completions.calls, 1)
        with self.assertRaises(RuntimeError):
            dispatch(packet())
        self.assertEqual(client.chat.completions.calls, 1)


if __name__ == "__main__":
    unittest.main()
