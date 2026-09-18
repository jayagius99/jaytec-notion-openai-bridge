import json
import unittest

from jaytec_read import (
    JaytecReadPolicyError,
    build_openrouter_web_fetch_tool,
    enforce_read_report,
    validate_public_source_url,
)
from orchestration import validate_packet


def read_packet(**overrides):
    packet = {
        "packet_version": "1.0",
        "task_id": "READ-1",
        "subtask_id": "READ-1-R1",
        "request": "Read the exact source URL.",
        "intent": "Verified URL retrieval.",
        "workflow_id": "JAYTEC_READ",
        "risk_level": "LOW",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "research", "analyze", "validate", "web_fetch"],
        "expected_output": "Verified READ_REPORT.",
        "validation_requirements": ["exact page evidence"],
        "side_effect_policy": "none",
        "idempotency_key": "read-1",
        "deadline": "2099-01-01T00:00:00+00:00",
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": {"source_url": "https://chatgpt.com/share/example"},
    }
    packet.update(overrides)
    return packet


def verified_result():
    source = "https://chatgpt.com/share/example"
    return {
        "status": "SUCCESS",
        "model": "google/gemini-3.1-pro-preview",
        "findings": ["Conversation title and source-specific detail recovered."],
        "evidence": ["Fetched exact shared conversation page."],
        "confidence": "HIGH",
        "conclusion": {
            "READ_REPORT": {
                "READ_REPORT_ID": "READ-example",
                "SOURCE_URL": source,
                "ACCESS_ROUTE": "google/gemini-3.1-pro-preview",
                "FETCH_STATUS": "SUCCESS",
                "VERIFIED": True,
                "TITLE": "Example conversation",
                "SOURCE_METADATA": {"kind": "chat_share"},
                "SUMMARY": "A source-grounded summary.",
                "KEY_FINDINGS": ["Specific finding from the fetched page."],
                "DECISIONS": [],
                "UNRESOLVED": [],
                "RISKS": [],
                "REFERENCES_IDENTIFIERS": ["example"],
                "MEETING_RELEVANCE": "Useful",
                "SUGGESTED_MEETING_DISCUSSION": ["Review the finding."],
                "CONFIDENCE": "HIGH",
                "DEDUPLICATION_KEY": "example",
                "ROUTE_AUDIT": {
                    "web_retrieval_used": True,
                    "gemini_used": True,
                    "notion_used": False,
                    "other_agents_used": [],
                },
            }
        },
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "requested_operations": [],
    }


class TestJaytecReadPolicy(unittest.TestCase):
    def test_public_url_normalizes_fragment(self):
        self.assertEqual(
            "https://chatgpt.com/share/example?x=1",
            validate_public_source_url("https://chatgpt.com/share/example?x=1#fragment"),
        )

    def test_private_targets_are_rejected(self):
        for url in (
            "http://127.0.0.1/",
            "http://10.1.2.3/",
            "http://localhost/",
            "http://service.internal/",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                with self.assertRaises(JaytecReadPolicyError):
                    validate_public_source_url(url)

    def test_openrouter_fetch_is_domain_restricted(self):
        tool = build_openrouter_web_fetch_tool("https://chatgpt.com/share/example")
        self.assertEqual("openrouter:web_fetch", tool["type"])
        self.assertEqual("openrouter", tool["parameters"]["engine"])
        self.assertEqual(["chatgpt.com"], tool["parameters"]["allowed_domains"])

    def test_fetch_engine_is_explicitly_allowlisted(self):
        exa = build_openrouter_web_fetch_tool(
            "https://chatgpt.com/share/example",
            engine="exa",
        )
        self.assertEqual("exa", exa["parameters"]["engine"])
        parallel = build_openrouter_web_fetch_tool(
            "https://chatgpt.com/share/example",
            engine="parallel",
        )
        self.assertEqual("parallel", parallel["parameters"]["engine"])
        with self.assertRaises(JaytecReadPolicyError):
            build_openrouter_web_fetch_tool(
                "https://chatgpt.com/share/example",
                engine="native",
            )

    def test_read_packet_requires_gemini_only(self):
        good = validate_packet(read_packet())
        self.assertTrue(good.ok, good.errors)

        bad = validate_packet(read_packet(specialist_plan=["codex", "gemini"], max_fanout=2))
        self.assertFalse(bad.ok)
        self.assertIn("jaytec_read_requires_gemini_only", bad.errors)

    def test_read_packet_forbids_side_effect_operations(self):
        bad = validate_packet(
            read_packet(
                allowed_operations=["read", "validate", "web_fetch", "code_staging"],
                side_effect_policy="staging_only",
            )
        )
        self.assertFalse(bad.ok)
        self.assertIn("jaytec_read_disallowed_operations:code_staging", bad.errors)
        self.assertIn("jaytec_read_side_effects_forbidden", bad.errors)

    def test_verified_report_passes(self):
        out = enforce_read_report(
            verified_result(), "https://chatgpt.com/share/example"
        )
        self.assertEqual("SUCCESS", out["status"])
        self.assertTrue(out["conclusion"]["READ_REPORT"]["VERIFIED"])

    def test_missing_or_unverified_report_fails_closed(self):
        result = verified_result()
        result["conclusion"]["READ_REPORT"]["VERIFIED"] = False
        out = enforce_read_report(result, "https://chatgpt.com/share/example")
        self.assertEqual("FAILED_CLOSED", out["status"])
        self.assertIn("jaytec_read_unverified", out["unresolved_items"])

    def test_notion_route_claim_fails_closed(self):
        result = verified_result()
        result["conclusion"]["READ_REPORT"]["ROUTE_AUDIT"]["notion_used"] = True
        out = enforce_read_report(result, "https://chatgpt.com/share/example")
        self.assertEqual("FAILED_CLOSED", out["status"])
        self.assertIn("jaytec_read_route_audit_notion", out["unresolved_items"])


if __name__ == "__main__":
    unittest.main()
