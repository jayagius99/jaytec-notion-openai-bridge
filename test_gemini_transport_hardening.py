import json
import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch
from worker_json import WorkerJsonError


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected extra provider call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def response(content, *, model=EXPECTED_GEMINI_MODEL, finish_reason="stop"):
    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
    )


def packet(max_retries=1):
    return {
        "task_id": "TASK-1",
        "subtask_id": "SUB-1",
        "max_retries": max_retries,
        "allowed_operations": ["research"],
    }


def valid_payload():
    return json.dumps(
        {
            "status": "SUCCESS",
            "model": EXPECTED_GEMINI_MODEL,
            "findings": ["ok"],
            "evidence": ["fixture"],
            "confidence": "HIGH",
            "conclusion": {"answer": "ok"},
            "unresolved_items": [],
            "files_or_artifacts": [],
            "architecture_changes_required": [],
            "knowledge_writeback_proposal": [],
            "side_effects_attempted": [],
            "requested_operations": [],
            "task_id": "TASK-1",
            "subtask_id": "SUB-1",
        }
    )


class TestGeminiTransportHardening(unittest.TestCase):
    def build(self, responses):
        client = FakeClient(responses)
        dispatch = build_gemini_dispatch(
            openrouter_client=client,
            gemini_model=EXPECTED_GEMINI_MODEL,
            gemini_timeout_s=10,
            circuit=CircuitBreaker(failure_threshold=5),
        )
        return client, dispatch

    def test_requests_json_object_and_records_safe_diagnostics(self):
        client, dispatch = self.build([response(valid_payload())])
        out = dispatch(packet())
        self.assertEqual("SUCCESS", out["status"])
        self.assertEqual(EXPECTED_GEMINI_MODEL, out["model"])
        self.assertEqual(1, len(client.chat.completions.calls))
        call = client.chat.completions.calls[0]
        self.assertEqual({"type": "json_object"}, call["response_format"])
        self.assertEqual("stop", out["bridge_diagnostics"]["finish_reason"])
        self.assertEqual(EXPECTED_GEMINI_MODEL, out["bridge_diagnostics"]["provider_model"])
        self.assertEqual(64, len(out["bridge_diagnostics"]["content_sha256"]))
        self.assertNotIn("content", out["bridge_diagnostics"])

    def test_provider_model_mismatch_fails_closed(self):
        _, dispatch = self.build([response(valid_payload(), model="other/model")])
        with self.assertRaises(RuntimeError):
            dispatch(packet())

    def test_malformed_first_response_gets_one_bounded_reanswer(self):
        client, dispatch = self.build(
            [response('{"status":"SUCCESS","detail":"unterminated}'), response(valid_payload())]
        )
        out = dispatch(packet(max_retries=1))
        self.assertEqual("SUCCESS", out["status"])
        self.assertEqual(2, len(client.chat.completions.calls))
        retry_prompt = client.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("previous transport attempt", retry_prompt)
        self.assertNotIn("unterminated", retry_prompt)

    def test_malformed_response_without_retry_budget_fails_closed(self):
        _, dispatch = self.build([response('{"status":"SUCCESS", findings: []}')])
        with self.assertRaises(WorkerJsonError):
            dispatch(packet(max_retries=0))

    def test_length_finish_reason_can_reanswer_once(self):
        client, dispatch = self.build(
            [response('{"status":"SUCCESS"', finish_reason="length"), response(valid_payload())]
        )
        out = dispatch(packet(max_retries=1))
        self.assertEqual("SUCCESS", out["status"])
        self.assertEqual(2, len(client.chat.completions.calls))

    def test_second_malformed_response_still_fails_closed(self):
        _, dispatch = self.build(
            [
                response('{"status":"SUCCESS","detail":"unterminated}'),
                response('{"status":"SUCCESS", findings: []}'),
            ]
        )
        with self.assertRaises(WorkerJsonError):
            dispatch(packet(max_retries=1))


    def _read_payload(self, *, verified, title="Example conversation", fetch_status="SUCCESS"):
        return {
            "status": "SUCCESS" if verified else "FAILED_CLOSED",
            "model": EXPECTED_GEMINI_MODEL,
            "findings": ["Recovered exact conversation title and details."] if verified else [],
            "evidence": ["Exact shared page fetched."] if verified else [],
            "confidence": "HIGH" if verified else "LOW",
            "conclusion": {
                "READ_REPORT": {
                    "READ_REPORT_ID": "READ-example",
                    "SOURCE_URL": "https://chatgpt.com/share/example",
                    "ACCESS_ROUTE": EXPECTED_GEMINI_MODEL,
                    "FETCH_STATUS": fetch_status,
                    "VERIFIED": verified,
                    "TITLE": title if verified else None,
                    "SOURCE_METADATA": {"kind": "chat_share"},
                    "SUMMARY": "Grounded source summary." if verified else "Fetch failed.",
                    "KEY_FINDINGS": ["Source-specific fact."] if verified else [],
                    "DECISIONS": [],
                    "UNRESOLVED": [] if verified else ["Exact page unavailable."],
                    "RISKS": [],
                    "REFERENCES_IDENTIFIERS": ["example"],
                    "MEETING_RELEVANCE": "Useful" if verified else "Unknown",
                    "SUGGESTED_MEETING_DISCUSSION": [],
                    "CONFIDENCE": "HIGH" if verified else "LOW",
                    "DEDUPLICATION_KEY": "example",
                    "ROUTE_AUDIT": {
                        "web_retrieval_used": True,
                        "gemini_used": True,
                        "notion_used": False,
                        "other_agents_used": [],
                    },
                }
            },
            "unresolved_items": [] if verified else ["fetch_failed"],
            "files_or_artifacts": [],
            "architecture_changes_required": [],
            "knowledge_writeback_proposal": [],
            "side_effects_attempted": [],
            "requested_operations": [],
            "task_id": "READ-1",
            "subtask_id": "READ-1-R1",
        }

    def _read_packet(self):
        return {
            "task_id": "READ-1",
            "subtask_id": "READ-1-R1",
            "workflow_id": "JAYTEC_READ",
            "max_retries": 0,
            "allowed_operations": ["read", "research", "analyze", "validate", "web_fetch"],
            "required_context": {"source_url": "https://chatgpt.com/share/example"},
        }

    def test_jaytec_read_falls_back_from_openrouter_to_exa_only(self):
        failed = self._read_payload(
            verified=False,
            fetch_status="HTTP 403: Forbidden",
        )
        success = self._read_payload(verified=True)
        client, dispatch = self.build([
            response(json.dumps(failed)),
            response(json.dumps(success)),
        ])
        out = dispatch(self._read_packet())
        self.assertEqual("SUCCESS", out["status"])
        self.assertEqual(2, len(client.chat.completions.calls))
        engines = [
            call["tools"][0]["parameters"]["engine"]
            for call in client.chat.completions.calls
        ]
        self.assertEqual(["openrouter", "exa"], engines)
        self.assertEqual(
            [
                {"engine": "openrouter", "status": "FAILED_CLOSED", "verified": False},
                {"engine": "exa", "status": "SUCCESS", "verified": True},
            ],
            out["bridge_diagnostics"]["web_retrieval_attempts"],
        )
        self.assertFalse(
            out["bridge_diagnostics"]["web_retrieval"]["notion_fallback"]
        )

    def test_jaytec_read_all_fetch_engines_fail_closed_without_notion(self):
        failed = self._read_payload(
            verified=False,
            fetch_status="UNAVAILABLE",
        )
        client, dispatch = self.build([
            response(json.dumps(failed)),
            response(json.dumps(failed)),
            response(json.dumps(failed)),
        ])
        out = dispatch(self._read_packet())
        self.assertEqual("FAILED_CLOSED", out["status"])
        engines = [
            call["tools"][0]["parameters"]["engine"]
            for call in client.chat.completions.calls
        ]
        self.assertEqual(["openrouter", "exa", "parallel"], engines)
        self.assertFalse(out["bridge_diagnostics"]["notion_fallback"])
        self.assertEqual(
            [False, False, False],
            [
                item["verified"]
                for item in out["bridge_diagnostics"]["web_retrieval_attempts"]
            ],
        )

    def test_jaytec_read_enables_web_fetch_and_requires_verified_report(self):
        client, dispatch = self.build([
            response(json.dumps(self._read_payload(verified=True)))
        ])
        out = dispatch(self._read_packet())
        self.assertEqual("SUCCESS", out["status"])
        call = client.chat.completions.calls[0]
        self.assertEqual("openrouter:web_fetch", call["tools"][0]["type"])
        self.assertEqual("required", call["tool_choice"])
        self.assertEqual(
            ["chatgpt.com"],
            call["tools"][0]["parameters"]["allowed_domains"],
        )
        self.assertEqual("openrouter", call["tools"][0]["parameters"]["engine"])
        self.assertTrue(out["bridge_diagnostics"]["web_retrieval"]["enabled"])
        self.assertFalse(out["bridge_diagnostics"]["web_retrieval"]["notion_fallback"])


if __name__ == "__main__":
    unittest.main()
