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


def _data(body: Mapping[str, Any]) -> Any:
    return body.get("data") if "data" in body else body


def _message_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    content = row.get("content")
    if isinstance(content, Mapping):
        content = content.get("text") or content.get("content") or json.dumps(content, ensure_ascii=False)
    if not isinstance(content, str):
        content = str(content or "")
    return {
        "role": row.get("role") or row.get("sender_type") or row.get("type"),
        "content": content[:MAX_CONTENT],
        "created_at": row.get("created_at") or row.get("createdAt"),
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
        detail_data = _data(detail)
        rows = _data(messages)
        if not isinstance(detail_data, Mapping):
            detail_data = {}
        if not isinstance(rows, list):
            rows = []
        out["task"] = {
            "status": detail_data.get("status"),
            "title": detail_data.get("title"),
            "credit_usage": detail_data.get("credit_usage"),
            "task_url": detail_data.get("task_url"),
        }
        summarized = [_message_summary(r) for r in rows if isinstance(r, Mapping)]
        # API requests descending order; keep only the newest bounded content.
        out["messages"] = summarized[:6]
        out["status"] = "PASS"
    except ManusError as exc:
        out["error"] = str(exc)
    except Exception as exc:
        out["error"] = type(exc).__name__
    print(json.dumps(redact(out), ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if out["status"] == "PASS" else 5


if __name__ == "__main__":
    raise SystemExit(main())
