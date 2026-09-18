import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestration import ExecutionRegistry, EXPECTED_MODELS, execute_task_packet_core
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_ENGINEERING_MODEL,
    resolve_engineering_model,
)


def packet(key="migration-stress"):
    now = datetime.now(timezone.utc)
    return {
        "packet_version": "1.0",
        "task_id": "JAYTEC-MIGRATION-STRESS",
        "subtask_id": "ENGINEERING-SPECIALIST",
        "parent_task_id": "JAYTEC-MIGRATION-STRESS",
        "request": "migration stress validation",
        "intent": "prove provider-neutral engineering specialist compatibility",
        "workflow_id": "WORKFLOW_ARCHITECTURE_DECISION",
        "risk_level": "low",
        "required_context": {},
        "context_digests": {},
        "known_facts": [],
        "constraints": ["no side effects"],
        "specialist_plan": ["codex"],
        "allowed_operations": ["read", "analyze", "validate", "test"],
        "expected_output": "structured envelope",
        "validation_requirements": ["exact model identity", "no side effects"],
        "side_effect_policy": "none",
        "idempotency_key": key,
        "deadline": (now + timedelta(minutes=10)).isoformat(),
        "max_fanout": 1,
        "max_retries": 0,
        "return_schema_version": "1.0",
    }


class TestEngineeringMigrationGuard(unittest.TestCase):
    def test_primary_engineering_model_is_exact_56_sol(self):
        self.assertEqual("gpt-5.6-sol", EXPECTED_ENGINEERING_MODEL)
        self.assertEqual(EXPECTED_ENGINEERING_MODEL, EXPECTED_CODEX_MODEL)
        self.assertEqual("gpt-5.6-sol", EXPECTED_MODELS["codex"])

    def test_engineering_model_config_alias_is_fail_closed(self):
        self.assertEqual("gpt-5.6-sol", resolve_engineering_model({}))
        self.assertEqual("gpt-5.6-sol", resolve_engineering_model({"ENGINEERING_MODEL": "gpt-5.6-sol"}))
        self.assertEqual("gpt-5.6-sol", resolve_engineering_model({"CODEX_MODEL": "gpt-5.6-sol"}))
        self.assertEqual(
            "gpt-5.6-sol",
            resolve_engineering_model({
                "ENGINEERING_MODEL": "gpt-5.6-sol",
                "CODEX_MODEL": "gpt-5.6-sol",
            }),
        )
        with self.assertRaises(RuntimeError):
            resolve_engineering_model({
                "ENGINEERING_MODEL": "gpt-5.6-sol",
                "CODEX_MODEL": "gpt-5.3-codex",
            })

    def test_active_runtime_has_no_legacy_53_model_lock(self):
        active = [
            "specialist_adapters.py",
            "orchestration.py",
            "server.py",
            "staging_server.py",
            "staging_runtime_probe.py",
            ".github/workflows/orchestration-staging.yml",
            "render.yaml",
            "staging_engineering_provider_probe.py",
        ]
        for name in active:
            text = Path(name).read_text(encoding="utf-8")
            self.assertNotIn("gpt-5.3-codex", text, name)
            self.assertNotIn("GPT-5.3 CODEX ENGINEERING", text, name)

    def test_runtime_uses_canonical_engineering_role_and_no_obsolete_boot_probe(self):
        adapter = Path("specialist_adapters.py").read_text(encoding="utf-8")
        render = Path("render.yaml").read_text(encoding="utf-8")
        self.assertIn("JAYTEC ENGINEERING SPECIALIST — GPT-5.6 SOL", adapter)
        self.assertIn("ENGINEERING_MODEL", render)
        self.assertIn("staging_engineering_provider_probe.py", render)
        self.assertNotIn("staging_codex_429_diagnostic.py", render)

    def test_legacy_wire_key_still_dispatches_new_engineering_model(self):
        p = packet("wire-compat")
        out = execute_task_packet_core(
            p,
            {"codex": lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.6-sol",
                "findings": ["ok"],
                "evidence": ["synthetic"],
                "requested_operations": [],
                "side_effects_attempted": [],
            }},
            ExecutionRegistry(),
            sleep_fn=lambda _: None,
        )
        self.assertEqual("SUCCESS", out["overall_status"])
        self.assertEqual("gpt-5.6-sol", out["codex_result"]["model"])

    def test_old_53_model_can_never_silently_pass(self):
        for i in range(500):
            p = packet(f"reject-old-{i}")
            out = execute_task_packet_core(
                p,
                {"codex": lambda _: {
                    "status": "SUCCESS",
                    "model": "gpt-5.3-codex",
                    "findings": [],
                    "evidence": [],
                    "requested_operations": [],
                    "side_effects_attempted": [],
                }},
                ExecutionRegistry(),
                sleep_fn=lambda _: None,
            )
            self.assertEqual("FAILED_CLOSED", out["overall_status"])
            self.assertTrue(any("model_mismatch" in x for x in out["unresolved_items"]))

    def test_2000_clean_engineering_packets_remain_deterministic(self):
        for i in range(2000):
            p = packet(f"success-{i}")
            out = execute_task_packet_core(
                p,
                {"codex": lambda _: {
                    "status": "SUCCESS",
                    "model": "gpt-5.6-sol",
                    "findings": ["stable"],
                    "evidence": ["synthetic"],
                    "requested_operations": [],
                    "side_effects_attempted": [],
                }},
                ExecutionRegistry(),
                sleep_fn=lambda _: None,
            )
            self.assertEqual("SUCCESS", out["overall_status"])
            self.assertEqual([], out["side_effects_attempted"])
            self.assertEqual("gpt-5.6-sol", out["codex_result"]["model"])

    def test_replay_stress_never_reexecutes_worker(self):
        p = packet("replay-stress")
        reg = ExecutionRegistry()
        calls = {"n": 0}

        def worker(_):
            calls["n"] += 1
            return {
                "status": "SUCCESS",
                "model": "gpt-5.6-sol",
                "findings": ["stable"],
                "evidence": ["synthetic"],
                "requested_operations": [],
                "side_effects_attempted": [],
            }

        first = execute_task_packet_core(p, {"codex": worker}, reg, sleep_fn=lambda _: None)
        for _ in range(2000):
            replay = execute_task_packet_core(copy.deepcopy(p), {"codex": worker}, reg, sleep_fn=lambda _: None)
            self.assertEqual(first["execution_id"], replay["execution_id"])
            self.assertTrue(replay["usage_summary"]["idempotent_replay"])
        self.assertEqual(1, calls["n"])


if __name__ == "__main__":
    unittest.main()
