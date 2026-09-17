from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import psycopg2
import psycopg2.extras

from concurrency import duplicate_assignment


GUARDIAN_LOCK_KEY = "jaytec-guardian-lite-v1"
REPAIRABLE_FINDING_TYPES = {
    "EXPIRED_JOB_LEASE",
    "STALE_EXECUTION_ROOM",
    "DUPLICATE_ASSIGNMENT",
}
FAILURE_EVENT_TYPES = {
    "SPECIALIST_ROUTE_FAILED",
    "BRIDGE_FAILED",
    "PROVIDER_FAILED",
    "CHECKPOINT_FAILED",
    "SPECIALIST_CAPABILITY_MISMATCH",
    "SPECIALIST_REASONING_ONLY",
    "SPECIALIST_EXECUTION_UNAVAILABLE",
    "SPECIALIST_CAPABILITY_REPORTED",
}


@dataclass(frozen=True)
class GuardianFinding:
    finding_id: str
    severity: str
    finding_type: str
    symptom: str
    job_id: Optional[str] = None
    evidence: Optional[Mapping[str, Any]] = None
    containment: Optional[str] = None
    proposed_repair: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "job_id": self.job_id,
            "severity": self.severity,
            "finding_type": self.finding_type,
            "symptom": self.symptom,
            "evidence": dict(self.evidence or {}),
            "containment": self.containment,
            "proposed_repair": self.proposed_repair,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _stable_finding_id(
    finding_type: str,
    *,
    job_id: Optional[str] = None,
    discriminator: Optional[str] = None,
) -> str:
    raw = "|".join((finding_type, job_id or "", discriminator or ""))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"guardian:{digest}"


