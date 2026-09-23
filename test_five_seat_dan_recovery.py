import json
import unittest

from dan_worker_relay import (
    DAN_WORKER_REQUEST_MARKER,
    DAN_WORKER_RESULT_MARKER,
    DanWorkerRelay,
    DanWorkerRelayConfig,
    DanWorkerRelayError,
    EXPECTED_QWEN_MODEL,
    EXPECTED_QWEN_ROUTE,
    EXPECTED_QWEN_SERVER_SHA256,
    EXPECTED_QWEN_SHA256,
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
        self.assert_issue_130(url)
        if method == "GET":
            return 200, list(reversed(self.comments))
        if method == "POST":
            self.posts += 1
            request = json.loads(str(body["body"]).split("\n", 1)[1])
            self.comments.append({
                "user": {"login": "jayagius99"},
                "body": body["body"],
            })
            result = {
                "schema": DAN_WORKER_RESULT_MARKER,
                "principal": "DAN-RECOVERY-SEAT",
                "status": "DAN_COMPLETE",
                "task_id": request["task_id"],
                "original_handoff_id": request["original_handoff_id"],
                "source_shared_state_version": request["source_shared_state_version"],
                "request_digest": request["request_digest"],
                "response_digest": "a" * 64,
                "route_id": EXPECTED_QWEN_ROUTE,
                "exact_model_id": EXPECTED_QWEN_MODEL,
                "model_sha256": EXPECTED_QWEN_SHA256,
                "server_sha256": EXPECTED_QWEN_SERVER_SHA256,
                "provider_spend_usd": 0,
                "side_effects": "NONE",
                "result": "Use the recovered bounded approach.",
                "evidence": ["proof"],
                "limitations": [],
                "recommended_next_action": "retry original worker",
                "package_path": r"C:\JAYTEC\Assignments\Completed\proof",
            }
            self.comments.append({
                "user": {"login": "jayagius99"},
                "body": DAN_WORKER_RESULT_MARKER + "\n" + json.dumps(result),
            })
            return 201, {"id": 1}
        raise AssertionError(method)

    @staticmethod
    def assert_issue_130(url):
        if "/issues/130/" not in url:
            raise AssertionError(url)


class TestDanWorkerRelay(unittest.TestCase):
    def test_round_trip_and_dedupe(self):
        transport = FakeRelayTransport()
        relay = DanWorkerRelay(
            DanWorkerRelayConfig.build(
                "token",
                "jayagius99/jaytec-work-engine-v2-g1",
                130,
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
        self.assertEqual("DAN-RECOVERY-SEAT", first["identity"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(1, transport.posts)
        self.assertEqual("WATCH_RECOVERY_CONTEXT_ONLY", first["acceptance"])

    def test_contract_mismatch_fails_closed(self):
        transport = FakeRelayTransport()
        relay = DanWorkerRelay(
            DanWorkerRelayConfig.build(
                "token",
                "owner/repo",
                130,
                timeout_seconds=10,
                poll_interval_seconds=0.25,
            ),
            transport=transport,
            sleep_fn=lambda _seconds: None,
        )
        attempt = relay.attempt_id("handoff-x")
        request_digest = "b" * 64
        bad = {
            "schema": DAN_WORKER_RESULT_MARKER,
            "principal": "DAN-RECOVERY-SEAT",
            "status": "DAN_COMPLETE",
            "task_id": "wrong",
            "source_shared_state_version": 1,
            "request_digest": request_digest,
            "response_digest": "a" * 64,
            "route_id": EXPECTED_QWEN_ROUTE,
            "exact_model_id": EXPECTED_QWEN_MODEL,
            "model_sha256": EXPECTED_QWEN_SHA256,
            "server_sha256": EXPECTED_QWEN_SERVER_SHA256,
            "provider_spend_usd": 0,
            "side_effects": "NONE",
        }
        transport.comments.append({
            "user": {"login": "jayagius99"},
            "body": DAN_WORKER_RESULT_MARKER + "\n" + json.dumps(bad),
        })
        self.assertIsNone(
            relay._matching_result(
                relay_task_id=attempt,
                request_digest=request_digest,
                source_state_version=1,
                ownership_fence="fence",
                return_worker_kind="TASK_PACKET",
                attempt_id=attempt,
            )
        )

    def test_matching_identity_with_wrong_runtime_hash_fails_closed(self):
        transport = FakeRelayTransport()
        relay = DanWorkerRelay(
            DanWorkerRelayConfig.build(
                "token", "owner/repo", 130, timeout_seconds=10
            ),
            transport=transport,
            sleep_fn=lambda _seconds: None,
        )
        attempt = relay.attempt_id("handoff-y")
        request_digest = "c" * 64
        bad = {
            "schema": DAN_WORKER_RESULT_MARKER,
            "principal": "DAN-RECOVERY-SEAT",
            "status": "DAN_COMPLETE",
            "task_id": attempt,
            "source_shared_state_version": 1,
            "request_digest": request_digest,
            "response_digest": "a" * 64,
            "route_id": EXPECTED_QWEN_ROUTE,
            "exact_model_id": EXPECTED_QWEN_MODEL,
            "model_sha256": "0" * 64,
            "server_sha256": EXPECTED_QWEN_SERVER_SHA256,
            "provider_spend_usd": 0,
            "side_effects": "NONE",
        }
        transport.comments.append({
            "user": {"login": "jayagius99"},
            "body": DAN_WORKER_RESULT_MARKER + "\n" + json.dumps(bad),
        })
        with self.assertRaises(DanWorkerRelayError):
            relay._matching_result(
                relay_task_id=attempt,
                request_digest=request_digest,
                source_state_version=1,
                ownership_fence="fence",
                return_worker_kind="TASK_PACKET",
                attempt_id=attempt,
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
                        "identity": "DAN-RECOVERY-SEAT",
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
        self.assertEqual("DAN-RECOVERY-SEAT", ctx["identity"])
        self.assertEqual("recovery guidance", ctx["result"])
        self.assertEqual("SUCCESS", result["whole_packet_status"])


if __name__ == "__main__":
    unittest.main()
