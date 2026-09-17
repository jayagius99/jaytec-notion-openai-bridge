import json
import unittest
from datetime import datetime, timezone

from compat_server import rewrite_jsonrpc_payload
from workload_read_model import build_workload_snapshot, normalize_limit, status_category


class TestWorkloadReadModel(unittest.TestCase):
    def test_status_mapping_covers_representative_runtime_states(self):
        self.assertEqual(("Active/RUNNING", True), status_category("RUNNING"))
        self.assertEqual(("Queued", True), status_category("QUEUED"))
        self.assertEqual(("Blocked-or-Paused", True), status_category("PAUSED"))
        self.assertEqual(("Failed unresolved", True), status_category("FAILED_SAFE"))
        self.assertEqual(("Completed/SUCCEEDED", True), status_category("SUCCEEDED"))
        self.assertEqual(("Unknown", False), status_category("CANCELED"))

    def test_summary_and_recovered_overlay(self):
        rows = [
            {"job_id": "active", "task_id": "t", "job_status": "RUNNING", "health": "HEALTHY", "lease_owner": "worker-1", "lease_expires_at": "2026-09-18T01:00:00+00:00"},
            {"job_id": "queued", "task_id": "t", "job_status": "QUEUED", "health": "HEALTHY"},
            {"job_id": "paused", "task_id": "t", "job_status": "PAUSED", "health": "DEGRADED", "recovery_event_types": ["TASK_PACKET_REQUEUED"]},
            {"job_id": "failed", "task_id": "t", "job_status": "FAILED_SAFE", "health": "FAILED_SAFE"},
            {"job_id": "done", "task_id": "t", "job_status": "SUCCEEDED", "health": "HEALTHY"},
            {"job_id": "fixed", "task_id": "t", "job_status": "SUCCEEDED", "health": "HEALTHY", "recovery_event_types": ["GUARDIAN_STALE_EXECUTION_CONTAINED"]},
        ]
        result = build_workload_snapshot(rows, observed_at=datetime(2026, 9, 18, tzinfo=timezone.utc))
        counts = result["summary"]["counts"]
        self.assertEqual(1, counts["Active/RUNNING"]["count"])
        self.assertEqual(1, counts["Queued"]["count"])
        self.assertEqual(1, counts["Blocked-or-Paused"]["count"])
        self.assertEqual(1, counts["Failed unresolved"]["count"])
        self.assertEqual(2, counts["Completed/SUCCEEDED"]["count"])
        self.assertEqual(2, counts["Recovered/Fixed"]["count"])
        self.assertTrue(counts["Recovered/Fixed"]["supported"])
        self.assertTrue(result["summary"]["recovered_fixed_is_an_evidence_overlay"])

    def test_limit_is_bounded_and_unknown_categories_are_explicit(self):
        self.assertEqual(50, normalize_limit())
        self.assertEqual(100, normalize_limit(1000))
        self.assertEqual(1, normalize_limit(0))
        result = build_workload_snapshot(
            [{"job_id": str(i), "job_status": "CANCELED"} for i in range(150)],
            limit=1000,
        )
        self.assertEqual(100, result["returned_count"])
        self.assertEqual(100, result["summary"]["counts"]["Unknown"]["count"])
        self.assertFalse(result["summary"]["counts"]["Unknown"]["supported"])

    def test_projection_excludes_packet_prompt_results_and_secrets(self):
        result = build_workload_snapshot(
            [{
                "job_id": "safe-job",
                "task_id": "task",
                "subtask_id": "subtask",
                "objective": "Inspect health without exposing OPENAI_API_KEY=super-secret",
                "job_status": "SUCCEEDED",
                "packet_status": "SUCCEEDED",
                "resource_scope": {"specialists": ["codex", "gemini"], "packet": "do not expose"},
                "packet": {"prompt": "private prompt body", "OPENAI_API_KEY": "super-secret"},
                "latest_verified_result": {"provider_output": "private provider output"},
            }]
        )
        serialized = json.dumps(result)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("private prompt body", serialized)
        self.assertNotIn("private provider output", serialized)
        self.assertEqual(["codex", "gemini"], result["assignments"][0]["specialist_plan"])
        self.assertIn("[REDACTED]", result["assignments"][0]["objective"])

    def test_stale_catalog_command_rewrites_to_native_tool(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "collaborate", "arguments": {"task": "JAYTEC_WORKLOAD_STATUS"}},
        }
        rewritten = rewrite_jsonrpc_payload(payload)
        self.assertEqual("workload_snapshot", rewritten["params"]["name"])
        self.assertEqual({}, rewritten["params"]["arguments"])


if __name__ == "__main__":
    unittest.main()