def capability_mismatch_from_event(event: Mapping[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if event_type in {
        "SPECIALIST_CAPABILITY_MISMATCH",
        "SPECIALIST_REASONING_ONLY",
        "SPECIALIST_EXECUTION_UNAVAILABLE",
    }:
        return True
    if event_type != "SPECIALIST_CAPABILITY_REPORTED" or not isinstance(payload, Mapping):
        return False
    reasoning_available = payload.get("reasoning_available") is True
    execution_available = payload.get("execution_available") is True
    writable_execution = payload.get("writable_execution") is True
    return reasoning_available and not (execution_available and writable_execution)


def repair_allowed(finding: GuardianFinding) -> bool:
    return finding.finding_type in REPAIRABLE_FINDING_TYPES


def duplicate_findings(jobs: Sequence[Mapping[str, Any]]) -> list[GuardianFinding]:
    findings: list[GuardianFinding] = []
    ordered = sorted(
        jobs,
        key=lambda row: (
            int(row.get("priority") or 100),
            str(row.get("created_at") or ""),
            str(row.get("job_id") or ""),
        ),
    )
    for index, keep in enumerate(ordered):
        for block in ordered[index + 1 :]:
            if duplicate_assignment(block, [keep]) != str(keep.get("job_id") or ""):
                continue
            keep_id = str(keep.get("job_id") or "")
            block_id = str(block.get("job_id") or "")
            if not keep_id or not block_id:
                continue
            findings.append(
                GuardianFinding(
                    finding_id=_stable_finding_id(
                        "DUPLICATE_ASSIGNMENT",
                        job_id=block_id,
                        discriminator=keep_id,
                    ),
                    severity="ERROR",
                    finding_type="DUPLICATE_ASSIGNMENT",
                    job_id=block_id,
                    symptom=f"Assignment {block_id} duplicates active/queued assignment {keep_id} on overlapping scope.",
                    evidence={
                        "keep_job_id": keep_id,
                        "block_job_id": block_id,
                        "keep_status": keep.get("status"),
                        "block_status": block.get("status"),
                    },
                    containment="Do not admit the duplicate assignment while the authoritative assignment remains active.",
                    proposed_repair="Block only the safe queued/paused duplicate; never terminate a live worker automatically.",
                )
            )
    return findings


class GuardianLite:
    """Bounded health audit and fail-closed containment for durable JAYTEC jobs.

    Guardian does not become semantic authority. It may contain mechanically
    provable stale/duplicate execution state, but ambiguous external effects,
    provider failures and capability mismatches are persisted/escalated for
    ChatGPT/OpenAI Lead convergence.
    """

    def __init__(
        self,
        database_url: str,
        *,
        stalled_after_minutes: int = 30,
        event_window_hours: int = 24,
    ):
        if not database_url:
            raise ValueError("database_url is required")
        if stalled_after_minutes < 1:
            raise ValueError("stalled_after_minutes must be positive")
        if event_window_hours < 1:
            raise ValueError("event_window_hours must be positive")
        self.database_url = database_url
        self.stalled_after_minutes = stalled_after_minutes
        self.event_window_hours = event_window_hours

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def audit(self) -> list[GuardianFinding]:
        findings: list[GuardianFinding] = []
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id, lease_owner, lease_expires_at, execution_room_id,
                           ownership_epoch, fence_token, updated_at
                    FROM jaytec_jobs
                    WHERE status='RUNNING'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < now()
                    """
                )
                for row in cur.fetchall():
                    evidence = dict(row)
                    job_id = str(row["job_id"])
                    findings.append(
                        GuardianFinding(
                            finding_id=_stable_finding_id("EXPIRED_JOB_LEASE", job_id=job_id),
                            severity="ERROR",
                            finding_type="EXPIRED_JOB_LEASE",
                            job_id=job_id,
                            symptom=f"Running job {job_id} has an expired ownership lease.",
                            evidence=evidence,
                            containment="Fence the stale execution owner before the job is eligible for takeover.",
                            proposed_repair="If no unresolved external operation exists, increment epoch/fence, clear the stale room/lease and pause the job.",
                        )
                    )
                    if row.get("execution_room_id"):
                        findings.append(
                            GuardianFinding(
                                finding_id=_stable_finding_id("STALE_EXECUTION_ROOM", job_id=job_id),
                                severity="ERROR",
                                finding_type="STALE_EXECUTION_ROOM",
                                job_id=job_id,
                                symptom=f"Execution room {row['execution_room_id']} is still attached to expired job {job_id}.",
                                evidence=evidence,
                                containment="Treat the room as stale and reject any later write carrying its old epoch/fence.",
                                proposed_repair="Clear the room only together with stale-lease fencing after unresolved-operation checks.",
                            )
                        )

                cur.execute(
                    """
                    SELECT job_id, execution_room_id, updated_at, lease_expires_at
                    FROM jaytec_jobs
                    WHERE status='RUNNING'
                      AND updated_at < now() - (%s * interval '1 minute')
                    """,
                    (self.stalled_after_minutes,),
                )
                for row in cur.fetchall():
                    job_id = str(row["job_id"])
                    findings.append(
                        GuardianFinding(
                            finding_id=_stable_finding_id("STALLED_DURABLE_JOB", job_id=job_id),
                            severity="WARNING",
                            finding_type="STALLED_DURABLE_JOB",
                            job_id=job_id,
                            symptom=f"Running job {job_id} has not updated within the configured health window.",
                            evidence=dict(row),
                            containment="Observe lease/heartbeat state; do not infer failure from age alone.",
                            proposed_repair="No automatic repair unless the lease is also expired and side effects are resolved.",
                        )
                    )

                cur.execute(
                    """
                    SELECT operation_id, job_id, status, operation_type, target,
                           intended_effect, updated_at, evidence
                    FROM jaytec_operations
                    WHERE status='UNCERTAIN_PARTIAL'
                       OR (status='IN_FLIGHT' AND updated_at < now() - (%s * interval '1 minute'))
                    """,
                    (self.stalled_after_minutes,),
                )
                for row in cur.fetchall():
                    job_id = str(row["job_id"])
                    operation_id = str(row["operation_id"])
                    finding_type = (
                        "UNCERTAIN_PARTIAL_OPERATION"
                        if row["status"] == "UNCERTAIN_PARTIAL"
                        else "STALLED_IN_FLIGHT_OPERATION"
                    )
                    findings.append(
                        GuardianFinding(
                            finding_id=_stable_finding_id(
                                finding_type,
                                job_id=job_id,
                                discriminator=operation_id,
                            ),
                            severity="CRITICAL" if row["status"] == "UNCERTAIN_PARTIAL" else "ERROR",
                            finding_type=finding_type,
                            job_id=job_id,
                            symptom=f"Operation {operation_id} for job {job_id} is {row['status']} and cannot be blindly retried.",
                            evidence=dict(row),
                            containment="Fail closed. Preserve operation identity and verify destination state before any retry/reclassification.",
                            proposed_repair="Manual/semantic verification must classify the operation VERIFIED_COMPLETE, VERIFIED_NOT_DONE, or FAILED_SAFE.",
                        )
                    )

                cur.execute(
                    """
                    SELECT event_id, job_id, event_type, source, payload, created_at
                    FROM jaytec_job_events
                    WHERE event_type = ANY(%s)
                      AND created_at > now() - (%s * interval '1 hour')
                    ORDER BY event_id DESC
                    LIMIT 250
                    """,
                    (list(FAILURE_EVENT_TYPES), self.event_window_hours),
                )
                for row in cur.fetchall():
                    event = dict(row)
                    event_type = str(row["event_type"])
                    job_id = str(row["job_id"]) if row.get("job_id") else None
                    if capability_mismatch_from_event(event):
                        finding_type = "SPECIALIST_CAPABILITY_MISMATCH"
                        severity = "ERROR"
                        symptom = "Specialist reasoning is available but the requested execution capability is unavailable or mismatched."
                        containment = "Use the specialist only for reasoning/review; route mutation through an authorised execution path."
                    elif event_type == "CHECKPOINT_FAILED":
                        finding_type = "CHECKPOINT_FAILURE"
                        severity = "ERROR"
                        symptom = "A durable checkpoint operation failed."
                        containment = "Do not claim handover/continuity success until a verified checkpoint is written or failure is explicit."
                    elif event_type in {"BRIDGE_FAILED", "PROVIDER_FAILED", "SPECIALIST_ROUTE_FAILED"}:
                        finding_type = "BRIDGE_PROVIDER_FAILURE"
                        severity = "WARNING"
                        symptom = f"Bridge/provider route emitted {event_type}."
                        containment = "Preserve current durable job state and use bounded retry/circuit-breaker policy only when safe."
                    else:
                        continue
                    findings.append(
                        GuardianFinding(
                            finding_id=_stable_finding_id(
                                finding_type,
                                job_id=job_id,
                                discriminator=str(row["event_id"]),
                            ),
                            severity=severity,
                            finding_type=finding_type,
                            job_id=job_id,
                            symptom=symptom,
                            evidence=event,
                            containment=containment,
                            proposed_repair="No blind automatic side-effect retry. Escalate to ChatGPT/OpenAI Lead if the condition remains unresolved.",
                        )
                    )

                cur.execute(
                    """
                    SELECT * FROM jaytec_jobs
                    WHERE status IN ('QUEUED','RUNNING','PAUSED','BLOCKED')
                    ORDER BY priority ASC, created_at ASC, job_id ASC
                    """
                )
                findings.extend(duplicate_findings([dict(row) for row in cur.fetchall()]))

        # De-duplicate identical stable findings produced by overlapping checks.
        deduped: Dict[str, GuardianFinding] = {}
        for finding in findings:
            deduped[finding.finding_id] = finding
        return list(deduped.values())

    def persist(self, findings: Iterable[GuardianFinding]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                for finding in findings:
                    cur.execute(
                        """
                        INSERT INTO jaytec_guardian_findings(
                          finding_id,job_id,severity,finding_type,symptom,evidence,
                          containment,proposed_repair,status
                        ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'OPEN')
                        ON CONFLICT (finding_id) DO UPDATE SET
                          severity=EXCLUDED.severity,
                          symptom=EXCLUDED.symptom,
                          evidence=EXCLUDED.evidence,
                          containment=EXCLUDED.containment,
                          proposed_repair=EXCLUDED.proposed_repair,
                          status=CASE
                            WHEN jaytec_guardian_findings.status='CLOSED' THEN 'OPEN'
                            ELSE jaytec_guardian_findings.status
                          END,
                          updated_at=now()
                        """,
                        (
                            finding.finding_id,
                            finding.job_id,
                            finding.severity,
                            finding.finding_type,
                            finding.symptom,
                            _json(finding.evidence or {}),
                            finding.containment,
                            finding.proposed_repair,
                        ),
                    )

    def _has_unresolved_operation(self, cur, job_id: str) -> bool:
        cur.execute(
            """
            SELECT 1 FROM jaytec_operations
            WHERE job_id=%s AND status IN ('IN_FLIGHT','UNCERTAIN_PARTIAL')
            LIMIT 1
            """,
            (job_id,),
        )
        return cur.fetchone() is not None

    def _contain_expired_job(self, cur, finding: GuardianFinding) -> str:
        if not finding.job_id:
            return "SKIPPED_NO_JOB"
        if self._has_unresolved_operation(cur, finding.job_id):
            return "ESCALATED_UNRESOLVED_OPERATION"
        cur.execute(
            """
            UPDATE jaytec_jobs
            SET status='PAUSED',
                health='DEGRADED',
                lease_owner=NULL,
                lease_expires_at=NULL,
                execution_room_id=NULL,
                ownership_epoch=ownership_epoch+1,
                fence_token=fence_token+1,
                version=version+1,
                updated_at=now()
            WHERE job_id=%s
              AND status='RUNNING'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < now()
            RETURNING job_id, ownership_epoch, fence_token
            """,
            (finding.job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "NO_LONGER_STALE"
        cur.execute(
            """
            INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
            VALUES (%s,'GUARDIAN_STALE_EXECUTION_CONTAINED','GUARDIAN_LITE',%s::jsonb)
            """,
            (
                finding.job_id,
                _json({"finding_id": finding.finding_id, "new_fence_token": row["fence_token"]}),
            ),
        )
        return "CONTAINED"

    def _block_safe_duplicate(self, cur, finding: GuardianFinding) -> str:
        if not finding.job_id:
            return "SKIPPED_NO_JOB"
        if self._has_unresolved_operation(cur, finding.job_id):
            return "ESCALATED_UNRESOLVED_OPERATION"
        cur.execute(
            """
            UPDATE jaytec_jobs
            SET status='BLOCKED', health='BLOCKED', version=version+1, updated_at=now()
            WHERE job_id=%s
              AND status IN ('QUEUED','PAUSED')
              AND (lease_expires_at IS NULL OR lease_expires_at < now())
            RETURNING job_id
            """,
            (finding.job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "SKIPPED_LIVE_OR_CHANGED"
        cur.execute(
            """
            INSERT INTO jaytec_job_events(job_id,event_type,source,payload)
            VALUES (%s,'GUARDIAN_DUPLICATE_CONTAINED','GUARDIAN_LITE',%s::jsonb)
            """,
            (finding.job_id, _json({"finding_id": finding.finding_id, **dict(finding.evidence or {})})),
        )
        return "CONTAINED"

    def bounded_repair(self, findings: Sequence[GuardianFinding]) -> Dict[str, str]:
        results: Dict[str, str] = {}
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (GUARDIAN_LOCK_KEY,))
                for finding in findings:
                    if not repair_allowed(finding):
                        results[finding.finding_id] = "ESCALATED_NO_AUTOREPAIR"
                        continue
                    if finding.finding_type in {"EXPIRED_JOB_LEASE", "STALE_EXECUTION_ROOM"}:
                        result = self._contain_expired_job(cur, finding)
                    elif finding.finding_type == "DUPLICATE_ASSIGNMENT":
                        result = self._block_safe_duplicate(cur, finding)
                    else:
                        result = "ESCALATED_NO_AUTOREPAIR"
                    results[finding.finding_id] = result
                    status = "CONTAINED" if result == "CONTAINED" else "ESCALATED"
                    cur.execute(
                        """
                        UPDATE jaytec_guardian_findings
                        SET status=%s, updated_at=now()
                        WHERE finding_id=%s
                        """,
                        (status, finding.finding_id),
                    )
        return results

    def run(self, *, auto_repair: bool = True) -> Dict[str, Any]:
        findings = self.audit()
        self.persist(findings)
        repairs = self.bounded_repair(findings) if auto_repair else {}
        return {
            "status": "HEALTHY" if not findings else "FINDINGS_PRESENT",
            "finding_count": len(findings),
            "findings": [finding.as_dict() for finding in findings],
            "repairs": repairs,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
