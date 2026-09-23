from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

DAN_WORKER_REQUEST_MARKER = "JAYTEC_DAN_JOB_V1"
DAN_WORKER_RESULT_MARKER = "JAYTEC_DAN_RESULT_V1"
DAN_WORKER_IDENTITY = "DAN-RECOVERY-SEAT"
TRUSTED_RELAY_LOGIN = "jayagius99"
EXPECTED_QWEN_MODEL = r"C:\\JAYTEC_BOOTSTRAP\\Scratch\\local-model-proof\\qwen2.5-1.5b-instruct-q4_k_m.gguf"
EXPECTED_QWEN_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
EXPECTED_QWEN_SERVER_SHA256 = "06f5c5463753a7a6fe729bb436a6d3ab5e71373527559339b42cec9fd7f1d27f"
EXPECTED_QWEN_ROUTE = "local-llama-127.0.0.1:18081"

Transport = Callable[
    [str, str, Mapping[str, str], Optional[Mapping[str, Any]]],
    tuple[int, Any],
]


class DanWorkerRelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class DanWorkerRelayConfig:
    token: str
    repository: str
    issue_number: int
    poll_interval_seconds: float = 3.0
    timeout_seconds: float = 180.0

    @classmethod
    def build(
        cls,
        token: str,
        repository: str,
        issue_number: int,
        *,
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float = 180.0,
    ) -> "DanWorkerRelayConfig":
        clean_token = str(token or "").strip()
        clean_repo = str(repository or "").strip()
        issue = int(issue_number)
        if not clean_token:
            raise DanWorkerRelayError("dan_worker_relay_token_missing")
        if "/" not in clean_repo:
            raise DanWorkerRelayError("dan_worker_relay_repository_invalid")
        if issue <= 0:
            raise DanWorkerRelayError("dan_worker_relay_issue_invalid")
        return cls(
            token=clean_token,
            repository=clean_repo,
            issue_number=issue,
            poll_interval_seconds=max(0.25, float(poll_interval_seconds)),
            timeout_seconds=max(5.0, float(timeout_seconds)),
        )


