from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

import psycopg2
import psycopg2.extras

from dan_worker_relay import (
    DAN_WORKER_IDENTITY,
    EXPECTED_QWEN_MODEL,
    EXPECTED_QWEN_ROUTE,
    EXPECTED_QWEN_SERVER_SHA256,
    EXPECTED_QWEN_SHA256,
    DanWorkerRelayError,
)

DAN_HTTP_RELAY_ENABLED = os.environ.get(
    "JAYTEC_DAN_HTTP_RELAY_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
DAN_HTTP_RELAY_TOKEN = os.environ.get(
    "JAYTEC_DAN_HTTP_RELAY_TOKEN", ""
).strip()
DAN_RELAY_SCHEMA_ENABLED = os.environ.get(
    "JAYTEC_DAN_RELAY_SCHEMA_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}

MAX_BODY_BYTES = 131_072
TABLE = "jaytec_dan_relay_jobs"


def _compact(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_compact(value).encode("utf-8")).hexdigest()


class DanRelayStoreError(RuntimeError):
    pass


class PostgresDanRelayStore:
    def __init__(self, database_url: str):
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise DanRelayStoreError("dan_relay_database_url_missing")

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {TABLE} (
                        relay_task_id TEXT PRIMARY KEY,
                        request_digest TEXT NOT NULL UNIQUE,
                        request JSONB NOT NULL,
                        status TEXT NOT NULL
                            CHECK (status IN ('PENDING','CLAIMED','COMPLETE')),
                        claim_token TEXT,
                        claimed_until TIMESTAMPTZ,
                        result JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS
                        jaytec_dan_relay_jobs_status_created_idx
                    ON {TABLE}(status, created_at)
                    """
                )

    def submit(self, job: Mapping[str, Any]) -> None:
        request = dict(job)
        relay_task_id = str(request.get("task_id") or "").strip()
        request_digest = str(request.get("request_digest") or "").strip()
        if not relay_task_id or not request_digest:
            raise DanRelayStoreError("dan_relay_job_identity_missing")
        canonical = dict(request)
        canonical.pop("request_digest", None)
        if _digest(canonical) != request_digest:
            raise DanRelayStoreError("dan_relay_request_digest_mismatch")

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT request_digest, request
                    FROM {TABLE}
                    WHERE relay_task_id=%s
                    """,
                    (relay_task_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if str(existing["request_digest"]) != request_digest:
                        raise DanRelayStoreError(
                            "dan_relay_conflicting_duplicate_task"
                        )
                    if _compact(existing["request"]) != _compact(request):
                        raise DanRelayStoreError(
                            "dan_relay_conflicting_duplicate_payload"
                        )
                    return
                cur.execute(
                    f"""
                    INSERT INTO {TABLE}(
                        relay_task_id, request_digest, request, status
                    )
                    VALUES (%s,%s,%s::jsonb,'PENDING')
                    """,
                    (relay_task_id, request_digest, _compact(request)),
                )

    def claim(self, *, lease_seconds: int = 300) -> Optional[dict[str, Any]]:
        token = secrets.token_hex(24)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    WITH candidate AS (
                        SELECT relay_task_id
                        FROM {TABLE}
                        WHERE
                            status='PENDING'
                            OR (
                                status='CLAIMED'
                                AND claimed_until IS NOT NULL
                                AND claimed_until < NOW()
                            )
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE {TABLE} j
                    SET
                        status='CLAIMED',
                        claim_token=%s,
                        claimed_until=NOW() + (%s * INTERVAL '1 second'),
                        updated_at=NOW()
                    FROM candidate c
                    WHERE j.relay_task_id=c.relay_task_id
                    RETURNING
                        j.relay_task_id,
                        j.request_digest,
                        j.request,
                        j.claim_token,
                        j.claimed_until
                    """,
                    (token, max(30, int(lease_seconds))),
                )
                row = cur.fetchone()
                return dict(row) if row is not None else None

    def complete(
        self,
        *,
        relay_task_id: str,
        request_digest: str,
        claim_token: str,
        result: Mapping[str, Any],
    ) -> None:
        bounded_result = dict(result)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT status, request_digest, claim_token, result
                    FROM {TABLE}
                    WHERE relay_task_id=%s
                    FOR UPDATE
                    """,
                    (relay_task_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise DanRelayStoreError("dan_relay_job_not_found")
                if str(row["request_digest"]) != request_digest:
                    raise DanRelayStoreError("dan_relay_completion_digest_mismatch")
                if str(row["status"]) == "COMPLETE":
                    if _compact(row.get("result")) == _compact(bounded_result):
                        return
                    raise DanRelayStoreError(
                        "dan_relay_conflicting_completion"
                    )
                if str(row.get("claim_token") or "") != claim_token:
                    raise DanRelayStoreError("dan_relay_claim_token_mismatch")
                cur.execute(
                    f"""
                    UPDATE {TABLE}
                    SET
                        status='COMPLETE',
                        result=%s::jsonb,
                        claim_token=NULL,
                        claimed_until=NULL,
                        updated_at=NOW()
                    WHERE relay_task_id=%s
                    """,
                    (_compact(bounded_result), relay_task_id),
                )

    def result(
        self,
        *,
        relay_task_id: str,
        request_digest: str,
    ) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT status, request_digest, result
                    FROM {TABLE}
                    WHERE relay_task_id=%s
                    """,
                    (relay_task_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                if str(row["request_digest"]) != request_digest:
                    raise DanRelayStoreError("dan_relay_result_digest_mismatch")
                if str(row["status"]) != "COMPLETE":
                    return None
                result = row.get("result")
                return dict(result) if isinstance(result, Mapping) else None

    def status(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        COUNT(*) FILTER (WHERE status='PENDING'),
                        COUNT(*) FILTER (WHERE status='CLAIMED'),
                        COUNT(*) FILTER (WHERE status='COMPLETE')
                    FROM {TABLE}
                    """
                )
                pending, claimed, complete = cur.fetchone()
        return {
            "pending": int(pending or 0),
            "claimed": int(claimed or 0),
            "complete": int(complete or 0),
        }


class PostgresDanWorkerRelay:
    """WATCH-facing durable DAN recovery relay.

    WATCH writes only bounded recovery jobs to Postgres and accepts only exact,
    zero-spend, no-side-effect receipts from the local DAN recovery seat.
    """

    def __init__(
        self,
        database_url: str,
        *,
        timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 3.0,
        store: Optional[PostgresDanRelayStore] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self.store = store or PostgresDanRelayStore(database_url)
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.poll_interval_seconds = max(0.25, float(poll_interval_seconds))
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

    @staticmethod
    def attempt_id(handoff_id: str) -> str:
        digest = hashlib.sha256(
            str(handoff_id).encode("utf-8")
        ).hexdigest()[:24]
        return "watch-dan-" + digest

    @staticmethod
    def _validate_result(
        result: Mapping[str, Any],
        *,
        relay_task_id: str,
        request_digest: str,
        source_state_version: int,
        ownership_fence: str,
        return_worker_kind: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        checks = {
            "principal": str(result.get("principal") or "")
            == DAN_WORKER_IDENTITY,
            "task_id": str(result.get("task_id") or "") == relay_task_id,
            "request_digest": str(result.get("request_digest") or "")
            == request_digest,
            "source_state_version": int(
                result.get("source_shared_state_version") or 0
            )
            == int(source_state_version),
            "status": str(result.get("status") or "").upper()
            == "DAN_COMPLETE",
            "model": str(result.get("exact_model_id") or "")
            == EXPECTED_QWEN_MODEL,
            "model_sha": str(result.get("model_sha256") or "").lower()
            == EXPECTED_QWEN_SHA256,
            "server_sha": str(result.get("server_sha256") or "").lower()
            == EXPECTED_QWEN_SERVER_SHA256,
            "route": str(result.get("route_id") or "")
            == EXPECTED_QWEN_ROUTE,
            "zero_spend": float(result.get("provider_spend_usd") or 0) == 0,
            "no_side_effects": str(result.get("side_effects") or "").upper()
            == "NONE",
        }
        if not all(checks.values()):
            raise DanWorkerRelayError(
                "dan_worker_result_contract_mismatch:" + _compact(checks)
            )
        return {
            "identity": DAN_WORKER_IDENTITY,
            "attempt_id": attempt_id,
            "source_state_version": int(source_state_version),
            "ownership_fence": ownership_fence,
            "return_worker_kind": return_worker_kind,
            "model_id": str(result.get("exact_model_id") or ""),
            "response_sha256": str(result.get("response_digest") or ""),
            "result": result.get("result"),
            "evidence": list(result.get("evidence") or []),
            "limitations": list(result.get("limitations") or []),
            "recommended_next_action": result.get(
                "recommended_next_action"
            ),
            "acceptance": "WATCH_RECOVERY_CONTEXT_ONLY",
            "package_path": result.get("package_path"),
            "provider_spend_usd": 0,
        }

    def recover(
        self,
        *,
        handoff_id: str,
        task_id: str,
        subtask_id: str,
        objective: str,
        failure_class: str,
        evidence: list[str],
        source_state_version: int,
        ownership_fence: str,
        return_worker_kind: str,
    ) -> dict[str, Any]:
        attempt_id = self.attempt_id(handoff_id)
        relay_task_id = attempt_id
        now = datetime.now(timezone.utc)
        job = {
            "schema": "JAYTEC_DAN_JOB_V1",
            "principal": DAN_WORKER_IDENTITY,
            "task_id": relay_task_id,
            "assignment_name": "WATCH_RECOVERY_" + task_id[:80],
            "original_task_id": task_id,
            "original_handoff_id": handoff_id,
            "subtask_id": subtask_id,
            "attempt_id": attempt_id,
            "source_shared_state_version": int(source_state_version),
            "objective": objective[:8000],
            "worker_failure": {
                "failure_class": failure_class[:200],
                "unresolved_items": [
                    str(item)[:2000] for item in evidence[:20]
                ],
                "return_worker_kind": return_worker_kind[:120],
                "ownership_fence": ownership_fence[:240],
            },
            "watch_reason": (
                "worker evidence does not satisfy acceptance contract"
            ),
            "cost_policy": "ZERO_SPEND",
            "side_effect_policy": "PACKAGE_ONLY",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        }
        request_digest = _digest(job)
        job["request_digest"] = request_digest
        self.store.submit(job)

        deadline = self._monotonic() + self.timeout_seconds
        while self._monotonic() < deadline:
            result = self.store.result(
                relay_task_id=relay_task_id,
                request_digest=request_digest,
            )
            if result is not None:
                return self._validate_result(
                    result,
                    relay_task_id=relay_task_id,
                    request_digest=request_digest,
                    source_state_version=source_state_version,
                    ownership_fence=ownership_fence,
                    return_worker_kind=return_worker_kind,
                    attempt_id=attempt_id,
                )
            self._sleep(self.poll_interval_seconds)
        raise DanWorkerRelayError("dan_worker_result_timeout")


class DanRelayMiddleware:
    """Dedicated private HTTP transport for the local DAN recovery seat."""

    def __init__(self, app):
        self.app = app
        self.enabled = DAN_HTTP_RELAY_ENABLED
        self.token = DAN_HTTP_RELAY_TOKEN
        database_url = os.environ.get("DATABASE_URL", "").strip()
        self.store = (
            PostgresDanRelayStore(database_url)
            if self.enabled and database_url
            else None
        )
        if self.enabled and not self.token:
            raise RuntimeError("dan_http_relay_token_missing")
        if self.enabled and self.store is None:
            raise RuntimeError("dan_http_relay_database_missing")
        if self.enabled and DAN_RELAY_SCHEMA_ENABLED:
            self.store.ensure_schema()

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path") or "")
        if path not in {
            "/dan-relay/v1/claim",
            "/dan-relay/v1/complete",
            "/dan-relay/v1/status",
        }:
            return await self.app(scope, receive, send)
        if not self.enabled or self.store is None:
            return await self._json_response(
                send, 404, {"ok": False, "error": "relay_disabled"}
            )
        try:
            supplied = self._bearer(scope)
            if not supplied or not hmac.compare_digest(
                supplied, self.token
            ):
                return await self._json_response(
                    send, 401, {"ok": False, "error": "unauthorized"}
                )
            method = str(scope.get("method") or "").upper()
            if path == "/dan-relay/v1/status":
                if method != "GET":
                    return await self._json_response(
                        send, 405, {"ok": False, "error": "method_not_allowed"}
                    )
                return await self._json_response(
                    send,
                    200,
                    {
                        "ok": True,
                        "schema": "JAYTEC_DAN_PRIVATE_RELAY_V1",
                        "queue": self.store.status(),
                    },
                )

            if method != "POST":
                return await self._json_response(
                    send, 405, {"ok": False, "error": "method_not_allowed"}
                )
            payload = await self._json_body(receive)
            if path == "/dan-relay/v1/claim":
                lease = int(payload.get("lease_seconds") or 300)
                row = self.store.claim(lease_seconds=min(900, lease))
                if row is None:
                    return await self._json_response(
                        send, 200, {"ok": True, "job": None}
                    )
                return await self._json_response(
                    send,
                    200,
                    {
                        "ok": True,
                        "job": row["request"],
                        "claim_token": row["claim_token"],
                        "claimed_until": str(row["claimed_until"]),
                    },
                )

            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise DanRelayStoreError("dan_relay_result_object_required")
            self.store.complete(
                relay_task_id=str(payload.get("task_id") or ""),
                request_digest=str(payload.get("request_digest") or ""),
                claim_token=str(payload.get("claim_token") or ""),
                result=result,
            )
            return await self._json_response(
                send, 200, {"ok": True, "status": "COMPLETE"}
            )
        except json.JSONDecodeError:
            return await self._json_response(
                send, 400, {"ok": False, "error": "invalid_json"}
            )
        except DanRelayStoreError as exc:
            return await self._json_response(
                send, 409, {"ok": False, "error": str(exc)}
            )
        except Exception as exc:
            return await self._json_response(
                send,
                502,
                {
                    "ok": False,
                    "error": "dan_relay_failed:" + type(exc).__name__,
                },
            )

    @staticmethod
    def _bearer(scope: Mapping[str, Any]) -> str:
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                raw = value.decode("utf-8", "ignore")
                if raw.lower().startswith("bearer "):
                    return raw.split(" ", 1)[1].strip()
        return ""

    @staticmethod
    async def _json_body(receive) -> dict[str, Any]:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise DanRelayStoreError("dan_relay_body_too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        if not chunks:
            return {}
        value = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(value, dict):
            raise DanRelayStoreError("dan_relay_body_must_be_object")
        return value

    @staticmethod
    async def _json_response(send, status: int, payload: Mapping[str, Any]):
        body = _compact(dict(payload)).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": body, "more_body": False}
        )
