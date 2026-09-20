import json
import unittest
from types import SimpleNamespace

from circuit_breaker import CircuitBreaker
from jaytec_read import build_jaytec_read_packet
from specialist_adapters import (
    EXPECTED_GEMINI_MODEL,
    build_gemini_dispatch,
)


SOURCE_URL = "https://chatgpt.com/share/example"


def _read_report(*, verified: bool) -> dict:
    return {
        "status": "SUCCESS" if verified else "FAILED_CLOSED",
        "model": EXPECTED_GEMINI_MODEL,
        "findings": ["Recovered source-specific shared-chat content."] if verified else [],
        "evidence": ["Exact requested page fetched."] if verified else [],
        "confidence": "HIGH" if verified else "LOW",
        "conclusion": {
            "READ_REPORT": {
                "READ_REPORT_ID": "READ-example",
                "SOURCE_URL": SOURCE_URL,
                "ACCESS_ROUTE": "openrouter:web_fetch",
                "FETCH_STATUS": "SUCCESS" if verified else "FAILED",
                "VERIFIED": verified,
                "TITLE": "Example shared chat" if verified else "",
                "SOURCE_METADATA": {"kind": "chat_share"},
                "SUMMARY": "Source-grounded summary." if verified else "",
                "KEY_FINDINGS": ["Specific source finding."] if verified else [],
                "DECISIONS": [],
                "UNRESOLVED": [] if verified else ["fetch_failed"],
                "RISKS": [],
                "REFERENCES_IDENTIFIERS": ["example"],
                "MEETING_RELEVANCE": "Useful",
                "SUGGESTED_MEETING_DISCUSSION": [],
                "CONFIDENCE": "HIGH" if verified else "LOW",
                "DEDUPLICATION_KEY": "example",
                "ROUTE_AUDIT": {
                    "web_retrieval_used": verified,
                    "reviewer_used": True,
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
    }


class _FakeCompletions:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        return SimpleNamespace(
            model=EXPECTED_GEMINI_MODEL,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(payload)),
                )
            ],
        )


class _FakeClient:
    def __init__(self, payloads):
        self.completions = _FakeCompletions(payloads)
        self.chat = SimpleNamespace(completions=self.completions)


def _dispatch(payloads):
    client = _FakeClient(payloads)
    dispatch = build_gemini_dispatch(
        openrouter_client=client,
        gemini_model=EXPECTED_GEMINI_MODEL,
        gemini_timeout_s=30,
        circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
    )
    return client, dispatch


class TestJaytecReadStagingReconcile(unittest.TestCase):
    def test_read_attaches_domain_restricted_server_tool(self):
        client, dispatch = _dispatch([_read_report(verified=True)])
        packet = build_jaytec_read_packet(SOURCE_URL)
        result = dispatch(packet)

        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(1, len(client.completions.calls))
        call = client.completions.calls[0]
        self.assertEqual("required", call["tool_choice"])
        self.assertEqual("openrouter:web_fetch", call["tools"][0]["type"])
        self.assertEqual("openrouter", call["tools"][0]["parameters"]["engine"])
        self.assertEqual(["chatgpt.com"], call["tools"][0]["parameters"]["allowed_domains"])
        self.assertIn("JAYTEC:READ HARD LOCK", call["messages"][0]["content"])
        self.assertIn(SOURCE_URL, call["messages"][0]["content"])

    def test_read_falls_back_from_openrouter_to_exa_only_after_unverified_result(self):
        client, dispatch = _dispatch([
            _read_report(verified=False),
            _read_report(verified=True),
        ])
        result = dispatch(build_jaytec_read_packet(SOURCE_URL))

        self.assertEqual("SUCCESS", result["status"])
        engines = [
            call["tools"][0]["parameters"]["engine"]
            for call in client.completions.calls
        ]
        self.assertEqual(["openrouter", "exa"], engines)
        attempts = result["bridge_diagnostics"]["web_retrieval_attempts"]
        self.assertEqual(
            [
                {"engine": "openrouter", "status": "FAILED_CLOSED", "verified": False},
                {"engine": "exa", "status": "SUCCESS", "verified": True},
            ],
            attempts,
        )

    def test_non_read_gemini_dispatch_does_not_gain_web_tool(self):
        client, dispatch = _dispatch([
            {"status": "SUCCESS", "model": EXPECTED_GEMINI_MODEL}
        ])
        packet = {
            "task_id": "GENERAL-1",
            "subtask_id": "GENERAL-1-R1",
            "workflow_id": "GENERAL_RESEARCH",
            "max_retries": 0,
        }
        dispatch(packet)

        call = client.completions.calls[0]
        self.assertNotIn("tools", call)
        self.assertNotIn("tool_choice", call)

    def test_all_fetch_engines_remain_bounded_and_fail_closed(self):
        client, dispatch = _dispatch([
            _read_report(verified=False),
            _read_report(verified=False),
            _read_report(verified=False),
        ])
        result = dispatch(build_jaytec_read_packet(SOURCE_URL))

        self.assertEqual("FAILED_CLOSED", result["status"])
        engines = [
            call["tools"][0]["parameters"]["engine"]
            for call in client.completions.calls
        ]
        self.assertEqual(["openrouter", "exa", "parallel"], engines)
        self.assertFalse(result["bridge_diagnostics"]["notion_fallback"])


if __name__ == "__main__":
    unittest.main()
