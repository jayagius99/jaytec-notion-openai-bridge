from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

DAN_WORKER_REQUEST_MARKER = "JAYTEC_DAN_WORKER_JOB_V1"
DAN_WORKER_RESULT_MARKER = "JAYTEC_DAN_WORKER_RESULT_V1"
DAN_WORKER_IDENTITY = "DAN-WORKER"

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
        attempt_id: str,
        task_id: str,
        subtask_id: str,
        source_state_version: int,
        ownership_fence: str,
        return_worker_kind: str,
    ) -> Optional[dict[str, Any]]:
        for comment in self._comments():
            result = _parse_marker(
                str(comment.get("body") or ""),
                DAN_WORKER_RESULT_MARKER,
            )
            if result is None:
                continue
            if str(result.get("identity") or "") != DAN_WORKER_IDENTITY:
                continue
            if str(result.get("attempt_id") or "") != attempt_id:
                continue
            checks = {
                "task_id": str(result.get("task_id") or "") == task_id,
                "subtask_id": str(result.get("subtask_id") or "") == subtask_id,
                "source_state_version": int(
                    result.get("source_state_version") or 0
                )
                == int(source_state_version),
                "ownership_fence": str(result.get("ownership_fence") or "")
                == ownership_fence,
                "return_worker_kind": str(result.get("return_worker_kind") or "")
                == return_worker_kind,
                "candidate_only": str(result.get("acceptance") or "")
                == "UNACCEPTED_CANDIDATE",
                "zero_spend": float(result.get("provider_spend_usd") or 0) == 0,
            }
            if not all(checks.values()):
                raise DanWorkerRelayError(
                    "dan_worker_result_contract_mismatch:" + _compact(checks)
                )
            receipt = result.get("qwen_receipt")
            if not isinstance(receipt, Mapping):
                raise DanWorkerRelayError("dan_worker_qwen_receipt_missing")
            if str(receipt.get("identity") or "") != DAN_WORKER_IDENTITY:
                raise DanWorkerRelayError("dan_worker_qwen_identity_mismatch")
            if int(receipt.get("http_status") or 0) != 200:
                raise DanWorkerRelayError("dan_worker_qwen_http_not_success")
            if float(receipt.get("provider_spend_usd") or 0) != 0:
                raise DanWorkerRelayError("dan_worker_qwen_nonzero_spend")
            candidate = receipt.get("candidate")
            if not isinstance(candidate, Mapping):
                raise DanWorkerRelayError("dan_worker_candidate_missing")
            if str(candidate.get("status") or "").upper() != "SUCCESS":
                raise DanWorkerRelayError("dan_worker_candidate_not_success")
            return dict(result)
        return None

    def _request_exists(self, attempt_id: str) -> bool:
        for comment in self._comments():
            job = _parse_marker(
                str(comment.get("body") or ""),
                DAN_WORKER_REQUEST_MARKER,
            )
            if (
                isinstance(job, Mapping)
                and str(job.get("attempt_id") or "") == attempt_id
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
        existing = self._matching_result(
            attempt_id=attempt_id,
            task_id=task_id,
            subtask_id=subtask_id,
            source_state_version=source_state_version,
            ownership_fence=ownership_fence,
            return_worker_kind=return_worker_kind,
        )
        if existing is not None:
            return existing

        if not self._request_exists(attempt_id):
            job = {
                "identity": DAN_WORKER_IDENTITY,
                "assignment_name": "WATCH_RECOVERY_" + task_id[:80],
                "task_id": task_id,
                "subtask_id": subtask_id,
                "attempt_id": attempt_id,
                "objective": objective[:8000],
                "failure_class": failure_class[:200],
                "evidence": [str(item)[:2000] for item in evidence[:20]],
                "source_state_version": int(source_state_version),
                "ownership_fence": ownership_fence[:240],
                "return_worker_kind": return_worker_kind[:120],
            }
            status, value = self._request(
                "POST",
                "/comments",
                {
                    "body": DAN_WORKER_REQUEST_MARKER + "\n" + _compact(job),
                },
            )
            if status != 201 or not isinstance(value, Mapping):
                raise DanWorkerRelayError(
                    "dan_worker_request_post_failed:" + str(status)
                )

        deadline = self._monotonic() + self.config.timeout_seconds
        while self._monotonic() < deadline:
            result = self._matching_result(
                attempt_id=attempt_id,
                task_id=task_id,
                subtask_id=subtask_id,
                source_state_version=source_state_version,
                ownership_fence=ownership_fence,
                return_worker_kind=return_worker_kind,
            )
            if result is not None:
                return result
            self._sleep(self.config.poll_interval_seconds)
        raise DanWorkerRelayError("dan_worker_result_timeout")
