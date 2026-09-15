import copy
import unittest
from datetime import datetime, timedelta, timezone

from orchestration import (
    ExecutionRegistry,
    ProviderUnavailableError,
    RateLimitError,
    execute_task_packet_core,
    parse_packet_json,
    redact,
    validate_packet,
)


def base_packet(now=None):
    now = now or datetime.now(timezone.utc)
    return {
        "packet_version": "1.0",
        "task_id": "JAYTEC-2026-0001",
        "subtask_id": "JAYTEC-2026-0001-C2",
        "parent_task_id": "JAYTEC-2026-0001",
        "request": "harmless staging validation",
        "intent": "validate orchestration core",
        "workflow_id": "WORKFLOW_ARCHITECTURE_DECISION",
        "risk_level": "low",
        "required_context": {"architecture": "test"},
        "context_digests": {},
        "known_facts": [],
        "constraints": ["no production writes"],
        "specialist_plan": ["gemini", "codex"],
        "allowed_operations": ["research", "validate", "code_staging"],
        "expected_output": "structured envelope",
        "validation_requirements": ["ids preserved"],
        "side_effect_policy": "staging_only",
        "idempotency_key": "idem-001",
        "deadline": (now + timedelta(minutes=10)).isoformat(),
        "max_fanout": 2,
        "max_retries": 2,
        "return_schema_version": "1.0",
    }


