"""JAYTEC:READ hard-lock policy and OpenRouter web-retrieval helpers.

This module is intentionally small and dependency-free.  It does not fetch URLs
inside the JAYTEC bridge.  For JAYTEC:READ, Gemini receives OpenRouter's
server-side web_fetch tool, restricted to the requested public domain.

Hard rule:
- JAYTEC:READ is Gemini + web retrieval only.
- No Notion fallback is part of this workflow.
- If the source cannot be retrieved and source-specific evidence cannot be
  returned, the workflow fails closed.
"""

from __future__ import annotations

import hashlib
import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

JAYTEC_READ_WORKFLOW_ID = "JAYTEC_READ"
JAYTEC_READ_ALLOWED_OPERATIONS = frozenset(
    {"read", "research", "analyze", "validate", "web_fetch"}
)
JAYTEC_READ_REQUIRED_OPERATIONS = frozenset({"read", "validate", "web_fetch"})

READ_REPORT_FIELDS = (
    "READ_REPORT_ID",
    "SOURCE_URL",
    "ACCESS_ROUTE",
    "FETCH_STATUS",
    "VERIFIED",
    "TITLE",
    "SOURCE_METADATA",
    "SUMMARY",
    "KEY_FINDINGS",
    "DECISIONS",
    "UNRESOLVED",
    "RISKS",
    "REFERENCES_IDENTIFIERS",
    "MEETING_RELEVANCE",
    "SUGGESTED_MEETING_DISCUSSION",
    "CONFIDENCE",
    "DEDUPLICATION_KEY",
    "ROUTE_AUDIT",
)

JAYTEC_READ_PROMPT = """JAYTEC:READ HARD LOCK
This is a source-retrieval task, not a general research task.

You MUST use the supplied openrouter:web_fetch tool on the exact SOURCE_URL.
Do not claim access merely because a URL was supplied in the prompt.
Do not substitute search snippets for the requested page when the page itself
cannot be fetched.

A successful READ requires source-specific evidence from the fetched page.
If the exact page cannot be fetched, is empty, or does not provide enough
source-specific evidence, set VERIFIED=false and status=FAILED_CLOSED.

NOTION IS NOT A FALLBACK FOR THIS WORKFLOW.
Do not request, suggest, or claim use of Notion.  No external agent fallback is
authorized by this task.

Put the final report in conclusion.READ_REPORT with exactly these required
fields:
READ_REPORT_ID, SOURCE_URL, ACCESS_ROUTE, FETCH_STATUS, VERIFIED, TITLE,
SOURCE_METADATA, SUMMARY, KEY_FINDINGS, DECISIONS, UNRESOLVED, RISKS,
REFERENCES_IDENTIFIERS, MEETING_RELEVANCE, SUGGESTED_MEETING_DISCUSSION,
CONFIDENCE, DEDUPLICATION_KEY, ROUTE_AUDIT.

ROUTE_AUDIT must be an object containing:
- web_retrieval_used: boolean
- gemini_used: true
- notion_used: false
- other_agents_used: []

VERIFIED may be true only if:
1) the exact requested page was retrieved,
2) TITLE and SUMMARY are grounded in the retrieved page, and
3) KEY_FINDINGS contains concrete source-specific details.

Never mark VERIFIED=true from URL structure, prior knowledge, search snippets,
or assumptions alone.
"""


class JaytecReadPolicyError(ValueError):
    pass


def is_jaytec_read_packet(packet: Mapping[str, Any]) -> bool:
    return packet.get("workflow_id") == JAYTEC_READ_WORKFLOW_ID


def source_url_from_packet(packet: Mapping[str, Any]) -> str:
    context = packet.get("required_context")
    if not isinstance(context, Mapping):
        raise JaytecReadPolicyError("jaytec_read_missing_required_context")
    value = context.get("source_url")
    if not isinstance(value, str) or not value.strip():
        raise JaytecReadPolicyError("jaytec_read_missing_source_url")
    return validate_public_source_url(value.strip())


def validate_public_source_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise JaytecReadPolicyError("source_url_empty")

    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise JaytecReadPolicyError("source_url_scheme_forbidden")
    if not parsed.hostname:
        raise JaytecReadPolicyError("source_url_host_missing")
    if parsed.username is not None or parsed.password is not None:
        raise JaytecReadPolicyError("source_url_userinfo_forbidden")

    host = parsed.hostname.rstrip(".").lower()
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        raise JaytecReadPolicyError("source_url_private_host_forbidden")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise JaytecReadPolicyError("source_url_private_ip_forbidden")

    # Normalize away fragments; fragments are never sent in HTTP requests.
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise JaytecReadPolicyError("source_url_port_invalid") from exc
    if port is not None:
        netloc += f":{port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def build_openrouter_web_fetch_tool(source_url: str) -> dict[str, Any]:
    normalized = validate_public_source_url(source_url)
    domain = urlsplit(normalized).hostname
    if not domain:
        raise JaytecReadPolicyError("source_url_host_missing")
    return {
        "type": "openrouter:web_fetch",
        "parameters": {
            "engine": "openrouter",
            "max_content_tokens": 50000,
            "allowed_domains": [domain],
        },
    }



