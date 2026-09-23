from __future__ import annotations

import unittest

from dan_relay_bus import PostgresDanWorkerRelay
from dan_worker_relay import (
    DAN_WORKER_IDENTITY,
    EXPECTED_QWEN_MODEL,
    EXPECTED_QWEN_ROUTE,
    EXPECTED_QWEN_SERVER_SHA256,
    EXPECTED_QWEN_SHA256,
)


class FakeStore:
    def __init__(self):
        self.job = None

    def submit(self, job):
        self.job = dict(job)

    def result(self, *, relay_task_id, request_digest):
        assert self.job is not None
        assert relay_task_id == self.job["task_id"]
        assert request_digest == self.job["request_digest"]
        return {
            "principal": DAN_WORKER_IDENTITY,
            "status": "DAN_COMPLETE",
            "task_id": relay_task_id,
            "source_shared_state_version": self.job[
                "source_shared_state_version"
            ],
            "request_digest": request_digest,
            "response_digest": "a" * 64,
            "route_id": EXPECTED_QWEN_ROUTE,
            "exact_model_id": EXPECTED_QWEN_MODEL,
            "model_sha256": EXPECTED_QWEN_SHA256,
            "server_sha256": EXPECTED_QWEN_SERVER_SHA256,
            "provider_spend_usd": 0,
            "side_effects": "NONE",
            "package_path": r"C:\JAYTEC\Assignments\Completed\proof",
            "result": "bounded recovery",
            "evidence": ["proof"],
            "limitations": [],
            "recommended_next_action": "resume original worker",
        }


class DanPrivateRelayTests(unittest.TestCase):
    def test_watch_recovery_round_trip_contract(self):
        store = FakeStore()
        relay = PostgresDanWorkerRelay(
            "postgresql://unused",
            timeout_seconds=5,
            poll_interval_seconds=0.01,
            store=store,
            sleep_fn=lambda _: None,
        )
        result = relay.recover(
            handoff_id="handoff-001",
            task_id="TASK-001",
            subtask_id="SUB-001",
            objective="recover bounded failure",
            failure_class="FAILED_CLOSED",
            evidence=["worker could not finish"],
            source_state_version=55,
            ownership_fence="job:1|handoff:2",
            return_worker_kind="TASK_PACKET",
        )
        self.assertEqual(result["identity"], "DAN-RECOVERY-SEAT")
        self.assertEqual(
            result["acceptance"], "WATCH_RECOVERY_CONTEXT_ONLY"
        )
        self.assertEqual(result["source_state_version"], 55)
        self.assertEqual(result["ownership_fence"], "job:1|handoff:2")
        self.assertEqual(result["return_worker_kind"], "TASK_PACKET")
        self.assertEqual(result["provider_spend_usd"], 0)
        self.assertEqual(store.job["cost_policy"], "ZERO_SPEND")
        self.assertEqual(store.job["side_effect_policy"], "PACKAGE_ONLY")
        self.assertEqual(
            store.job["principal"], "DAN-RECOVERY-SEAT"
        )


if __name__ == "__main__":
    unittest.main()