class TestOrchestration(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(validate_packet(base_packet()).ok)

    def test_malformed_json(self):
        packet, errors = parse_packet_json('{"task_id": "x"')
        self.assertIsNone(packet)
        self.assertTrue(errors[0].startswith("malformed_json:"))

    def test_unknown_fields_fail_closed(self):
        p = base_packet(); p["surprise"] = True
        self.assertTrue(any(e.startswith("unknown_fields:") for e in validate_packet(p).errors))

    def test_missing_task_id(self):
        p = base_packet(); del p["task_id"]
        self.assertIn("missing:task_id", validate_packet(p).errors)

    def test_missing_deadline(self):
        p = base_packet(); del p["deadline"]
        self.assertIn("missing:deadline", validate_packet(p).errors)

    def test_unknown_specialist(self):
        p = base_packet(); p["specialist_plan"] = ["manus"]
        self.assertTrue(any(e.startswith("unknown_specialist") for e in validate_packet(p).errors))

    def test_oversized_context(self):
        p = base_packet(); p["required_context"] = {"x": "a" * 300000}
        self.assertIn("oversized_context", validate_packet(p).errors)

    def test_unauthorized_side_effect(self):
        p = base_packet(); p["allowed_operations"] = ["production_write"]
        self.assertTrue(any(e.startswith("unauthorized_operation") for e in validate_packet(p).errors))

    def test_expired_deadline(self):
        now = datetime.now(timezone.utc)
        p = base_packet(now); p["deadline"] = (now - timedelta(seconds=1)).isoformat()
        self.assertIn("deadline_expired", validate_packet(p, now=now).errors)

    def test_successful_fan_in_and_deterministic_order(self):
        p = base_packet()
        def codex(_): return {"status": "SUCCESS", "model": "gpt-5.3-codex", "findings": ["c"], "evidence": [], "conclusion": {"ok": True}}
        def gemini(_): return {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": ["g"], "evidence": [], "conclusion": {"ok": True}}
        out = execute_task_packet_core(p, {"codex": codex, "gemini": gemini}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("SUCCESS", out["overall_status"])
        self.assertEqual(["codex", "gemini"], [x["specialist"] for x in out["worker_trace"]])

    def test_one_specialist_failure(self):
        p = base_packet()
        out = execute_task_packet_core(p, {
            "codex": lambda _: {"status": "SUCCESS", "model": "gpt-5.3-codex", "findings": [], "evidence": []},
            "gemini": lambda _: {"status": "FAILED_CLOSED", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": []},
        }, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("PARTIAL_SUCCESS", out["overall_status"])

    def test_both_fail(self):
        p = base_packet()
        out = execute_task_packet_core(p, {
            "codex": lambda _: {"status": "FAILED_CLOSED", "model": "gpt-5.3-codex", "findings": [], "evidence": []},
            "gemini": lambda _: {"status": "FAILED_CLOSED", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": []},
        }, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("FAILED_CLOSED", out["overall_status"])

    def test_conflicting_conclusions(self):
        p = base_packet()
        out = execute_task_packet_core(p, {
            "codex": lambda _: {"status": "SUCCESS", "model": "gpt-5.3-codex", "findings": [], "evidence": [], "conclusion": "A"},
            "gemini": lambda _: {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": [], "conclusion": "B"},
        }, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("NEEDS_VALIDATION", out["overall_status"])
        self.assertTrue(out["conflicts"])

    def test_idempotent_replay(self):
        p = base_packet(); reg = ExecutionRegistry(); calls = {"n": 0}
        def codex(_): calls["n"] += 1; return {"status": "SUCCESS", "model": "gpt-5.3-codex", "findings": [], "evidence": []}
        def gemini(_): calls["n"] += 1; return {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": []}
        dispatch = {"codex": codex, "gemini": gemini}
        first = execute_task_packet_core(p, dispatch, reg, sleep_fn=lambda _: None)
        second = execute_task_packet_core(p, dispatch, reg, sleep_fn=lambda _: None)
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(2, calls["n"])
        self.assertTrue(second["usage_summary"]["idempotent_replay"])

    def test_stale_cache_reexecutes(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        p = base_packet(now); p["deadline"] = (now + timedelta(days=3)).isoformat()
        reg = ExecutionRegistry(ttl_seconds=10); calls = {"n": 0}
        def codex(_): calls["n"] += 1; return {"status": "SUCCESS", "model": "gpt-5.3-codex", "findings": [], "evidence": []}
        def gemini(_): calls["n"] += 1; return {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": []}
        execute_task_packet_core(p, {"codex": codex, "gemini": gemini}, reg, now=now, sleep_fn=lambda _: None)
        execute_task_packet_core(p, {"codex": codex, "gemini": gemini}, reg, now=now + timedelta(seconds=11), sleep_fn=lambda _: None)
        self.assertEqual(4, calls["n"])

    def test_conflicting_duplicate_fails_closed(self):
        p = base_packet(); reg = ExecutionRegistry()
        dispatch = {
            "codex": lambda _: {"status": "SUCCESS", "model": "gpt-5.3-codex", "findings": [], "evidence": []},
            "gemini": lambda _: {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": []},
        }
        execute_task_packet_core(p, dispatch, reg, sleep_fn=lambda _: None)
        p2 = copy.deepcopy(p); p2["request"] = "different"
        out = execute_task_packet_core(p2, dispatch, reg, sleep_fn=lambda _: None)
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("CONFLICTING_DUPLICATE", out["unresolved_items"])

    def test_model_mismatch_fails_closed(self):
        p = base_packet(); p["specialist_plan"] = ["codex"]; p["max_fanout"] = 1
        out = execute_task_packet_core(p, {"codex": lambda _: {"status": "SUCCESS", "model": "gpt-5.6-sol", "findings": [], "evidence": []}}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertTrue(any("model_mismatch" in x for x in out["unresolved_items"]))

    def test_malicious_worker_operation_policy_blocked(self):
        p = base_packet(); p["specialist_plan"] = ["gemini"]; p["max_fanout"] = 1
        out = execute_task_packet_core(p, {
            "gemini": lambda _: {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": [], "requested_operations": ["production_write"]},
        }, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("POLICY_BLOCKED", out["overall_status"])

    def test_codex_timeout(self):
        p = base_packet(); p["specialist_plan"] = ["codex"]; p["max_fanout"] = 1
        def timeout(_): raise TimeoutError("timeout")
        out = execute_task_packet_core(p, {"codex": timeout}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("TIMEOUT", out["overall_status"])

    def test_gemini_timeout(self):
        p = base_packet(); p["specialist_plan"] = ["gemini"]; p["max_fanout"] = 1
        def timeout(_): raise TimeoutError("timeout")
        out = execute_task_packet_core(p, {"gemini": timeout}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("TIMEOUT", out["overall_status"])

    def test_rate_limit_retry_after_then_success(self):
        p = base_packet(); p["specialist_plan"] = ["gemini"]; p["max_fanout"] = 1; p["max_retries"] = 2
        calls = {"n": 0}; sleeps = []
        def worker(_):
            calls["n"] += 1
            if calls["n"] == 1: raise RateLimitError(retry_after="3")
            return {"status": "SUCCESS", "model": "google/gemini-3.1-pro-preview", "findings": [], "evidence": []}
        out = execute_task_packet_core(p, {"gemini": worker}, ExecutionRegistry(), sleep_fn=lambda x: sleeps.append(x))
        self.assertEqual("SUCCESS", out["overall_status"])
        self.assertEqual([3.0], sleeps)
        self.assertEqual(1, out["usage_summary"]["retry_count"])

    def test_rate_limit_budget_exhausted(self):
        p = base_packet(); p["specialist_plan"] = ["gemini"]; p["max_fanout"] = 1; p["max_retries"] = 1
        def worker(_): raise RateLimitError(retry_after=0)
        out = execute_task_packet_core(p, {"gemini": worker}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("RATE_LIMITED", out["overall_status"])

    def test_provider_unavailable_retries_and_fails_closed(self):
        p = base_packet(); p["specialist_plan"] = ["codex"]; p["max_fanout"] = 1; p["max_retries"] = 1
        def worker(_): raise ProviderUnavailableError(retry_after=0)
        out = execute_task_packet_core(p, {"codex": worker}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertEqual(2, len(out["retry_trace"]))

    def test_redaction(self):
        safe = redact({"OPENROUTER_API_KEY": "supersecret", "text": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"})
        self.assertEqual("[REDACTED]", safe["OPENROUTER_API_KEY"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", safe["text"])


if __name__ == "__main__":
    unittest.main()
