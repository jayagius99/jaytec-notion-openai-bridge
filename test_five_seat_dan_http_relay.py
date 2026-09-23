import asyncio
import json
import os
import unittest
from unittest.mock import patch

import psycopg2

import dan_relay_http
from dan_relay_store import (
    DanRelayStoreError,
    EXPECTED_QWEN_MODEL,
    EXPECTED_QWEN_ROUTE,
    EXPECTED_QWEN_SERVER_SHA256,
    EXPECTED_QWEN_SHA256,
    PostgresDanRelay,
)


class TestDanRelayStore(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["DATABASE_URL"]
        self.store = PostgresDanRelay(
            self.url,
            poll_interval_seconds=0.01,
            timeout_seconds=1,
        )
        self.store.ensure_schema()
        self.request = self.store._build_request(
            handoff_id="handoff-ci-dan-http",
            task_id="CI-DAN-HTTP",
            subtask_id="CI-SUB",
            objective="bounded recovery proof",
            failure_class="FAILED_CLOSED",
            evidence=["worker could not complete bounded task"],
            source_state_version=1,
            ownership_fence="job:1|handoff:2",
            return_worker_kind="TASK_PACKET",
        )
        self.store._submit(self.request)

    def tearDown(self):
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jaytec_dan_relay_jobs WHERE request_id=%s",
                    (self.request["task_id"],),
                )

    def _result(self, status="DAN_COMPLETE"):
        return {
            "schema": "JAYTEC_DAN_RESULT_V1",
            "principal": "DAN-RECOVERY-SEAT",
            "status": status,
            "task_id": self.request["task_id"],
            "source_shared_state_version": 1,
            "request_digest": self.request["request_digest"],
            "response_digest": "a" * 64,
            "route_id": EXPECTED_QWEN_ROUTE,
            "exact_model_id": EXPECTED_QWEN_MODEL,
            "model_sha256": EXPECTED_QWEN_SHA256,
            "server_sha256": EXPECTED_QWEN_SERVER_SHA256,
            "provider_spend_usd": 0,
            "side_effects": "NONE",
            "result": "bounded recovery context",
            "evidence": ["proof"],
            "limitations": [],
            "recommended_next_action": "resume original worker",
            "package_path": r"C:\JAYTEC\Assignments\Completed\proof",
        }

    def test_claim_complete_and_readback(self):
        jobs = self.store.claim_pending(
            relay_id="ci-relay",
            limit=1,
            lease_seconds=60,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["request_id"], self.request["task_id"])
        self.assertTrue(
            self.store.complete(
                relay_id="ci-relay",
                request_id=self.request["task_id"],
                request_digest=self.request["request_digest"],
                result=self._result(),
            )
        )
        raw = self.store._result(self.request["task_id"])
        bounded = self.store._validate_result(
            self.request,
            raw,
            ownership_fence="job:1|handoff:2",
            return_worker_kind="TASK_PACKET",
        )
        self.assertEqual(bounded["identity"], "DAN-RECOVERY-SEAT")
        self.assertEqual(bounded["acceptance"], "WATCH_RECOVERY_CONTEXT_ONLY")
        self.assertEqual(bounded["return_worker_kind"], "TASK_PACKET")

    def test_partial_is_recorded_but_not_accepted_for_rework(self):
        jobs = self.store.claim_pending(relay_id="ci-relay", limit=1)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(
            self.store.complete(
                relay_id="ci-relay",
                request_id=self.request["task_id"],
                request_digest=self.request["request_digest"],
                result=self._result("DAN_PARTIAL"),
            )
        )
        raw = self.store._result(self.request["task_id"])
        with self.assertRaisesRegex(
            DanRelayStoreError,
            "dan_relay_candidate_not_complete",
        ):
            self.store._validate_result(
                self.request,
                raw,
                ownership_fence="job:1|handoff:2",
                return_worker_kind="TASK_PACKET",
            )

    def test_wrong_runtime_identity_fails_closed(self):
        bad = self._result()
        bad["model_sha256"] = "0" * 64
        jobs = self.store.claim_pending(relay_id="ci-relay", limit=1)
        self.assertEqual(len(jobs), 1)
        with self.assertRaisesRegex(
            DanRelayStoreError,
            "dan_relay_result_contract_mismatch",
        ):
            self.store.complete(
                relay_id="ci-relay",
                request_id=self.request["task_id"],
                request_digest=self.request["request_digest"],
                result=bad,
            )


class FakeStore:
    def claim_pending(self, **kwargs):
        return [{"request_id": "r1", "request_digest": "a" * 64, "request": {}}]

    def complete(self, **kwargs):
        return True


class TestDanRelayHttpMiddleware(unittest.TestCase):
    def _call(self, token):
        sent = []
        body = json.dumps(
            {"relay_id": "ci-relay", "limit": 1, "lease_seconds": 60}
        ).encode()

        async def downstream(scope, receive, send):
            raise AssertionError("DAN relay path leaked to downstream MCP app")

        middleware = dan_relay_http.DanRelayMiddleware(downstream)
        messages = iter([
            {"type": "http.request", "body": body, "more_body": False},
        ])

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/dan-relay/v1/poll",
            "headers": [(b"authorization", ("Bearer " + token).encode())],
        }
        asyncio.run(middleware(scope, receive, send))
        status = sent[0]["status"]
        payload = json.loads(sent[1]["body"].decode())
        return status, payload

    def test_bad_token_is_401(self):
        with patch.dict(
            os.environ,
            {
                "DAN_RELAY_HTTP_ENABLED": "1",
                "DAN_RELAY_HTTP_TOKEN": "correct-token-value-that-is-long-enough",
            },
            clear=False,
        ):
            status, payload = self._call("wrong-token")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_valid_token_can_only_poll_bounded_store(self):
        with patch.dict(
            os.environ,
            {
                "DAN_RELAY_HTTP_ENABLED": "1",
                "DAN_RELAY_HTTP_TOKEN": "correct-token-value-that-is-long-enough",
            },
            clear=False,
        ), patch.object(dan_relay_http, "_store", return_value=FakeStore()):
            status, payload = self._call(
                "correct-token-value-that-is-long-enough"
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"], "JAYTEC_DAN_RELAY_POLL_V1")
        self.assertEqual(len(payload["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
