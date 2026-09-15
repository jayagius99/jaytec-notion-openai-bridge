import copy
import unittest

from orchestration import ExecutionRegistry, execute_task_packet_core
from test_orchestration import base_packet


class TestWorkerPolicyHardening(unittest.TestCase):
    def _run_codex(self, packet, worker):
        packet = copy.deepcopy(packet)
        packet["specialist_plan"] = ["codex"]
        packet["max_fanout"] = 1
        return execute_task_packet_core(
            packet,
            {"codex": worker},
            ExecutionRegistry(),
            sleep_fn=lambda _: None,
        )

    def test_missing_worker_status_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("missing_required_field:status", out["unresolved_items"])

    def test_missing_worker_findings_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "evidence": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("missing_required_field:findings", out["unresolved_items"])

    def test_missing_worker_evidence_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("missing_required_field:evidence", out["unresolved_items"])

    def test_worker_status_wrong_type_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": 123,
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_type:status", out["unresolved_items"])

    def test_worker_status_unsupported_value_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "WAT",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_worker_status", out["unresolved_items"])

    def test_worker_findings_wrong_type_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": "nope",
                "evidence": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_type:findings", out["unresolved_items"])

    def test_worker_evidence_wrong_type_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": "nope",
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_type:evidence", out["unresolved_items"])

    def test_worker_unresolved_items_wrong_type_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "FAILED_CLOSED",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
                "unresolved_items": "oops",
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_type:unresolved_items", out["unresolved_items"])

    def test_malformed_requested_operations_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
                "requested_operations": {"performed": ["read"]},
                "side_effects_attempted": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_requested_operations_type", out["unresolved_items"])

    def test_packet_scoped_operation_policy_blocks(self):
        p = base_packet()
        p["allowed_operations"] = ["validate"]
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
                "requested_operations": ["read"],
                "side_effects_attempted": [],
            },
        )
        self.assertEqual("POLICY_BLOCKED", out["overall_status"])
        self.assertTrue(any("worker_operation_not_allowed_by_packet:read" in x for x in out["unresolved_items"]))

    def test_nonempty_side_effects_policy_blocks(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
                "requested_operations": ["validate"],
                "side_effects_attempted": ["code_staging"],
            },
        )
        self.assertEqual("POLICY_BLOCKED", out["overall_status"])
        self.assertTrue(any("worker_side_effect_attempted:code_staging" in x for x in out["unresolved_items"]))

    def test_malformed_side_effects_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
                "requested_operations": [],
                "side_effects_attempted": {"attempted": []},
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("invalid_side_effects_attempted_type", out["unresolved_items"])

    def test_worker_oversized_output_fails_closed(self):
        p = base_packet()
        huge = "x" * 300000
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "model": "gpt-5.3-codex",
                "findings": [],
                "evidence": [],
                "extra": huge,
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertIn("oversized_worker_output", out["unresolved_items"])

    def test_missing_model_fails_closed(self):
        p = base_packet()
        out = self._run_codex(
            p,
            lambda _: {
                "status": "SUCCESS",
                "findings": [],
                "evidence": [],
                "requested_operations": [],
                "side_effects_attempted": [],
            },
        )
        self.assertEqual("FAILED_CLOSED", out["overall_status"])
        self.assertTrue(any("model_mismatch" in x for x in out["unresolved_items"]))

    def test_malformed_packet_operation_items_rejected_not_crashed(self):
        p = base_packet()
        p["allowed_operations"] = [{"op": "read"}]
        out = execute_task_packet_core(p, {}, ExecutionRegistry(), sleep_fn=lambda _: None)
        self.assertEqual("INVALID_PACKET", out["overall_status"])
        self.assertIn("invalid:allowed_operations", out["unresolved_items"])


if __name__ == "__main__":
    unittest.main()
