"""STAGING ONLY: bounded Manus API v2 adapter for the JAYTEC control door.

Secrets remain server-side. This module never returns or logs MANUS_API_KEY.
Initial door verification is read-only; mutating methods require explicit callers.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

MANUS_BASE_URL = os.environ.get("MANUS_BASE_URL", "https://api.manus.ai/v2").rstrip("/")
MANUS_API_KEY = os.environ.get("MANUS_API_KEY", "").strip()
MANUS_TIMEOUT_S = float(os.environ.get("MANUS_TIMEOUT_S", "20"))
MANUS_MAX_RETRIES = int(os.environ.get("MANUS_MAX_RETRIES", "1"))


class ManusError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManusResponse:
    endpoint: str
    status_code: int
    body: Mapping[str, Any]


class ManusClient:
    def __init__(self, api_key: str = MANUS_API_KEY, base_url: str = MANUS_BASE_URL) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        if not self._api_key:
            raise ManusError("MANUS_API_KEY_NOT_CONFIGURED")

    def _request(self, method: str, endpoint: str, *, params: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> ManusResponse:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}/{endpoint}" + (f"?{query}" if query else "")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "x-manus-api-key": self._api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        attempts = max(1, MANUS_MAX_RETRIES + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=MANUS_TIMEOUT_S) as resp:
                    raw = resp.read().decode("utf-8")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, Mapping):
                        raise ManusError("MANUS_RESPONSE_NOT_OBJECT")
                    if parsed.get("ok") is False:
                        err = parsed.get("error") if isinstance(parsed.get("error"), Mapping) else {}
                        raise ManusError(f"MANUS_API_ERROR:{err.get('code', 'unknown')}")
                    return ManusResponse(endpoint=endpoint, status_code=int(resp.status), body=parsed)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code != 429 or attempt + 1 >= attempts:
                    raise ManusError(f"MANUS_HTTP_{exc.code}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = min(5.0, float(retry_after)) if retry_after and retry_after.replace('.', '', 1).isdigit() else 1.0
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    raise ManusError("MANUS_TRANSPORT_ERROR") from exc
                time.sleep(min(2.0, 0.5 * (2 ** attempt)))
        raise ManusError("MANUS_REQUEST_FAILED") from last

    def user_me(self) -> Mapping[str, Any]:
        return self._request("GET", "user.me").body

    def list_projects(self) -> Mapping[str, Any]:
        return self._request("GET", "project.list").body

    def list_tasks(self, *, project_id: str | None = None, limit: int = 100) -> Mapping[str, Any]:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 100), "order": "desc"}
        if project_id:
            params.update({"scope": "project", "project_id": project_id})
        return self._request("GET", "task.list", params=params).body

    def task_detail(self, task_id: str) -> Mapping[str, Any]:
        return self._request("GET", "task.detail", params={"task_id": task_id}).body

    def list_messages(self, task_id: str, *, limit: int = 50) -> Mapping[str, Any]:
        return self._request("GET", "task.listMessages", params={"task_id": task_id, "order": "desc", "limit": min(max(limit, 1), 200)}).body

    def send_message(self, task_id: str, content: str) -> Mapping[str, Any]:
        if not content.strip():
            raise ManusError("EMPTY_MESSAGE")
        return self._request("POST", "task.sendMessage", payload={"task_id": task_id, "message": {"content": content}}).body

    def stop_task(self, task_id: str) -> Mapping[str, Any]:
        return self._request("POST", "task.stop", payload={"task_id": task_id}).body


def safe_identity_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    data = body.get("data") if isinstance(body.get("data"), Mapping) else body.get("user")
    if not isinstance(data, Mapping):
        data = {}
    return {
        "authenticated": bool(body.get("ok", True)),
        "user_id": data.get("id"),
        "display_name": data.get("name") or data.get("display_name"),
    }
