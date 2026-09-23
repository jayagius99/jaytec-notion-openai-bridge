import json
import unittest

from five_seat_adapters import RetryableAdapterError
from five_seat_service import (
    FABRIC_SERVICE_ID,
    TASK_PACKET_CAPABILITY,
    TASK_PACKET_WORKER_KIND,
    build_task_packet_adapter,
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
