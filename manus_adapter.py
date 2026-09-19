"""STAGING ONLY: bounded Manus API v2 adapter for the JAYTEC control door.

Secrets remain server-side. This module never returns or logs MANUS_API_KEY.
Mutating methods require explicit callers and fail closed when Manus reports
insufficient credits. The paid OpenAI engineering reserve is not used here.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

MANUS_BASE_URL = os.environ.get("MANUS_BASE_URL", "https://api.manus.ai/v2").rstrip("/")
MANUS_API_KEY = os.environ.get("MANUS_API_KEY", "").strip()
MANUS_TIMEOUT_S = float(os.environ.get("MANUS_TIMEOUT_S", "20"))
MANUS_MAX_RETRIES = min(max(int(os.environ.get("MANUS_MAX_RETRIES", "1")), 0), 3)
MANUS_MAX_RESPONSE_BYTES = min(max(int(os.environ.get("MANUS_MAX_RESPONSE_BYTES", "1048576")), 4096), 4 * 1024 * 1024)
MANUS_MAX_MESSAGE_CHARS = min(max(int(os.environ.get("MANUS_MAX_MESSAGE_CHARS", "12000")), 1), 50000)


class ManusError(RuntimeError):
    pass


class ManusInsufficientCredits(ManusError):
    """Fail-closed signal: stop Manus work/mutation calls until credits recover."""


@dataclass(frozen=True)
class ManusResponse:
    endpoint: str
    status_code: int
    body: Mapping[str, Any]
    request_id: str | None = None
    credit_usage: float | int | None = None


def _error_code(parsed: Mapping[str, Any]) -> str | None:
    error = parsed.get("error")
    if isinstance(error, Mapping) and error.get("code") is not None:
        return str(error.get("code"))
    if parsed.get("code") is not None:
        return str(parsed.get("code"))
    return None


def _credit_usage(parsed: Mapping[str, Any]) -> float | int | None:
    for candidate in (parsed, parsed.get("data")):
        if isinstance(candidate, Mapping):
            value = candidate.get("credit_usage")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def _is_credit_failure(parsed: Mapping[str, Any]) -> bool:
    if _error_code(parsed) == "10091":
        return True
    text = json.dumps(parsed, ensure_ascii=False).lower()
    return "not enough credits" in text or "insufficient credits" in text


class ManusClient:
    def __init__(self, api_key: str = MANUS_API_KEY, base_url: str = MANUS_BASE_URL) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        if not self._api_key:
            raise ManusError("MANUS_API_KEY_NOT_CONFIGURED")

    @staticmethod
    def _parse_json(raw: bytes) -> Mapping[str, Any]:
        if len(raw) > MANUS_MAX_RESPONSE_BYTES:
            raise ManusError("MANUS_RESPONSE_TOO_LARGE")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManusError("MANUS_RESPONSE_INVALID_JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ManusError("MANUS_RESPONSE_NOT_OBJECT")
        return parsed

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(10.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(5.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.25)

    def _request(self, method: str, endpoint: str, *, params: Mapping[str, Any] | None = None, payload: Mapping[str, Any] | None = None) -> ManusResponse:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}/{endpoint}" + (f"?{query}" if query else "")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "x-manus-api-key": self._api_key, "User-Agent": "JAYTEC-Manus-Door/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        attempts = MANUS_MAX_RETRIES + 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=MANUS_TIMEOUT_S) as resp:
                    raw = resp.read(MANUS_MAX_RESPONSE_BYTES + 1)
                    parsed = self._parse_json(raw)
                    if _is_credit_failure(parsed):
                        raise ManusInsufficientCredits("MANUS_INSUFFICIENT_CREDITS")
                    if parsed.get("ok") is False:
                        raise ManusError(f"MANUS_API_ERROR:{_error_code(parsed) or 'unknown'}")
                    return ManusResponse(
                        endpoint=endpoint,
                        status_code=int(resp.status),
                        body=parsed,
                        request_id=resp.headers.get("x-request-id") or resp.headers.get("request-id"),
                        credit_usage=_credit_usage(parsed),
                    )
            except urllib.error.HTTPError as exc:
                last = exc
                try:
                    parsed = self._parse_json(exc.read(MANUS_MAX_RESPONSE_BYTES + 1))
                except ManusError:
                    parsed = {}
                if _is_credit_failure(parsed):
                    raise ManusInsufficientCredits("MANUS_INSUFFICIENT_CREDITS") from exc
                if exc.code != 429 or attempt + 1 >= attempts:
                    raise ManusError(f"MANUS_HTTP_{exc.code}:{_error_code(parsed) or 'unknown'}") from exc
                time.sleep(self._retry_delay(attempt, exc.headers.get("Retry-After")))
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    raise ManusError("MANUS_TRANSPORT_ERROR") from exc
                time.sleep(self._retry_delay(attempt, None))
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
        message = content.strip()
        if not message:
            raise ManusError("EMPTY_MESSAGE")
        if len(message) > MANUS_MAX_MESSAGE_CHARS:
            raise ManusError("MESSAGE_TOO_LARGE")
        return self._request("POST", "task.sendMessage", payload={"task_id": task_id, "message": {"content": message}}).body

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
