from __future__ import annotations

from typing import Any, Dict

import psycopg2.extras

from guardian import GuardianFinding, GuardianLite, _stable_finding_id


RUNTIME_INCIDENT_TYPES = (
    "MCP_DEPENDENCY_TIMEOUT",
    "MESSAGE_DELIVERY_TIMEOUT",
    "UI_DELIVERY_TIMEOUT",
    "SESSION_FREEZE",
    "SPECIALIST_TIMEOUT",
    "SPECIALIST_PROVIDER_FAILURE",
    "DURABLE_WORKER_EXCEPTION",
    "GUARDIAN_LOOP_EXCEPTION",
)


class ReliabilityGuardian(GuardianLite):
    """Guardian Lite plus reliability-runtime self-diagnosis.

    These findings are observation/escalation only. They deliberately do not
    expand Guardian's automatic repair authority beyond the mechanically safe
    containment rules already defined by Guardian Lite.
    """

    def audit(self) -> list[GuardianFinding]:
        findings = list(super().audit())
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT event_id,job_id,event_type,source,payload,created_at
                    FROM jaytec_job_events
                    WHERE event_type = ANY(%s)
                      AND created_at > now() - (%s * interval '1 hour')
                    ORDER BY event_id DESC
                    LIMIT 500
                    """,
                    (list(RUNTIME_INCIDENT_TYPES), self.event_window_hours),
                )
                for row in cur.fetchall():
                    event_type = str(row["event_type"])
                    job_id = str(row["job_id"]) if row.get("job_id") else None
                    if event_type in {"MCP_DEPENDENCY_TIMEOUT", "MESSAGE_DELIVERY_TIMEOUT", "UI_DELIVERY_TIMEOUT", "SESSION_FREEZE"}:
                        severity = "ERROR"
                        finding_type = "DELIVERY_OR_DEPENDENCY_TIMEOUT"
                        symptom = f"Runtime observed {event_type}; caller-visible continuity may have been interrupted."
                        containment = "Recover by durable job/idempotency identity; never reconstruct or replay side effects from chat memory alone."
                    elif event_type == "SPECIALIST_TIMEOUT":
                        severity = "WARNING"
                        finding_type = "SPECIALIST_TIMEOUT"
                        symptom = "A durable specialist execution exceeded its provider timeout budget."
                        containment = "Use bounded durable retry/backoff while the packet deadline and attempt budget remain valid."
                    elif event_type == "SPECIALIST_PROVIDER_FAILURE":
                        severity = "WARNING"
                        finding_type = "SPECIALIST_PROVIDER_FAILURE"
                        symptom = "A durable specialist execution encountered a transient provider failure/rate limit."
                        containment = "Preserve packet identity and use bounded retry/backoff; do not create a replacement assignment."
                    elif event_type == "DURABLE_WORKER_EXCEPTION":
                        severity = "ERROR"
                        finding_type = "DURABLE_WORKER_EXCEPTION"
                        symptom = "The durable specialist worker encountered an execution/runtime exception."
                        containment = "Keep the job durable; lease/fence/Guardian recovery decides whether it can be retried safely."
                    else:
                        severity = "CRITICAL"
                        finding_type = "GUARDIAN_LOOP_EXCEPTION"
                        symptom = "Guardian's background self-check loop encountered an exception."
                        containment = "Keep durable state unchanged and surface the loop failure for immediate control-plane review."
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
                            evidence=dict(row),
                            containment=containment,
                            proposed_repair="No blind automatic external retry; resolve through the durable packet/job identity and verified state.",
                        )
                    )

                # Packet/job state drift should never be silent. A crashed worker
                # may temporarily leave packet RUNNING while Guardian fences its
                # job; that is recoverable, but it must be visible until reclaimed.
                cur.execute(
                    """
                    SELECT p.job_id,p.status AS packet_status,j.status AS job_status,
                           j.lease_owner,j.lease_expires_at,j.next_attempt_at,
                           p.attempt_count,p.max_attempts,p.updated_at
                    FROM jaytec_task_packets p
                    JOIN jaytec_jobs j ON j.job_id=p.job_id
                    WHERE (p.status='RUNNING' AND j.status NOT IN ('RUNNING','PAUSED'))
                       OR (p.status='QUEUED' AND j.status IN ('SUCCEEDED','FAILED_SAFE','CANCELED'))
                    """
                )
                for row in cur.fetchall():
                    job_id = str(row["job_id"])
                    findings.append(
                        GuardianFinding(
                            finding_id=_stable_finding_id("TASK_PACKET_STATE_DRIFT", job_id=job_id),
                            severity="ERROR",
                            finding_type="TASK_PACKET_STATE_DRIFT",
                            job_id=job_id,
                            symptom=f"Durable packet state {row['packet_status']} disagrees with job state {row['job_status']}.",
                            evidence=dict(row),
                            containment="Do not infer completion or retry from one table alone; reconcile the fenced job and packet states.",
                            proposed_repair="Control-plane reconciliation required unless a later worker claim/terminal write resolves the drift.",
                        )
                    )

                cur.execute(
                    """
                    SELECT p.job_id,p.status AS packet_status,j.status AS job_status,
                           j.next_attempt_at,j.updated_at,p.attempt_count,p.max_attempts
                    FROM jaytec_task_packets p
                    JOIN jaytec_jobs j ON j.job_id=p.job_id
                    WHERE p.status IN ('QUEUED','RUNNING')
                      AND j.status IN ('QUEUED','PAUSED')
                      AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= now())
                      AND j.updated_at < now() - (%s * interval '1 minute')
                    """,
                    (self.stalled_after_minutes,),
                )
                for row in cur.fetchall():
                    job_id = str(row["job_id"])
                    findings.append(
                        GuardianFinding(
                            finding_id=_stable_finding_id("STALLED_DURABLE_PACKET", job_id=job_id),
                            severity="WARNING",
                            finding_type="STALLED_DURABLE_PACKET",
                            job_id=job_id,
                            symptom=f"Runnable durable packet {job_id} has remained unclaimed beyond the health window.",
                            evidence=dict(row),
                            containment="Verify worker-loop liveness and scheduler admission conflicts before changing job state.",
                            proposed_repair="Restart/repair the worker loop only after confirming no live lease or unresolved operation owns the job.",
                        )
                    )

        deduped: Dict[str, GuardianFinding] = {}
        for finding in findings:
            deduped[finding.finding_id] = finding
        return list(deduped.values())