def build_jaytec_read_packet(
    source_url: str,
    *,
    now: datetime | None = None,
    request_suffix: str = "",
    validation_suffix: list[str] | None = None,
) -> dict[str, Any]:
    """Build the canonical Gemini-only JAYTEC:READ packet."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    normalized = validate_public_source_url(source_url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    stamp = int(current.timestamp())
    request = (
        "Fetch and read the exact SOURCE_URL. Produce a verified JAYTEC READ REPORT. "
        "Do not use search snippets as a substitute for the requested page."
    )
    if request_suffix.strip():
        request += " " + request_suffix.strip()

    validations = [
        "Exact SOURCE_URL is fetched",
        "Title and summary are grounded in fetched page content",
        "KEY_FINDINGS contains source-specific evidence",
        "VERIFIED is true only when page retrieval is proven",
        "Notion is not used and no fallback agent is used",
    ]
    if validation_suffix:
        validations.extend(str(item) for item in validation_suffix if str(item).strip())

    return {
        "packet_version": "1.0",
        "task_id": f"JAYTEC-READ-{digest[:12]}-{stamp}",
        "subtask_id": f"JAYTEC-READ-{digest[:12]}-{stamp}-R1",
        "request": request,
        "intent": "Recover source-specific content from a public URL for JAYTEC continuity.",
        "workflow_id": JAYTEC_READ_WORKFLOW_ID,
        "risk_level": "LOW",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "research", "analyze", "validate", "web_fetch"],
        "expected_output": "A standardized conclusion.READ_REPORT with source-specific evidence.",
        "validation_requirements": validations,
        "side_effect_policy": "none",
        "idempotency_key": f"jaytec-read:{digest}:{stamp}",
        "deadline": (current + timedelta(minutes=3)).isoformat(),
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": {"source_url": normalized},
        "known_facts": [],
        "constraints": [
            "Gemini only",
            "OpenRouter web_fetch only for retrieval",
            "No Notion fallback",
            "Fail closed if exact page cannot be verified",
        ],
    }


def enforce_read_report(result: Mapping[str, Any], source_url: str) -> dict[str, Any]:
    """Fail closed unless Gemini returned a complete, verified READ_REPORT."""

    normalized_source = validate_public_source_url(source_url)
    out = dict(result)
    conclusion = out.get("conclusion")
    report = conclusion.get("READ_REPORT") if isinstance(conclusion, Mapping) else None

    problems: list[str] = []
    if not isinstance(report, Mapping):
        problems.append("jaytec_read_missing_read_report")
        report_dict: dict[str, Any] = {}
    else:
        report_dict = dict(report)

    for field in READ_REPORT_FIELDS:
        if field not in report_dict:
            problems.append(f"jaytec_read_missing_field:{field}")

    report_url = report_dict.get("SOURCE_URL")
    if isinstance(report_url, str):
        try:
            report_url = validate_public_source_url(report_url)
        except JaytecReadPolicyError:
            problems.append("jaytec_read_invalid_report_source_url")
    if report_url != normalized_source:
        problems.append("jaytec_read_source_url_mismatch")

    title = report_dict.get("TITLE")
    summary = report_dict.get("SUMMARY")
    findings = report_dict.get("KEY_FINDINGS")
    route_audit = report_dict.get("ROUTE_AUDIT")

    if not isinstance(title, str) or not title.strip():
        problems.append("jaytec_read_title_missing")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("jaytec_read_summary_missing")
    if not isinstance(findings, list) or not any(
        isinstance(item, str) and item.strip() for item in findings
    ):
        problems.append("jaytec_read_source_specific_findings_missing")

    if not isinstance(route_audit, Mapping):
        problems.append("jaytec_read_route_audit_missing")
    else:
        if route_audit.get("gemini_used") is not True:
            problems.append("jaytec_read_route_audit_gemini")
        if route_audit.get("web_retrieval_used") is not True:
            problems.append("jaytec_read_route_audit_web")
        if route_audit.get("notion_used") is not False:
            problems.append("jaytec_read_route_audit_notion")
        if route_audit.get("other_agents_used") != []:
            problems.append("jaytec_read_route_audit_other_agents")

    if report_dict.get("VERIFIED") is not True:
        problems.append("jaytec_read_unverified")

    if problems:
        out["status"] = "FAILED_CLOSED"
        unresolved = out.get("unresolved_items")
        if not isinstance(unresolved, list):
            unresolved = []
        out["unresolved_items"] = list(unresolved) + problems
        if isinstance(conclusion, Mapping):
            new_conclusion = dict(conclusion)
        else:
            new_conclusion = {}
        if report_dict:
            report_dict["VERIFIED"] = False
            new_conclusion["READ_REPORT"] = report_dict
        out["conclusion"] = new_conclusion or None
    return out
