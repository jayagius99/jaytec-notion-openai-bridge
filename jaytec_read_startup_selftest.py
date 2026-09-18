"""Optional production-startup acceptance test for JAYTEC:READ.

Set JAYTEC_READ_STARTUP_SELFTEST_URL to run one real Gemini + web_fetch read
before the service starts. If unset/blank, this script exits immediately.

This test never calls Notion.
"""

from __future__ import annotations

import json
import os
import sys

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from jaytec_read import build_jaytec_read_packet
from orchestration import ExecutionRegistry, execute_task_packet_core
from specialist_adapters import (
    EXPECTED_GEMINI_MODEL,
    build_gemini_dispatch,
)

TEST_URL = os.environ.get("JAYTEC_READ_STARTUP_SELFTEST_URL", "").strip()
EXPECTED_TITLE = os.environ.get("JAYTEC_READ_STARTUP_EXPECTED_TITLE", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
TIMEOUT_S = float(os.environ.get("JAYTEC_READ_SELFTEST_TIMEOUT_S", "90"))


def _safe_summary(result: dict) -> dict:
    gemini = result.get("gemini_result")
    report = {}
    if isinstance(gemini, dict):
        conclusion = gemini.get("conclusion")
        if isinstance(conclusion, dict):
            maybe = conclusion.get("READ_REPORT")
            if isinstance(maybe, dict):
                report = maybe
    findings = report.get("KEY_FINDINGS")
    unresolved = report.get("UNRESOLVED")
    audit = report.get("ROUTE_AUDIT")
    return {
        "overall_status": result.get("overall_status"),
        "title": report.get("TITLE"),
        "verified": report.get("VERIFIED"),
        "fetch_status": report.get("FETCH_STATUS"),
        "key_findings_count": len(findings) if isinstance(findings, list) else 0,
        "unresolved_count": len(unresolved) if isinstance(unresolved, list) else 0,
        "route_audit": audit if isinstance(audit, dict) else None,
        "unresolved_items": result.get("unresolved_items", []),
    }


def main() -> int:
    if not TEST_URL:
        print("JAYTEC_READ_STARTUP_SELFTEST: SKIPPED")
        return 0

    if not OPENROUTER_API_KEY:
        print("JAYTEC_READ_STARTUP_SELFTEST: FAILED missing OPENROUTER_API_KEY")
        return 2

    client = OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )
    dispatch = build_gemini_dispatch(
        openrouter_client=client,
        gemini_model=GEMINI_MODEL,
        gemini_timeout_s=TIMEOUT_S,
        circuit=CircuitBreaker(failure_threshold=3, reset_after_seconds=60),
    )

    packet = build_jaytec_read_packet(
        TEST_URL,
        request_suffix=(
            "For this live acceptance test, return the conversation title, at least "
            "five concrete facts/events from the source, and the unresolved repair state."
        ),
        validation_suffix=[
            "At least five concrete source-specific facts/events are returned",
            "The unresolved repair state is explicitly captured",
        ],
    )

    result = execute_task_packet_core(
        packet,
        {"gemini": dispatch},
        ExecutionRegistry(),
    )
    summary = _safe_summary(result)
    print(
        "JAYTEC_READ_STARTUP_SELFTEST_RESULT="
        + json.dumps(summary, ensure_ascii=False, sort_keys=True)
    )

    ok = (
        summary["overall_status"] == "SUCCESS"
        and summary["verified"] is True
        and summary["key_findings_count"] >= 5
        and summary["unresolved_count"] >= 1
        and isinstance(summary["route_audit"], dict)
        and summary["route_audit"].get("web_retrieval_used") is True
        and summary["route_audit"].get("gemini_used") is True
        and summary["route_audit"].get("notion_used") is False
        and summary["route_audit"].get("other_agents_used") == []
    )
    if EXPECTED_TITLE:
        ok = ok and summary["title"] == EXPECTED_TITLE

    if not ok:
        print("JAYTEC_READ_STARTUP_SELFTEST: FAILED")
        return 3

    print("JAYTEC_READ_STARTUP_SELFTEST: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