def _compact(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _parse_marker(body: str, marker: str) -> Optional[dict[str, Any]]:
    text = str(body or "")
    index = text.find(marker)
    if index < 0:
        return None
    rest = text[index + len(marker) :].strip()
    if rest.startswith("```json"):
        rest = rest[len("```json") :].strip()
    elif rest.startswith("```"):
        rest = rest[len("```") :].strip()
    if "```" in rest:
        rest = rest.split("```", 1)[0].strip()
    try:
        value = json.loads(rest)
    except Exception:
        return None
    return dict(value) if isinstance(value, Mapping) else None


class DanWorkerRelay:
    """Durable WATCH <-> local DAN-WORKER rendezvous through one private issue.

    The relay transports bounded recovery envelopes and candidate evidence only.
    It does not grant DAN canonical authority and it never exposes the GitHub
    credential to the local job payload.
    """

    def __init__(
        self,
        config: DanWorkerRelayConfig,
        *,
        transport: Optional[Transport] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._transport = transport or self._urllib_transport
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn

    def _urllib_transport(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[Mapping[str, Any]],
    ) -> tuple[int, Any]:
        encoded = None if body is None else _compact(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            method=method,
            headers=dict(headers),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(1_000_001)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read(1_000_001)
            status = int(exc.code)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DanWorkerRelayError(
                "dan_worker_relay_network_error:" + type(exc).__name__
            ) from exc
        if len(raw) > 1_000_000:
            raise DanWorkerRelayError("dan_worker_relay_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else None
        except Exception as exc:
            raise DanWorkerRelayError("dan_worker_relay_json_invalid") from exc
        return status, value

    def _request(
        self,
        method: str,
        suffix: str,
        body: Optional[Mapping[str, Any]] = None,
    ) -> tuple[int, Any]:
        url = (
            "https://api.github.com/repos/"
            + self.config.repository
            + "/issues/"
            + str(self.config.issue_number)
            + suffix
        )
        headers = {
            "Authorization": "Bearer " + self.config.token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "JAYTEC-DAN-WATCH-RELAY/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        return self._transport(method, url, headers, body)

    def _comments(self) -> list[Mapping[str, Any]]:
        status, value = self._request(
            "GET",
            "/comments?per_page=100&sort=created&direction=desc",
        )
        if status != 200 or not isinstance(value, list):
            raise DanWorkerRelayError(
                "dan_worker_relay_comments_failed:" + str(status)
            )
        return [row for row in value if isinstance(row, Mapping)]

    @staticmethod
    def attempt_id(handoff_id: str) -> str:
        digest = hashlib.sha256(str(handoff_id).encode("utf-8")).hexdigest()[:24]
        return "watch-dan-" + digest

    def _matching_result(
        self,
        *,
        relay_task_id: str,
        request_digest: str,
        source_state_version: int,
        ownership_fence: str,
        return_worker_kind: str,
        attempt_id: str,
    ) -> Optional[dict[str, Any]]:
        for comment in self._comments():
            login = str((comment.get("user") or {}).get("login") or "").lower()
            if login != TRUSTED_RELAY_LOGIN.lower():
                continue
            result = _parse_marker(
                str(comment.get("body") or ""),
                DAN_WORKER_RESULT_MARKER,
            )
            if result is None:
                continue
            if str(result.get("principal") or "") != DAN_WORKER_IDENTITY:
                continue
            if str(result.get("task_id") or "") != relay_task_id:
                continue
            if str(result.get("request_digest") or "") != request_digest:
                continue
            checks = {
                "source_state_version": int(
                    result.get("source_shared_state_version") or 0
                ) == int(source_state_version),
                "status": str(result.get("status") or "").upper() == "DAN_COMPLETE",
                "model": str(result.get("exact_model_id") or "") == EXPECTED_QWEN_MODEL,
                "model_sha": str(result.get("model_sha256") or "").lower() == EXPECTED_QWEN_SHA256,
                "server_sha": str(result.get("server_sha256") or "").lower() == EXPECTED_QWEN_SERVER_SHA256,
                "route": str(result.get("route_id") or "") == EXPECTED_QWEN_ROUTE,
                "zero_spend": float(result.get("provider_spend_usd") or 0) == 0,
                "no_side_effects": str(result.get("side_effects") or "").upper() == "NONE",
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
                "recommended_next_action": result.get("recommended_next_action"),
                "acceptance": "WATCH_RECOVERY_CONTEXT_ONLY",
                "package_path": result.get("package_path"),
                "provider_spend_usd": 0,
            }
        return None

    def _request_exists(self, relay_task_id: str, request_digest: str) -> bool:
        for comment in self._comments():
            login = str((comment.get("user") or {}).get("login") or "").lower()
            if login != TRUSTED_RELAY_LOGIN.lower():
                continue
            job = _parse_marker(
                str(comment.get("body") or ""),
                DAN_WORKER_REQUEST_MARKER,
            )
            if (
                isinstance(job, Mapping)
                and str(job.get("task_id") or "") == relay_task_id
                and str(job.get("request_digest") or "") == request_digest
            ):
                return True
        return False

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
            "schema": DAN_WORKER_REQUEST_MARKER,
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
                "unresolved_items": [str(item)[:2000] for item in evidence[:20]],
                "return_worker_kind": return_worker_kind[:120],
                "ownership_fence": ownership_fence[:240],
            },
            "watch_reason": "worker evidence does not satisfy acceptance contract",
            "cost_policy": "ZERO_SPEND",
            "side_effect_policy": "PACKAGE_ONLY",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        }
        request_digest = hashlib.sha256(_compact(job).encode("utf-8")).hexdigest()
        job["request_digest"] = request_digest

        existing = self._matching_result(
            relay_task_id=relay_task_id,
            request_digest=request_digest,
            source_state_version=source_state_version,
            ownership_fence=ownership_fence,
            return_worker_kind=return_worker_kind,
            attempt_id=attempt_id,
        )
        if existing is not None:
            return existing

        if not self._request_exists(relay_task_id, request_digest):
            status, value = self._request(
                "POST",
                "/comments",
                {
                    "body": DAN_WORKER_REQUEST_MARKER + "\n\`\`\`json\n"
                    + json.dumps(job, indent=2, ensure_ascii=False)
                    + "\n\`\`\`",
                },
            )
            if status != 201 or not isinstance(value, Mapping):
                raise DanWorkerRelayError(
                    "dan_worker_request_post_failed:" + str(status)
                )

        deadline = self._monotonic() + self.config.timeout_seconds
        while self._monotonic() < deadline:
            result = self._matching_result(
                relay_task_id=relay_task_id,
                request_digest=request_digest,
                source_state_version=source_state_version,
                ownership_fence=ownership_fence,
                return_worker_kind=return_worker_kind,
                attempt_id=attempt_id,
            )
            if result is not None:
                return result
            self._sleep(self.config.poll_interval_seconds)
        raise DanWorkerRelayError("dan_worker_result_timeout")

