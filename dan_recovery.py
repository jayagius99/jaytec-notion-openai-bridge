from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import psycopg2
import psycopg2.extras

from durable_tasks import contains_secret_material
from five_seat_signals import WORK_AVAILABLE_CHANNEL

DAN_OWNER_ID = "DAN-OWNER"
DAN_RECOVERY_ID = "DAN-RECOVERY-SEAT"
DAN_JOB_MARKER = "JAYTEC_DAN_JOB_V1"
DAN_RESULT_MARKER = "JAYTEC_DAN_RESULT_V1"
DAN_RELAY_REPO_DEFAULT = "jayagius99/jaytec-work-engine-v2-g1"
DAN_RELAY_ISSUE_DEFAULT = 130
EXPECTED_QWEN_MODEL = r"C:\JAYTEC_BOOTSTRAP\Scratch\local-model-proof\qwen2.5-1.5b-instruct-q4_k_m.gguf"
EXPECTED_QWEN_SHA256 = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
EXPECTED_QWEN_ROUTE = "local-llama-127.0.0.1:18081"
_HARD_BOUNDARY_TERMS = (
    "policy", "permission", "secret", "credential", "privacy", "spend",
    "owner approval", "hold", "stop", "canonical authority", "unsafe",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_marker(body: str, marker: str) -> dict[str, Any] | None:
    if marker not in body:
        return None
    tail = body.split(marker, 1)[1].strip()
    if tail.startswith("```json"):
        tail = tail[len("```json"):].strip()
        if "```" in tail:
            tail = tail.split("```", 1)[0].strip()
    elif tail.startswith("```"):
        tail = tail[3:].strip()
        if "```" in tail:
            tail = tail.split("```", 1)[0].strip()
    try:
        value = json.loads(tail)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def recoverable_watch_block(*, decision: str, reason: str, result: Mapping[str, Any]) -> tuple[bool, str]:
    if str(decision).upper() != "BLOCK":
        return False, "watch_decision_not_block"
    partial = str(result.get("partial_side_effect_status") or "NONE").upper()
    if partial not in {"NONE", "VERIFIED_NOT_DONE"}:
        return False, "ambiguous_or_partial_side_effect"
    overall = str(result.get("whole_packet_status") or result.get("overall_status") or "FAILED_CLOSED").upper()
    if overall in {"SUCCESS", "POLICY_BLOCKED"}:
        return False, "terminal_or_policy_status"
    text = " ".join([
        str(reason or ""),
        _canonical(result.get("unresolved_items") or []),
        str(result.get("worker_completion_classification") or ""),
    ]).lower()
    if any(term in text for term in _HARD_BOUNDARY_TERMS):
        return False, "hard_boundary_detected"
    return True, "eligible_worker_capability_failure"


class GitHubDanRelay:
    def __init__(self, token: str, repo: str, issue: int):
        self.token = str(token or "").strip()
        self.repo = str(repo or "").strip()
        self.issue = int(issue)
        if not self.token or "/" not in self.repo or self.issue <= 0:
            raise ValueError("invalid DAN relay configuration")

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/" + path.lstrip("/"),
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self.token,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "JAYTEC-DAN-Recovery/1.0",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(f"DAN_RELAY_HTTP_{exc.code}:{detail}") from exc

    def post_job(self, envelope: Mapping[str, Any]) -> int:
        data = self._request(
            "POST",
            f"repos/{self.repo}/issues/{self.issue}/comments",
            {"body": DAN_JOB_MARKER + "\n```json\n" + json.dumps(dict(envelope), indent=2) + "\n```"},
        )
        return int(data["id"])

    def results(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while page <= 10:
            rows = self._request("GET", f"repos/{self.repo}/issues/{self.issue}/comments?per_page=100&page={page}")
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                parsed = _parse_marker(str(row.get("body") or ""), DAN_RESULT_MARKER)
                if parsed is not None:
                    parsed["_relay_comment_id"] = int(row.get("id") or 0)
                    parsed["_relay_author"] = str((row.get("user") or {}).get("login") or "")
                    out.append(parsed)
            if len(rows) < 100:
                break
            page += 1
        return out


class DanRecoveryManager:
    def __init__(self, database_url: str, relay: GitHubDanRelay):
        self.database_url = database_url
        self.relay = relay
        self._last_poll = 0.0

    @classmethod
    def from_env(cls, database_url: str) -> "DanRecoveryManager | None":
        enabled = os.environ.get("JAYTEC_DAN_RECOVERY_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return None
        token = os.environ.get("JAYTEC_GITHUB_BROKER_TOKEN", "").strip()
        if not token:
            return None
        repo = os.environ.get("JAYTEC_DAN_RELAY_REPO", DAN_RELAY_REPO_DEFAULT).strip()
        issue = int(os.environ.get("JAYTEC_DAN_RELAY_ISSUE", str(DAN_RELAY_ISSUE_DEFAULT)))
        return cls(database_url, GitHubDanRelay(token, repo, issue))

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def dispatch_watch_block(self, handoff: Mapping[str, Any], result: Mapping[str, Any], *, decision: str, reason: str) -> dict[str, Any]:
        eligible, why = recoverable_watch_block(decision=decision, reason=reason, result=result)
        if not eligible:
            return {"dispatched": False, "reason": why}
        job_id = str(handoff.get("job_id") or "")
        handoff_id = str(handoff.get("handoff_id") or "")
        if not job_id or not handoff_id:
            return {"dispatched": False, "reason": "missing_job_or_handoff"}
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT j.task_id,j.objective,j.source_shared_state_version,j.fabric_state,
                              e.worker_kind,e.payload
                       FROM jaytec_jobs j JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                       WHERE j.job_id=%s""",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return {"dispatched": False, "reason": "job_not_found"}
                cur.execute(
                    """SELECT 1 FROM jaytec_job_events
                       WHERE job_id=%s AND event_type='DAN_RECOVERY_DISPATCHED'
                         AND payload->>'handoff_id'=%s LIMIT 1""",
                    (job_id, handoff_id),
                )
                if cur.fetchone() is not None:
                    return {"dispatched": False, "reason": "already_dispatched"}
                compact_result = {
                    "whole_packet_status": result.get("whole_packet_status"),
                    "worker_completion_classification": result.get("worker_completion_classification"),
                    "unresolved_items": list(result.get("unresolved_items") or [])[:20],
                    "provider_identity": result.get("provider_identity"),
                    "evidence": list(result.get("evidence") or [])[:20],
                }
                envelope = {
                    "schema": DAN_JOB_MARKER,
                    "principal": DAN_RECOVERY_ID,
                    "task_id": "dan-recovery-" + hashlib.sha256((job_id + "|" + handoff_id).encode()).hexdigest()[:20],
                    "assignment_name": "WATCH_RECOVERY_" + str(row["task_id"])[:80],
                    "original_job_id": job_id,
                    "original_handoff_id": handoff_id,
                    "original_worker_kind": str(row["worker_kind"]),
                    "source_shared_state_version": int(row["source_shared_state_version"]),
                    "objective": str(row["objective"])[:8000],
                    "worker_failure": compact_result,
                    "watch_reason": str(reason)[:2000],
                    "cost_policy": "ZERO_SPEND",
                    "side_effect_policy": "PACKAGE_ONLY",
                    "created_at": _utc_now().isoformat(),
                    "expires_at": (_utc_now() + timedelta(minutes=30)).isoformat(),
                }
                if contains_secret_material(envelope):
                    return {"dispatched": False, "reason": "secret_material_refused"}
                envelope["request_digest"] = _digest(envelope)
                comment_id = self.relay.post_job(envelope)
                cur.execute(
                    """INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                       VALUES (%s,'DAN_RECOVERY_DISPATCHED',%s,%s,%s::jsonb)""",
                    (job_id, DAN_RECOVERY_ID, int(row["source_shared_state_version"]), json.dumps({
                        "handoff_id": handoff_id,
                        "request_digest": envelope["request_digest"],
                        "relay_comment_id": comment_id,
                        "principal": DAN_RECOVERY_ID,
                    })),
                )
        return {"dispatched": True, "comment_id": comment_id, "request_digest": envelope["request_digest"]}

    def poll_results_and_requeue(self) -> dict[str, Any]:
        now = time.time()
        if now - self._last_poll < 8.0:
            return {"polled": False, "accepted": []}
        self._last_poll = now
        accepted: list[str] = []
        for receipt in self.relay.results():
            if receipt.get("principal") != DAN_RECOVERY_ID:
                continue
            if str(receipt.get("status") or "").upper() != "DAN_COMPLETE":
                continue
            if receipt.get("exact_model_id") != EXPECTED_QWEN_MODEL:
                continue
            if str(receipt.get("model_sha256") or "").lower() != EXPECTED_QWEN_SHA256:
                continue
            if receipt.get("route_id") != EXPECTED_QWEN_ROUTE:
                continue
            if receipt.get("provider_spend_usd") != 0:
                continue
            if str(receipt.get("side_effects") or "").upper() != "NONE":
                continue
            job_id = str(receipt.get("original_job_id") or "")
            handoff_id = str(receipt.get("original_handoff_id") or "")
            if job_id and handoff_id and self._accept_and_requeue(job_id, handoff_id, receipt):
                accepted.append(job_id)
        return {"polled": True, "accepted": accepted}

    def _accept_and_requeue(self, job_id: str, handoff_id: str, receipt: Mapping[str, Any]) -> bool:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("jaytec-dan-recovery:" + job_id,))
                cur.execute(
                    """SELECT j.*,a.current_shared_state_version
                       FROM jaytec_jobs j
                       JOIN jaytec_fabric_authority_state a ON a.authority_id='FABRIC'
                       WHERE j.job_id=%s FOR UPDATE OF j,a""",
                    (job_id,),
                )
                job = cur.fetchone()
                if job is None:
                    return False
                cur.execute("SELECT decision FROM jaytec_watch_reviews WHERE job_id=%s AND handoff_id=%s", (job_id, handoff_id))
                review = cur.fetchone()
                if review is None or str(review["decision"]).upper() != "BLOCK":
                    return False
                cur.execute(
                    """SELECT payload FROM jaytec_job_events
                       WHERE job_id=%s AND event_type='DAN_RECOVERY_DISPATCHED'
                         AND payload->>'handoff_id'=%s ORDER BY event_id DESC LIMIT 1""",
                    (job_id, handoff_id),
                )
                dispatched = cur.fetchone()
                if dispatched is None:
                    return False
                dispatch_payload = dict(dispatched["payload"] or {})
                if receipt.get("request_digest") != dispatch_payload.get("request_digest"):
                    return False
                cur.execute(
                    """SELECT 1 FROM jaytec_job_events
                       WHERE job_id=%s AND event_type='DAN_RECOVERY_ACCEPTED'
                         AND payload->>'handoff_id'=%s LIMIT 1""",
                    (job_id, handoff_id),
                )
                if cur.fetchone() is not None:
                    return False
                source_version = int(job["source_shared_state_version"])
                if source_version != int(job["current_shared_state_version"]):
                    return False
                if str(job["status"]) != "BLOCKED" or str(job["fabric_state"]) != "QUARANTINED":
                    return False
                if job.get("seat_id") is not None or job.get("lease_owner") is not None:
                    return False
                context = {
                    "source": "DAN_RECOVERY",
                    "principal": DAN_RECOVERY_ID,
                    "handoff_id": handoff_id,
                    "request_digest": receipt.get("request_digest"),
                    "response_digest": receipt.get("response_digest"),
                    "relay_comment_id": receipt.get("_relay_comment_id"),
                    "package_path": receipt.get("package_path"),
                    "result": receipt.get("result"),
                    "evidence": receipt.get("evidence"),
                    "limitations": receipt.get("limitations"),
                    "recommended_next_action": receipt.get("recommended_next_action"),
                }
                if contains_secret_material(context):
                    return False
                cur.execute(
                    """UPDATE jaytec_jobs
                       SET status='QUEUED',fabric_state='REWORK_QUEUED',blockers=%s::jsonb,
                           next_attempt_at=now(),fabric_attempt_count=0,
                           fabric_rework_count=fabric_rework_count+1,cancel_requested_at=NULL,
                           version=version+1,updated_at=now()
                       WHERE job_id=%s AND status='BLOCKED' AND fabric_state='QUARANTINED'
                         AND seat_id IS NULL AND lease_owner IS NULL
                       RETURNING job_id""",
                    (json.dumps([context]), job_id),
                )
                if cur.fetchone() is None:
                    return False
                cur.execute(
                    """INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                       VALUES (%s,'DAN_RECOVERY_ACCEPTED',%s,%s,%s::jsonb)""",
                    (job_id, DAN_RECOVERY_ID, source_version, json.dumps({
                        "handoff_id": handoff_id,
                        "request_digest": receipt.get("request_digest"),
                        "response_digest": receipt.get("response_digest"),
                        "relay_comment_id": receipt.get("_relay_comment_id"),
                        "package_path": receipt.get("package_path"),
                    })),
                )
                cur.execute("SELECT pg_notify(%s,%s)", (WORK_AVAILABLE_CHANNEL, json.dumps({"reason": "dan_recovery_accepted", "job_id": job_id})))
        return True
