from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import psycopg2
import psycopg2.extras


REQUEST_SCHEMA = "JAYTEC_DAN_JOB_V1"
RESULT_SCHEMA = "JAYTEC_DAN_RESULT_V1"
RECOVERY_PRINCIPAL = "DAN-RECOVERY-SEAT"
EXPECTED_QWEN_MODEL = r"C:\\JAYTEC_BOOTSTRAP\\Scratch\\local-model-proof\\qwen2.5-1.5b-instruct-q4_k_m.gguf"
EXPECTED_QWEN_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
EXPECTED_QWEN_SERVER_SHA256 = "06f5c5463753a7a6fe729bb436a6d3ab5e71373527559339b42cec9fd7f1d27f"
EXPECTED_QWEN_ROUTE = "local-llama-127.0.0.1:18081"


class DanRelayStoreError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class PostgresDanRelay:
    """Durable WATCH <-> PC DAN recovery relay.

    This store carries bounded recovery envelopes and result evidence only.
    It grants no canonical-write authority and contains no provider credentials.
    """

    def __init__(
        self,
        database_url: str,
        *,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 180.0,
    ):
        if not str(database_url or "").strip():
            raise DanRelayStoreError("dan_relay_database_url_missing")
        self.database_url = str(database_url).strip()
        self.poll_interval_seconds = max(0.25, float(poll_interval_seconds))
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jaytec_dan_relay_jobs(
                      request_id TEXT PRIMARY KEY,
                      request_digest TEXT NOT NULL,
                      principal TEXT NOT NULL,
                      request_payload JSONB NOT NULL,
                      status TEXT NOT NULL,
                      claim_owner TEXT,
                      claim_expires_at TIMESTAMPTZ,
                      result_payload JSONB,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                      expires_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS jaytec_dan_relay_jobs_status_idx
                    ON jaytec_dan_relay_jobs(status,expires_at,created_at)
                    """
                )

    @staticmethod
    def _request_id(handoff_id: str) -> str:
        return "watch-dan-" + hashlib.sha256(
            str(handoff_id).encode("utf-8")
        ).hexdigest()[:24]

    def _build_request(
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
        now = datetime.now(timezone.utc)
        request_id = self._request_id(handoff_id)
        payload = {
            "schema": REQUEST_SCHEMA,
            "principal": RECOVERY_PRINCIPAL,
            "task_id": request_id,
            "assignment_name": "WATCH_RECOVERY_" + str(task_id)[:80],
            "original_task_id": str(task_id),
            "original_handoff_id": str(handoff_id),
            "subtask_id": str(subtask_id),
            "attempt_id": request_id,
            "source_shared_state_version": int(source_state_version),
            "objective": str(objective)[:8000],
            "worker_failure": {
                "failure_class": str(failure_class)[:200],
                "unresolved_items": [str(item)[:2000] for item in evidence[:20]],
                "return_worker_kind": str(return_worker_kind)[:120],
                "ownership_fence": str(ownership_fence)[:240],
            },
            "watch_reason": "worker evidence does not satisfy acceptance contract",
            "cost_policy": "ZERO_SPEND",
            "side_effect_policy": "PACKAGE_ONLY",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        }
        payload["request_digest"] = _digest(payload)
        return payload

    def _submit(self, payload: Mapping[str, Any]) -> None:
        request_id = str(payload["task_id"])
        request_digest = str(payload["request_digest"])
        expires_at = datetime.fromisoformat(
            str(payload["expires_at"]).replace("Z", "+00:00")
        )
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_dan_relay_jobs(
                      request_id,request_digest,principal,request_payload,status,expires_at
                    ) VALUES (%s,%s,%s,%s::jsonb,'PENDING',%s)
                    ON CONFLICT (request_id) DO NOTHING
                    """,
                    (
                        request_id,
                        request_digest,
                        RECOVERY_PRINCIPAL,
                        _canonical(dict(payload)),
                        expires_at,
                    ),
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        SELECT request_digest,principal
                        FROM jaytec_dan_relay_jobs
                        WHERE request_id=%s
                        """,
                        (request_id,),
                    )
                    row = cur.fetchone()
                    if (
                        row is None
                        or str(row["request_digest"]) != request_digest
                        or str(row["principal"]) != RECOVERY_PRINCIPAL
                    ):
                        raise DanRelayStoreError(
                            "dan_relay_conflicting_duplicate"
                        )

    def _result(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT status,result_payload,expires_at
                    FROM jaytec_dan_relay_jobs
                    WHERE request_id=%s
                    """,
                    (request_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise DanRelayStoreError("dan_relay_request_expired")
        if str(row["status"]) != "COMPLETE":
            return None
        value = row["result_payload"]
        return dict(value) if isinstance(value, Mapping) else None

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
        payload = self._build_request(
            handoff_id=handoff_id,
            task_id=task_id,
            subtask_id=subtask_id,
            objective=objective,
            failure_class=failure_class,
            evidence=evidence,
            source_state_version=source_state_version,
            ownership_fence=ownership_fence,
            return_worker_kind=return_worker_kind,
        )
        self._submit(payload)
        request_id = str(payload["task_id"])
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            result = self._result(request_id)
            if result is not None:
                return self._validate_result(
                    payload,
                    result,
                    ownership_fence=ownership_fence,
                    return_worker_kind=return_worker_kind,
                )
            time.sleep(self.poll_interval_seconds)
        raise DanRelayStoreError("dan_relay_result_timeout")

    @staticmethod
    def _validate_result(
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        ownership_fence: str,
        return_worker_kind: str,
    ) -> dict[str, Any]:
        checks = {
            "schema": str(result.get("schema") or "") == RESULT_SCHEMA,
            "principal": str(result.get("principal") or "") == RECOVERY_PRINCIPAL,
            "status": str(result.get("status") or "").upper() == "DAN_COMPLETE",
            "task_id": str(result.get("task_id") or "") == str(request["task_id"]),
            "request_digest": str(result.get("request_digest") or "")
            == str(request["request_digest"]),
            "source_state_version": int(
                result.get("source_shared_state_version") or 0
            )
            == int(request["source_shared_state_version"]),
            "model": str(result.get("exact_model_id") or "") == EXPECTED_QWEN_MODEL,
            "model_sha": str(result.get("model_sha256") or "").lower()
            == EXPECTED_QWEN_SHA256,
            "server_sha": str(result.get("server_sha256") or "").lower()
            == EXPECTED_QWEN_SERVER_SHA256,
            "route": str(result.get("route_id") or "") == EXPECTED_QWEN_ROUTE,
            "zero_spend": float(result.get("provider_spend_usd") or 0) == 0,
            "no_side_effects": str(result.get("side_effects") or "").upper()
            == "NONE",
        }
        if not all(checks.values()):
            raise DanRelayStoreError(
                "dan_relay_result_contract_mismatch:" + _canonical(checks)
            )
        return {
            "identity": RECOVERY_PRINCIPAL,
            "attempt_id": str(request.get("attempt_id") or ""),
            "source_state_version": int(
                request.get("source_shared_state_version") or 0
            ),
            "ownership_fence": str(ownership_fence),
            "return_worker_kind": str(return_worker_kind),
            "model_id": str(result.get("exact_model_id") or ""),
            "response_sha256": str(result.get("response_digest") or ""),
            "result": result.get("result"),
            "evidence": list(result.get("evidence") or []),
            "limitations": list(result.get("limitations") or []),
            "recommended_next_action": result.get("recommended_next_action"),
            "acceptance": "WATCH_RECOVERY_CONTEXT_ONLY",
            "package_path": result.get("package_path"),
            "provider_spend_usd": 0,
        }

    def claim_pending(
        self,
        *,
        relay_id: str,
        limit: int = 1,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 10))
        lease = max(30, min(int(lease_seconds), 600))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH candidates AS (
                      SELECT request_id
                      FROM jaytec_dan_relay_jobs
                      WHERE expires_at > now()
                        AND (
                          status='PENDING'
                          OR (status='CLAIMED' AND claim_expires_at < now())
                        )
                      ORDER BY created_at ASC
                      FOR UPDATE SKIP LOCKED
                      LIMIT %s
                    )
                    UPDATE jaytec_dan_relay_jobs j
                    SET status='CLAIMED',
                        claim_owner=%s,
                        claim_expires_at=now() + (%s * interval '1 second'),
                        updated_at=now()
                    FROM candidates c
                    WHERE j.request_id=c.request_id
                    RETURNING j.request_id,j.request_digest,j.request_payload,j.expires_at
                    """,
                    (bounded, str(relay_id)[:120], lease),
                )
                rows = cur.fetchall()
        return [
            {
                "request_id": str(row["request_id"]),
                "request_digest": str(row["request_digest"]),
                "request": dict(row["request_payload"]),
                "expires_at": row["expires_at"].isoformat(),
            }
            for row in rows
        ]

    def complete(
        self,
        *,
        relay_id: str,
        request_id: str,
        request_digest: str,
        result: Mapping[str, Any],
    ) -> bool:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT request_digest,request_payload,status,claim_owner,expires_at
                    FROM jaytec_dan_relay_jobs
                    WHERE request_id=%s
                    FOR UPDATE
                    """,
                    (str(request_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise DanRelayStoreError("dan_relay_request_not_found")
                if row["expires_at"] <= datetime.now(timezone.utc):
                    raise DanRelayStoreError("dan_relay_request_expired")
                if str(row["request_digest"]) != str(request_digest):
                    raise DanRelayStoreError("dan_relay_request_digest_mismatch")
                if str(row["claim_owner"] or "") != str(relay_id):
                    raise DanRelayStoreError("dan_relay_claim_owner_mismatch")
                request = dict(row["request_payload"])
                self._validate_result(
                    request,
                    dict(result),
                    ownership_fence=str(
                        (request.get("worker_failure") or {}).get(
                            "ownership_fence", ""
                        )
                    ),
                    return_worker_kind=str(
                        (request.get("worker_failure") or {}).get(
                            "return_worker_kind", ""
                        )
                    ),
                )
                if str(row["status"]) == "COMPLETE":
                    return True
                cur.execute(
                    """
                    UPDATE jaytec_dan_relay_jobs
                    SET status='COMPLETE',
                        result_payload=%s::jsonb,
                        updated_at=now()
                    WHERE request_id=%s
                    """,
                    (_canonical(dict(result)), str(request_id)),
                )
        return True
