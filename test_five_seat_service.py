import json
import unittest

from five_seat_adapters import RetryableAdapterError
from five_seat_service import (
    FABRIC_SERVICE_ID,
    TASK_PACKET_CAPABILITY,
    TASK_PACKET_WORKER_KIND,
    build_task_packet_adapter,
    submit_low_risk_task_packet,
)


class TestFiveSeatProductionServiceContract(unittest.TestCase):
    def test_exact_worker_kind_and_capability(self):
        self.assertEqual(TASK_PACKET_WORKER_KIND, "TASK_PACKET")
        self.assertEqual(TASK_PACKET_CAPABILITY, "jaytec.task_packet.execute")
        self.assertEqual(FABRIC_SERVICE_ID, "JAYTEC_FIVE_SEAT_PRODUCTION_HOST_V1")

    def test_adapter_preserves_existing_executor_and_success_evidence(self):
        seen = []

        def execute(packet_json):
            seen.append(json.loads(packet_json))
            return {
                "overall_status": "SUCCESS",
                "codex_result": {"model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
                "unresolved_items": [],
            }

        result = build_task_packet_adapter(execute)({
            "packet_json": json.dumps({"task_id": "x"})
        })
        self.assertEqual(seen, [{"task_id": "x"}])
        self.assertEqual(result["whole_packet_status"], "SUCCESS")
        self.assertEqual(result["partial_side_effect_status"], "NONE")
        self.assertEqual(result["unresolved_items"], [])
        self.assertIn("nvidia/nemotron", result["provider_identity"])
        self.assertEqual(result["worker_completion_classification"], "CANDIDATE_COMPLETE")

    def test_low_risk_ingress_accepts_valid_safe_operations(self):
        class Queue:
            def __init__(self):
                self.kwargs = None

            def submit(self, **kwargs):
                self.kwargs = kwargs
                return {"job_id": "fabric-test"}

        packet = {
            "packet_version": "1.0",
            "task_id": "TASK-1",
            "subtask_id": "SUB-1",
            "request": "Review the implementation.",
            "intent": "Independent safe review",
            "workflow_id": "TEST_REVIEW",
            "risk_level": "low",
            "specialist_plan": ["gemini"],
            "allowed_operations": ["read", "analyze", "validate"],
            "expected_output": "JSON review",
            "validation_requirements": ["evidence"],
            "side_effect_policy": "none",
            "idempotency_key": "idem-test-1",
            "deadline": "2099-01-01T00:00:00Z",
            "max_fanout": 1,
            "max_retries": 2,
            "return_schema_version": "1.0",
        }
        queue = Queue()
        result = submit_low_risk_task_packet(
            queue,
            packet_json=json.dumps(packet),
            source_shared_state_version=7,
        )
        self.assertEqual(result["job_id"], "fabric-test")
        self.assertEqual(queue.kwargs["authority_class"], "READ_ONLY")
        self.assertEqual(queue.kwargs["concurrency_class"], "A")
        self.assertEqual(queue.kwargs["cost_policy"]["mode"], "ZERO_SPEND")
        stored = json.loads(queue.kwargs["payload"]["packet_json"])
        self.assertEqual(stored["max_retries"], 0)
        self.assertEqual(stored["allowed_operations"], ["read", "analyze", "validate"])

    def test_low_risk_ingress_rejects_sol_and_side_effect_policy(self):
        class Queue:
            def submit(self, **kwargs):
                raise AssertionError("unsafe packet must not reach queue")

        base = {
            "packet_version": "1.0",
            "task_id": "TASK-2",
            "subtask_id": "SUB-2",
            "request": "Review.",
            "intent": "Review",
            "workflow_id": "TEST_REVIEW",
            "risk_level": "low",
            "specialist_plan": ["gemini"],
            "allowed_operations": ["read"],
            "expected_output": "JSON",
            "validation_requirements": ["evidence"],
            "side_effect_policy": "none",
            "idempotency_key": "idem-test-2",
            "deadline": "2099-01-01T00:00:00Z",
            "max_fanout": 1,
            "max_retries": 0,
            "return_schema_version": "1.0",
        }

        unsafe = dict(base)
        unsafe["side_effect_policy"] = "staging_only"
        with self.assertRaisesRegex(Exception, "side_effect_policy=none"):
            submit_low_risk_task_packet(
                Queue(),
                packet_json=json.dumps(unsafe),
                source_shared_state_version=7,
            )

    def test_adapter_turns_transient_provider_result_into_safe_retry(self):
        def execute(_packet_json):
            return {
                "overall_status": "TIMEOUT",
                "codex_result": {"status": "TIMEOUT"},
                "unresolved_items": ["worker_timeout"],
            }

        with self.assertRaises(RetryableAdapterError):
            build_task_packet_adapter(execute)({"packet_json": "{}"})

    def test_service_source_uses_exactly_five_workers_and_no_schema_ddl(self):
        from pathlib import Path

        source = Path(__file__).with_name("five_seat_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("for index in range(1, 6)", source)
        self.assertIn("self.scheduler.verify_schema_ready()", source)
        self.assertNotIn("five_seat_schema.sql", source)
        self.assertNotIn("CREATE TABLE", source)
        self.assertNotIn("ALTER TABLE", source)


if __name__ == "__main__":
    unittest.main()
