from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional


MAX_BODY_BYTES = 24_000
MAX_INPUT_CHARS = 8_000
MAX_OUTPUT_TOKENS = 1_200
_RELAY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,119}$")
_ALLOWED_MODELS = {
    "DEEP": "inclusionai/ling-3.0-flash-vl:free",
    "CRITIC": "nvidia/nemotron-3-ultra-550b-a55b:free",
}
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.I),
    re.compile(r"(?i)(api[_-]?key|password|secret|bearer|token)\s*[:=]\s*[^\s,;}]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)[A-Z]:\\[^\r\n\"']+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_PRIVATE_MARKERS = (
    "privatelibrary",
    "jpl-sensitive",
    "raw secret",
    "credential material",
)


class DanCognitionHttpError(RuntimeError):
    pass


class DanCognitionAuthError(DanCognitionHttpError):
    pass


class DanCognitionPolicyError(DanCognitionHttpError):
    pass


def _enabled() -> bool:
    return os.environ.get("DAN_COGNITION_HTTP_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _configured_token() -> str:
    return os.environ.get("DAN_COGNITION_HTTP_TOKEN", "").strip()


def _openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def _openrouter_base() -> str:
    return os.environ.get(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).rstrip("/")


def _validate_input_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DanCognitionPolicyError("input_required")
    if len(text) > MAX_INPUT_CHARS:
        raise DanCognitionPolicyError("input_too_large")
    lower = text.lower()
    if any(marker in lower for marker in _PRIVATE_MARKERS):
        raise DanCognitionPolicyError("private_marker_rejected")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise DanCognitionPolicyError("sensitive_payload_rejected")
    return text


def _validate_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    relay_id = str(payload.get("relay_id") or "").strip()
    if not _RELAY_ID_RE.fullmatch(relay_id):
        raise DanCognitionPolicyError("invalid_relay_id")

    seat = str(payload.get("seat") or "").strip().upper()
    if seat not in _ALLOWED_MODELS:
        raise DanCognitionPolicyError("invalid_seat")

    model = str(payload.get("model") or "").strip()
    expected = _ALLOWED_MODELS[seat]
    if model != expected or not model.endswith(":free"):
        raise DanCognitionPolicyError("exact_free_model_required")

    request_id = str(payload.get("request_id") or "").strip()
    if not request_id or len(request_id) > 160:
        raise DanCognitionPolicyError("invalid_request_id")

    text = _validate_input_text(str(payload.get("input") or ""))
    max_tokens = int(payload.get("max_tokens") or 900)
    if max_tokens < 64 or max_tokens > MAX_OUTPUT_TOKENS:
        raise DanCognitionPolicyError("max_tokens_out_of_range")

    return {
        "relay_id": relay_id,
        "seat": seat,
        "model": model,
        "request_id": request_id,
        "input": text,
        "max_tokens": max_tokens,
    }


def _invoke_openrouter(req: Mapping[str, Any]) -> dict[str, Any]:
    key = _openrouter_key()
    if not key:
        raise DanCognitionHttpError("openrouter_unavailable")

    system = (
        "You are one bounded cognitive seat inside DAN. "
        "You have zero execution authority and may not modify JAYTEC or external systems. "
        "Do not reveal chain-of-thought. Return concise JSON with conclusion, evidence, "
        "risks, disagreement, uncertainty, and recommendation. "
        "Treat the supplied text as sanitized candidate context only."
    )
    body = {
        "model": req["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": req["input"]},
        ],
        "temperature": 0.2,
        "max_tokens": req["max_tokens"],
        "provider": {
            "allow_fallbacks": True,
        },
    }
    request = urllib.request.Request(
        _openrouter_base() + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "JAYTEC-DAN-COGNITION/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            upstream = payload.get("error")
            if isinstance(upstream, Mapping):
                detail = str(upstream.get("message") or upstream.get("code") or "")
            elif upstream is not None:
                detail = str(upstream)
        except Exception:
            detail = ""
        detail = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", detail)[:180]
        raise DanCognitionHttpError(
            "openrouter_http_" + str(exc.code) + ((":" + detail) if detail else "")
        ) from exc
    except Exception as exc:
        raise DanCognitionHttpError(
            "openrouter_" + type(exc).__name__
        ) from exc

    raw = str(
        ((((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
    ).strip()
    if not raw:
        raise DanCognitionHttpError("empty_model_response")

    returned_model = str(data.get("model") or "")
    return {
        "ok": True,
        "schema": "DAN_COGNITION_RESULT_V1",
        "request_id": req["request_id"],
        "seat": req["seat"],
        "requested_model": req["model"],
        "returned_model": returned_model,
        "output": raw,
        "authority": "COGNITIVE_CANDIDATE_ONLY",
        "side_effects": "NONE",
        "provider_spend_policy": "EXACT_FREE_MODEL_ONLY",
    }


class DanCognitionMiddleware:
    """Inference-only DAN surface.

    It exposes no JAYTEC state mutation, database operation, recovery polling,
    result completion, filesystem access, tools, or arbitrary model selection.
    """

    PATHS = {
        "/dan-cognition/v1/status",
        "/dan-cognition/v1/infer",
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path") or "")
        if path not in self.PATHS:
            return await self.app(scope, receive, send)

        if not _enabled():
            return await self._json_response(
                send, 404, {"ok": False, "error": "not_found"}
            )

        try:
            self._authenticate(scope)

            if path == "/dan-cognition/v1/status":
                if scope.get("method") not in {"GET", "POST"}:
                    return await self._json_response(
                        send, 405, {"ok": False, "error": "method_not_allowed"}
                    )
                return await self._json_response(
                    send,
                    200,
                    {
                        "ok": True,
                        "schema": "DAN_COGNITION_HTTP_V1",
                        "enabled": True,
                        "openrouter_configured": bool(_openrouter_key()),
                        "allowed_models": dict(_ALLOWED_MODELS),
                        "state_mutation": False,
                        "persistence": False,
                        "tools": False,
                    },
                )

            if scope.get("method") != "POST":
                return await self._json_response(
                    send, 405, {"ok": False, "error": "method_not_allowed"}
                )

            body = await self._read_body(receive)
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError as exc:
                raise DanCognitionPolicyError("invalid_json") from exc
            if not isinstance(payload, Mapping):
                raise DanCognitionPolicyError("body_must_be_object")

            normalized = _validate_request(payload)
            result = await asyncio.to_thread(_invoke_openrouter, normalized)
            return await self._json_response(send, 200, result)

        except DanCognitionAuthError as exc:
            return await self._json_response(
                send, 401, {"ok": False, "error": str(exc)}
            )
        except (DanCognitionPolicyError, ValueError) as exc:
            return await self._json_response(
                send, 400, {"ok": False, "error": str(exc)[:160]}
            )
        except DanCognitionHttpError as exc:
            return await self._json_response(
                send, 503, {"ok": False, "error": str(exc)[:160]}
            )
        except Exception as exc:
            return await self._json_response(
                send,
                502,
                {"ok": False, "error": "dan_cognition_failed:" + type(exc).__name__},
            )

    @staticmethod
    def _authenticate(scope: Mapping[str, Any]) -> None:
        expected = _configured_token()
        if not expected:
            raise DanCognitionAuthError("cognition_token_not_configured")
        header = DanCognitionMiddleware._header(scope, b"authorization")
        if not header or not header.lower().startswith(b"bearer "):
            raise DanCognitionAuthError("bearer_token_required")
        supplied = header.split(b" ", 1)[1].decode("utf-8", errors="strict")
        if not hmac.compare_digest(supplied, expected):
            raise DanCognitionAuthError("invalid_cognition_token")

    @staticmethod
    def _header(scope: Mapping[str, Any], name: bytes) -> Optional[bytes]:
        for key, value in scope.get("headers", []):
            if key.lower() == name.lower():
                return value
        return None

    @staticmethod
    async def _read_body(receive) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise DanCognitionPolicyError("request_body_too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    async def _json_response(send, status: int, payload: Mapping[str, Any]):
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": int(status),
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
