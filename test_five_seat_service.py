import json
import os
import threading
import time
import unittest
import uuid

import psycopg2

from five_seat_adapters import RetryableAdapterError
from five_seat_service import (
    FABRIC_SERVICE_ID,
    FiveSeatFabricService,
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
        self.assertEqual(queue.kwargs["max_attempts"], 1)
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

        self.assertIn("WatchLeaderUnavailable", source)
        self.assertIn("self.watch.release_leader(token)", source)
        self.assertIn("FIVE_SEAT_WATCH_LEADER_ACQUIRED", source)
        self.assertIn("FIVE_SEAT_WATCH_LEADER_RELEASED", source)



@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required")
class TestFiveSeatProductionHostPostgres(unittest.TestCase):
    def test_five_simultaneous_packets_complete_through_watch(self):
        from five_seat_authority import PostgresFabricAuthority
        from five_seat_queue import PostgresFabricQueue

        database_url = os.environ["DATABASE_URL"]
        authority = PostgresFabricAuthority(database_url)
        source_version = int(authority.current_state()["current_shared_state_version"])
        self.assertGreater(source_version, 0)

        barrier = threading.Barrier(5, timeout=15)

        def execute(packet_json):
            packet = json.loads(packet_json)
            barrier.wait()
            time.sleep(0.2)
            return {
                "overall_status": "SUCCESS",
                "codex_result": {
                    "status": "SUCCESS",
                    "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                },
                "unresolved_items": [],
                "task_id": packet["task_id"],
            }

        service = FiveSeatFabricService(
            database_url,
            execute,
            instance_id="pg-proof-" + uuid.uuid4().hex[:8],
            lease_seconds=60,
            guardian_interval_seconds=5,
            report_interval_seconds=3600,
        )
        queue = PostgresFabricQueue(database_url)
        job_ids = []
        service.start()
        try:
            run = uuid.uuid4().hex[:10]
            for index in range(1, 6):
                packet = {
                    "packet_version": "1.0",
                    "task_id": f"HOST-{run}-{index}",
                    "subtask_id": f"SUB-{index}",
                    "request": "No-provider production-host proof",
                    "intent": "Prove five-seat continuous host",
                    "workflow_id": "FS08_PRODUCTION_HOST_PROOF",
                    "risk_level": "low",
                    "specialist_plan": ["codex"],
                    "allowed_operations": ["read", "analyze", "validate"],
                    "expected_output": "proof",
                    "validation_requirements": ["WATCH ACCEPT"],
                    "side_effect_policy": "none",
                    "idempotency_key": f"host-proof-{run}-{index}",
                    "deadline": "2099-01-01T00:00:00Z",
                    "max_fanout": 1,
                    "max_retries": 2,
                    "return_schema_version": "1.0",
                }
                snapshot = submit_low_risk_task_packet(
                    queue,
                    packet_json=json.dumps(packet),
                    source_shared_state_version=source_version,
                    priority=index,
                )
                job_ids.append(snapshot["job_id"])

            deadline = time.time() + 30
            states = {}
            while time.time() < deadline:
                states = {
                    job_id: service.job_status(job_id)
                    for job_id in job_ids
                }
                if all(
                    item.get("job", {}).get("fabric_state") == "SUCCEEDED"
                    and item.get("job", {}).get("watch_decision") == "ACCEPT"
                    for item in states.values()
                ):
                    break
                time.sleep(0.25)

            self.assertEqual(len(states), 5)
            self.assertTrue(
                all(
                    item.get("job", {}).get("fabric_state") == "SUCCEEDED"
                    and item.get("job", {}).get("watch_decision") == "ACCEPT"
                    for item in states.values()
                ),
                states,
            )
            report = service.reporter.last_60_minutes()
            completed = {
                item["job_id"]
                for item in report["completed_jobs"]
            }
            self.assertTrue(set(job_ids).issubset(completed))
            self.assertEqual(report["summary"]["seats_total"], 5)
        finally:
            service.stop()
            time.sleep(0.3)
            with psycopg2.connect(database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE jaytec_worker_seats
                           SET state='FREE',current_job_id=NULL,worker_id=NULL,
                               lease_owner=NULL,lease_expires_at=NULL
                           WHERE current_job_id = ANY(%s)""",
                        (job_ids or ["__none__"],),
                    )
                    if job_ids:
                        for table in (
                            "jaytec_watch_reviews",
                            "jaytec_worker_handoffs",
                            "jaytec_fabric_approvals",
                            "jaytec_job_events",
                            "jaytec_operations",
                            "jaytec_job_steps",
                            "jaytec_task_packets",
                            "jaytec_fabric_envelopes",
                        ):
                            cur.execute(
                                f"DELETE FROM {table} WHERE job_id = ANY(%s)",
                                (job_ids,),
                            )
                        cur.execute(
                            "DELETE FROM jaytec_jobs WHERE job_id = ANY(%s)",
                            (job_ids,),
                        )
                    cur.execute(
                        """UPDATE jaytec_watch_leader
                           SET lease_owner=NULL,lease_expires_at=NULL,
                               version=version+1,updated_at=now()
                           WHERE controller_id='WATCH'"""
                    )


if __name__ == "__main__":
    unittest.main()
