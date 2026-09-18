"""STAGING ONLY: read-only Manus review task readback.

Uses an already-authorized Manus task id supplied through JAYTEC_MANUS_REVIEW_TASK_ID.
Never creates/stops/messages a task and never exposes MANUS_API_KEY.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from manus_adapter import ManusClient, ManusError
from orchestration import redact

TASK_ID = os.environ.get("JAYTEC_MANUS_REVIEW_TASK_ID", "").strip()
MAX_CONTENT = 6000


def _task(body: Mapping[str, Any]) -> Mapping[str, Any]:
    value = body.get("task")
    return value if isinstance(value, Mapping) else {}


def _messages(body: Mapping[str, Any]) -> list[Any]:
    value = body.get("messages")
    return value if isinstance(value, list) else []


def _message_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    event_type = str(row.get("type") or "")
    payload = row.get(event_type) if event_type else None
    if not isinstance(payload, Mapping):
        for key in ("assistant_message", "user_message", "error_message", "status_update"):
            candidate = row.get(key)
            if isinstance(candidate, Mapping):
                payload = candidate
                event_type = key
                break
    if not isinstance(payload, Mapping):
        payload = {}
    content = payload.get("content")
    if not isinstance(content, str):
        content = payload.get("description") or payload.get("brief") or ""
    return {
        "type": event_type or None,
        "content": str(content)[:MAX_CONTENT],
        "agent_status": payload.get("agent_status"),
        "timestamp": row.get("timestamp"),
    }


def main() -> int:
    if not TASK_ID:
        print(json.dumps({"event": "JAYTEC_MANUS_REVIEW_READBACK", "status": "SKIP", "reason": "TASK_ID_NOT_CONFIGURED"}, sort_keys=True))
        return 0

    out: dict[str, Any] = {"event": "JAYTEC_MANUS_REVIEW_READBACK", "status": "FAIL", "task_id": TASK_ID}
    try:
        client = ManusClient()
        detail = client.task_detail(TASK_ID)
        messages = client.list_messages(TASK_ID, limit=20)
        detail_data = _task(detail)
        rows = _messages(messages)

        out["task"] = {
            "id": detail_data.get("id"),
            "status": detail_data.get("status"),
            "title": detail_data.get("title"),
            "credit_usage": detail_data.get("credit_usage"),
            "task_url": detail_data.get("task_url"),
        }

        summarized = [_message_summary(r) for r in rows if isinstance(r, Mapping)]
        out["messages"] = summarized[:8]

        assistant_messages = [
            item for item in summarized
            if item.get("type") == "assistant_message" and item.get("content")
        ]
        meaningful_task = any(
            detail_data.get(k) not in (None, "")
            for k in ("id", "status", "title", "task_url", "credit_usage")
        )

        if meaningful_task and assistant_messages:
            out["status"] = "PASS"
        elif meaningful_task:
            out["status"] = "PENDING"
            out["error"] = "MANUS_REVIEW_OUTPUT_NOT_AVAILABLE_YET"
        else:
            out["status"] = "UNVERIFIED"
            out["error"] = "MANUS_TASK_NOT_VISIBLE_OR_EMPTY"
    except ManusError as exc:
        out["error"] = str(exc)
    except Exception as exc:
        out["error"] = type(exc).__name__

    print(json.dumps(redact(out), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if out["status"] == "PASS" else 5


if __name__ == "__main__":
    raise SystemExit(main())
