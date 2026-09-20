"""Deterministic policy for the Notion-facing JAYTEC courier.

Notion is transport only. It has no routing, planning, retry, research, or
follow-up discretion. The only accepted payloads are exact JAYTEC-authored
status or task-packet commands.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

ALLOWED_STATUS = "JAYTEC_ORCHESTRATION_STATUS"
ALLOWED_PACKET_PREFIX = "JAYTEC_EXECUTE_TASK_PACKET_JSON:"
MAX_TASK_CHARS = 65_536
MAX_PACKET_CHARS = 60_000


class CourierPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class CourierCommand:
    operation: Literal["status", "execute_task_packet"]
    packet_json: str | None = None


def _require_empty_agent_fields(*, notion_analysis: Any, context: Any) -> None:
    if notion_analysis not in ("", None):
        raise CourierPolicyError("NOTION_ANALYSIS_FORBIDDEN")
    if context not in ("", None):
        raise CourierPolicyError("NOTION_CONTEXT_FORBIDDEN")


def parse_courier_command(
    task: Any,
    *,
    notion_analysis: Any = "",
    context: Any = "",
) -> CourierCommand:
    """Parse one exact courier command without adding or inferring intent."""
    _require_empty_agent_fields(
        notion_analysis=notion_analysis,
        context=context,
    )
    if not isinstance(task, str):
        raise CourierPolicyError("TASK_MUST_BE_STRING")
    if not task or len(task) > MAX_TASK_CHARS:
        raise CourierPolicyError("TASK_SIZE_INVALID")

    if task == ALLOWED_STATUS:
        return CourierCommand(operation="status")

    if not task.startswith(ALLOWED_PACKET_PREFIX):
        raise CourierPolicyError("FREE_FORM_FORBIDDEN")

    raw = task[len(ALLOWED_PACKET_PREFIX) :]
    if not raw or len(raw) > MAX_PACKET_CHARS:
        raise CourierPolicyError("PACKET_SIZE_INVALID")
    try:
        packet = json.loads(raw)
    except Exception as exc:
        raise CourierPolicyError("PACKET_JSON_INVALID") from exc
    if not isinstance(packet, dict):
        raise CourierPolicyError("PACKET_OBJECT_REQUIRED")

    # Canonicalization is deterministic transport normalization, not reasoning.
    canonical = json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CourierCommand(
        operation="execute_task_packet",
        packet_json=canonical,
    )


def rejection_payload(reason: str) -> str:
    return json.dumps(
        {
            "status": "REJECTED",
            "reason": str(reason)[:120],
            "terminal": True,
            "retry": False,
            "agent_action": "STOP",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
