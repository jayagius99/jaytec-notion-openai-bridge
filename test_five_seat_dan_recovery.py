import json
import unittest

from dan_worker_relay import (
    DAN_WORKER_REQUEST_MARKER,
    DAN_WORKER_RESULT_MARKER,
    DanWorkerRelay,
    DanWorkerRelayConfig,
    DanWorkerRelayError,
)
from five_seat_service import (
    FiveSeatFabricService,
    build_task_packet_adapter,
)


class FakeRelayTransport:
    def __init__(self):
        self.comments = []
        self.posts = 0

    def __call__(self, method, url, headers, body):
        if method == "GET":
            return 200, list(reversed(self.comments))
        if method == "POST":
            self.posts += 1
            request = json.loads(str(body["body"]).split("\n", 1)[1])
            self.comments.append({"body": body["body"]})
            result = {
                "schema": "JAYTEC_DAN_WORKER_RESULT_V1",
                "identity": "DAN-WORKER",
                "task_id": request["task_id"],
                "subtask_id": request["subtask_id"],
                "attempt_id": request["attempt_id"],
                "source_state_version": request["source_state_version"],
                "ownership_fence": request["ownership_fence"],
                "return_worker_kind": request["return_worker_kind"],
                "qwen_receipt": {
                    "schema": "JAYTEC_DAN_QWEN_RECEIPT_V1",
                    "identity": "DAN-WORKER",
                    "http_status": 200,
                    "model_id": "Qwen2.5-1.5B-Instruct-Q4_K_M",
                    "response_sha256": "a" * 64,
                    "provider_spend_usd": 0,
                    "candidate": {
                        "status": "SUCCESS",
                        "result": "Use the recovered bounded approach.",
                        "evidence": ["proof"],
                        "limitations": [],
                        "recommended_next_action": "retry original worker",
                    },
                },
                "acceptance": "UNACCEPTED_CANDIDATE",
                "provider_spend_usd": 0,
            }
            self.comments.append({
                "body": DAN_WORKER_RESULT_MARKER + "\n" + json.dumps(result)
            })
            return 201, {"id": 1}
        raise AssertionError(method)


class TestDanWorkerRelay(unittest.TestCase):
    def test_round_trip_and_dedupe(self):
        transport = FakeRelayTransport()
        relay = DanWorkerRelay(
            DanWorkerRelayConfig.build(
                "token",
                "jayagius99/jaytec-work-engine-v2-g1",
                128,
                timeout_seconds=10,
                poll_interval_seconds=0.25,
            ),
            transport=transport,
            sleep_fn=lambda _seconds: None,
        )
        kwargs = dict(
            handoff_id="handoff-123",
            task_id="task-1",
            subtask_id="sub-1",
            objective="finish bounded task",
            failure_class="FAILED_CLOSED",
            evidence=["worker failed"],
            source_state_version=7,
            ownership_fence="job:3|handoff:4",
            return_worker_kind="TASK_PACKET",
        )
        first = relay.recover(**kwargs)
        second = relay.recover(**kwargs)
        self.assertEqual("DAN-WORKER", first["identity"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(1, transport.posts)

    def test_contract_mismatch_fails_closed(self):
        transport = FakeRelayTransport()
        relay = DanWorkerRelay(
            DanWorkerRelayConfig.build(
                "token",
                "owner/repo",
                1,
                timeout_seconds=10,
                poll_interval_seconds=0.25,
            ),
            transport=transport,
            sleep_fn=lambda _seconds: None,
        )
        attempt = relay.attempt_id("handoff-x")
        bad = {
            "identity": "DAN-WORKER",
            "task_id": "wrong",
            "subtask_id": "sub",
            "attempt_id": attempt,
            "source_state_version": 1,
            "ownership_fence": "fence",
            "return_worker_kind": "TASK_PACKET",
            "qwen_receipt": {
                "identity": "DAN-WORKER",
                "http_status": 200,
                "provider_spend_usd": 0,
                "candidate": {"status": "SUCCESS"},
            },
            "acceptance": "UNACCEPTED_CANDIDATE",
            "provider_spend_usd": 0,
        }
        transport.comments.append({
            "body": DAN_WORKER_RESULT_MARKER + "\n" + json.dumps(bad)
        })
        with self.assertRaises(DanWorkerRelayError):
            relay._matching_result(
                attempt_id=attempt,
                task_id="task",
                subtask_id="sub",
                source_state_version=1,
                ownership_fence="fence",
                return_worker_kind="TASK_PACKET",
            )


class TestDanWatchRecoveryContract(unittest.TestCase):
    def test_watch_eligibility_is_narrow(self):
        handoff = {
            "fabric_rework_count": 0,
            "fabric_max_reworks": 2,
            "source_shared_state_version": 1,
            "job_fence_token": 4,
        }
        self.assertTrue(
            FiveSeatFabricService._dan_recovery_eligible(
                handoff,
                worker_kind="TASK_PACKET",
                overall="FAILED_CLOSED",
                partial="NONE",
            )
        )
        self.assertFalse(
            FiveSeatFabricService._dan_recovery_eligible(
                handoff,
                worker_kind="TASK_PACKET",
                overall="POLICY_BLOCKED",
                partial="NONE",
            )
        )
        self.assertFalse(
            FiveSeatFabricService._dan_recovery_eligible(
                handoff,
                worker_kind="GITHUB_BRANCH_PR",
                overall="FAILED_CLOSED",
                partial="NONE",
            )
        )
        self.assertFalse(
            FiveSeatFabricService._dan_recovery_eligible(
                handoff,
                worker_kind="TASK_PACKET",
                overall="FAILED_CLOSED",
                partial="UNCERTAIN_PARTIAL",
            )
        )

    def test_reworked_worker_receives_dan_context(self):
        captured = {}

        def execute(packet_json):
            captured["packet"] = json.loads(packet_json)
            return {
                "overall_status": "SUCCESS",
                "unresolved_items": [],
            }

        adapter = build_task_packet_adapter(execute)
        packet = {
            "task_id": "task",
            "subtask_id": "sub",
            "required_context": {"existing": True},
        }
        result = adapter({
            "packet_json": json.dumps(packet),
            "_fabric_context": {
                "last_watch_evidence": {
                    "dan_worker_recovery": {
                        "identity": "DAN-WORKER",
                        "attempt_id": "attempt",
                        "source_state_version": 1,
                        "ownership_fence": "job:1|handoff:2",
                        "model_id": "qwen",
                        "response_sha256": "a" * 64,
                        "result": "recovery guidance",
                        "evidence": ["proof"],
                        "limitations": "",
                        "recommended_next_action": "retry",
                        "acceptance": "WATCH_RECOVERY_CONTEXT_ONLY",
                    }
                }
            },
        })
        ctx = captured["packet"]["required_context"]["dan_worker_recovery"]
        self.assertEqual("DAN-WORKER", ctx["identity"])
        self.assertEqual("recovery guidance", ctx["result"])
        self.assertEqual("SUCCESS", result["whole_packet_status"])


if __name__ == "__main__":
    unittest.main()
