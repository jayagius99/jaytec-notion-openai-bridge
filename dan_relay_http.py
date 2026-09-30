from __future__ import annotations

import hmac
import json
import os
import re
from typing import Any, Mapping, Optional

from dan_relay_store import DanRelayStoreError, PostgresDanRelay


MAX_BODY_BYTES = 65_536
_RELAY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,119}$")


class DanRelayHttpError(RuntimeError):
    pass


class DanRelayAuthError(DanRelayHttpError):
    pass


class DanRelayPolicyError(DanRelayHttpError):
    pass


def _enabled() -> bool:
    return os.environ.get("DAN_RELAY_HTTP_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _configured_token() -> str:
    return os.environ.get("DAN_RELAY_HTTP_TOKEN", "").strip()


def _store() -> PostgresDanRelay:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise DanRelayHttpError("database_unavailable")
    store = PostgresDanRelay(database_url)
    store.ensure_schema()
    return store


class DanRelayMiddleware:
    """Dedicated authenticated HTTP surface for the JAYTEC-PC DAN relay.

    This middleware is intentionally outside the MCP tool catalog. It can only
    lease already-created DAN recovery envelopes and return bounded results.
    """

    PATHS = {
        "/dan-relay/v1/poll",
        "/dan-relay/v1/result",
        "/dan-relay/v1/status",
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
            if path == "/dan-relay/v1/status":
                if scope.get("method") not in {"GET", "POST"}:
                    return await self._json_response(
                        send, 405, {"ok": False, "error": "method_not_allowed"}
                    )
                return await self._json_response(
                    send,
                    200,
                    {
                        "ok": True,
                        "schema": "JAYTEC_DAN_RELAY_HTTP_V1",
                        "enabled": True,
                        "operations": ["poll", "result"],
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
                raise DanRelayPolicyError("invalid_json") from exc
            if not isinstance(payload, dict):
                raise DanRelayPolicyError("body_must_be_object")

            relay_id = str(payload.get("relay_id") or "").strip()
            if not _RELAY_ID_RE.fullmatch(relay_id):
                raise DanRelayPolicyError("invalid_relay_id")

            store = _store()
            if path == "/dan-relay/v1/poll":
                jobs = store.claim_pending(
                    relay_id=relay_id,
                    limit=int(payload.get("limit") or 1),
                    lease_seconds=int(payload.get("lease_seconds") or 300),
                )
                return await self._json_response(
                    send,
                    200,
                    {
                        "ok": True,
                        "schema": "JAYTEC_DAN_RELAY_POLL_V1",
                        "jobs": jobs,
                    },
                )

            request_id = str(payload.get("request_id") or "").strip()
            request_digest = str(payload.get("request_digest") or "").strip()
            result = payload.get("result")
            if not request_id or len(request_id) > 160:
                raise DanRelayPolicyError("invalid_request_id")
            if (
                len(request_digest) != 64
                or any(ch not in "0123456789abcdef" for ch in request_digest.lower())
            ):
                raise DanRelayPolicyError("invalid_request_digest")
            if not isinstance(result, Mapping):
                raise DanRelayPolicyError("result_object_required")
            completed = store.complete(
                relay_id=relay_id,
                request_id=request_id,
                request_digest=request_digest,
                result=dict(result),
            )
            return await self._json_response(
                send,
                200,
                {
                    "ok": True,
                    "schema": "JAYTEC_DAN_RELAY_RESULT_ACK_V1",
                    "completed": bool(completed),
                },
            )
        except DanRelayAuthError as exc:
            return await self._json_response(
                send, 401, {"ok": False, "error": str(exc)}
            )
        except (DanRelayPolicyError, ValueError) as exc:
            return await self._json_response(
                send, 400, {"ok": False, "error": str(exc)[:160]}
            )
        except DanRelayStoreError as exc:
            safe_error = str(exc)[:500]
            print(
                "DAN_RELAY_STORE_REJECTED="
                + json.dumps(
                    {
                        "path": path,
                        "error": safe_error,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return await self._json_response(
                send, 409, {"ok": False, "error": safe_error[:200]}
            )
        except DanRelayHttpError as exc:
            return await self._json_response(
                send, 503, {"ok": False, "error": str(exc)[:160]}
            )
        except Exception as exc:
            return await self._json_response(
                send,
                502,
                {"ok": False, "error": "dan_relay_failed:" + type(exc).__name__},
            )

    @staticmethod
    def _authenticate(scope: Mapping[str, Any]) -> None:
        expected = _configured_token()
        if not expected:
            raise DanRelayAuthError("relay_token_not_configured")
        header = DanRelayMiddleware._header(scope, b"authorization")
        if not header or not header.lower().startswith(b"bearer "):
            raise DanRelayAuthError("bearer_token_required")
        supplied = header.split(b" ", 1)[1].decode("utf-8", errors="strict")
        if not hmac.compare_digest(supplied, expected):
            raise DanRelayAuthError("invalid_relay_token")

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
                raise DanRelayPolicyError("request_body_too_large")
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
