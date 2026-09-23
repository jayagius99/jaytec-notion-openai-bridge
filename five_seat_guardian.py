from __future__ import annotations

from typing import Any, Dict

import psycopg2
import psycopg2.extras

from five_seat_remedies import PostgresFabricRemedies


class FiveSeatGuardian:
    """Mechanical dead-worker reconciler and five-seat consistency auditor.

    It may fence expired workers and apply the narrow recovery rules in
    PostgresFabricRemedies. It cannot review/accept worker results or widen
    task authority.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.remedies = PostgresFabricRemedies(database_url)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def run_once(self) -> Dict[str, Any]:
        recovered = self.remedies.reconcile_expired_leases()
        findings = self.audit()
        return {
            "expired_lease_actions": recovered,
            "findings": findings,
        }

    def audit(self) -> list[Dict[str, Any]]:
        findings: list[Dict[str, Any]] = []
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT s.seat_id,s.state,s.current_job_id,s.worker_id,
                           s.lease_owner,s.lease_expires_at,
                           j.status AS job_status,j.fabric_state,j.seat_id AS job_seat_id,
                           j.lease_owner AS job_lease_owner,j.lease_expires_at AS job_lease_expires_at
                    FROM jaytec_worker_seats s
                    LEFT JOIN jaytec_jobs j ON j.job_id=s.current_job_id
                    WHERE
                      (s.state='FREE' AND (
                        s.current_job_id IS NOT NULL
                        OR s.worker_id IS NOT NULL
                        OR s.lease_owner IS NOT NULL
                        OR s.lease_expires_at IS NOT NULL
                      ))
                      OR
                      (s.state='RUNNING' AND (
                        s.current_job_id IS NULL
                        OR j.job_id IS NULL
                        OR j.status <> 'RUNNING'
                        OR j.seat_id <> s.seat_id
                        OR j.lease_owner IS DISTINCT FROM s.lease_owner
                      ))
                    ORDER BY s.seat_id
                    """
                )
                for row in cur.fetchall():
                    findings.append(
                        {
                            "severity": "CRITICAL",
                            "finding_type": "SEAT_JOB_OWNERSHIP_DRIFT",
                            "seat_id": row["seat_id"],
                            "job_id": row.get("current_job_id"),
                            "evidence": dict(row),
                            "automatic_action": "NONE_FAIL_CLOSED",
                        }
                    )

                cur.execute(
                    """
                    SELECT job_id,seat_id,lease_owner,lease_expires_at,
                           ownership_epoch,fence_token,fabric_attempt_count,
                           fabric_max_attempts
                    FROM jaytec_jobs
                    WHERE status='RUNNING'
                      AND (
                        seat_id IS NULL
                        OR lease_owner IS NULL
                        OR lease_expires_at IS NULL
                      )
                    ORDER BY job_id
                    """
                )
                for row in cur.fetchall():
                    findings.append(
                        {
                            "severity": "CRITICAL",
                            "finding_type": "RUNNING_JOB_WITHOUT_COMPLETE_OWNERSHIP",
                            "job_id": row["job_id"],
                            "evidence": dict(row),
                            "automatic_action": "NONE_FAIL_CLOSED",
                        }
                    )

                cur.execute(
                    """
                    SELECT job_id,collision_key,mutation_scope,read_scope,
                           resource_scope,blockers,updated_at
                    FROM jaytec_jobs
                    WHERE fabric_state='QUARANTINED'
                    ORDER BY updated_at
                    """
                )
                for row in cur.fetchall():
                    findings.append(
                        {
                            "severity": "ERROR",
                            "finding_type": "QUARANTINED_JOB",
                            "job_id": row["job_id"],
                            "evidence": dict(row),
                            "automatic_action": "BLOCK_OVERLAPPING_SCOPE_ONLY",
                        }
                    )

                cur.execute(
                    """
                    SELECT worker_kind,state,consecutive_failures,failure_threshold,
                           open_until,last_failure,updated_at
                    FROM jaytec_fabric_circuits
                    WHERE state <> 'CLOSED'
                    ORDER BY worker_kind
                    """
                )
                for row in cur.fetchall():
                    findings.append(
                        {
                            "severity": "WARNING",
                            "finding_type": "ADAPTER_CIRCUIT_NOT_CLOSED",
                            "worker_kind": row["worker_kind"],
                            "evidence": dict(row),
                            "automatic_action": "BOUNDED_HALF_OPEN_ONLY",
                        }
                    )

        return findings
