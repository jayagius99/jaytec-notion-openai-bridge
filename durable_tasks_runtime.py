from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, Mapping, Optional

import psycopg2.extras

from durable_tasks import (
    ASSIGNMENT_TYPE,
    SUCCESS_OVERALL_STATUSES,
    DurableTaskConflict,
    DurableTaskFenceError,
    DurableTaskQueue,
    DurableTaskWorker,
    retry_delay_seconds,
)
from orchestration import PacketValidationError, packet_hash, parse_packet_json, validate_packet
from durable_tasks import contains_secret_material
from reliability_registry import transient_specialist_statuses


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class ReliableDurableTaskQueue(DurableTaskQueue):
    """Production-hardened queue additions over the tested durable task base.

    Adds an advisory lock around the absent-row idempotency case, requires a
    real Shared House source version, and provides a read-only production
    schema gate. The job row remains the scheduler/lease/fence authority.
    """

    def verify_schema_ready(self) -> Dict[str, bool]:
        """Fail closed unless the separately approved reliability schema exists.

        Runtime startup must never apply migration DDL. Production schema is
        changed only through the tracked Neon migration/approval path.
        """
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                      EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name='jaytec_jobs'
                          AND column_name='next_attempt_at'
                      ) AS next_attempt_at,
                      to_regclass(current_schema() || '.jaytec_task_packets') IS NOT NULL
                        AS task_packets,
                      to_regclass(current_schema() || '.jaytec_jobs_ready_idx') IS NOT NULL
                        AS jobs_ready_idx,
                      to_regclass(current_schema() || '.jaytec_task_packets_status_idx') IS NOT NULL
                        AS task_packets_status_idx
                    """
                )
                row = dict(cur.fetchone() or {})
        checks = {
            "next_attempt_at": bool(row.get("next_attempt_at")),
            "task_packets": bool(row.get("task_packets")),
            "jobs_ready_idx": bool(row.get("jobs_ready_idx")),
            "task_packets_status_idx": bool(row.get("task_packets_status_idx")),
        }
        missing = sorted(name for name, ready in checks.items() if not ready)
        if missing:
            raise RuntimeError("reliability_runtime_schema_not_ready:" + ",".join(missing))
        return checks

    def submit(
        self,
        packet_json: str,
        *,
        source_shared_state_version: int,
        priority: int = 100,
        source_execution_room_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        packet, parse_errors = parse_packet_json(packet_json)
        if packet is None:
            raise PacketValidationError(";".join(parse_errors))
        validation = validate_packet(packet)
        if not validation.ok:
            raise PacketValidationError(";".join(validation.errors))
        if contains_secret_material(packet):
            raise PacketValidationError("packet_contains_secret_material")
        if source_shared_state_version <= 0:
            raise ValueError("source_shared_state_version must be > 0")
        if priority < 0 or priority > 1_000_000:
            raise ValueError("priority out of range")

        digest = packet_hash(packet)
        idem = str(packet["idempotency_key"])
        job_id = "packet-" + hashlib.sha256(f"{idem}:{digest}".encode("utf-8")).hexdigest()[:24]
        objective = str(packet.get("intent") or packet.get("request") or "specialist packet")[:4000]
        max_attempts = min(5, max(1, int(packet.get("max_retries", 0)) + 1))
        specialist_plan = [str(item) for item in packet.get("specialist_plan", [])]

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Lock even when the idempotency row does not exist yet. This
                # makes acknowledgement loss / simultaneous submit races resolve
                # to one durable job rather than a unique-constraint exception.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"jaytec-task-packet-submit:{idem}",),
                )
                cur.execute(
                    "SELECT * FROM jaytec_task_packets WHERE idempotency_key=%s FOR UPDATE",
                    (idem,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    if existing["packet_hash"] != digest:
                        raise DurableTaskConflict("CONFLICTING_DUPLICATE")
                    return self._snapshot_with_cursor(cur, str(existing["job_id"]))

                cur.execute(
                    """
                    INSERT INTO jaytec_jobs(
                      job_id,project_id,task_id,subtask_id,assignment_type,objective,
                      status,priority,source_shared_state_version,concurrency_class,
                      mutation_scope,read_scope,dependencies,resource_scope,health
                    ) VALUES (%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s,'A',
                              '[]'::jsonb,%s::jsonb,'[]'::jsonb,%s::jsonb,'HEALTHY')
                    """,
                    (
                        job_id,
                        str(packet.get("parent_task_id") or packet["task_id"]),
                        str(packet["task_id"]),
                        str(packet["subtask_id"]),
                        ASSIGNMENT_TYPE,
                        objective,
                        priority,
                        int(source_shared_state_version),
                        _json([f"specialist:{name}" for name in specialist_plan]),
                        _json({"specialists": specialist_plan}),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_task_packets(
                      job_id,packet_hash,idempotency_key,packet,status,max_attempts
                    ) VALUES (%s,%s,%s,%s::jsonb,'QUEUED',%s)
                    """,
                    (job_id, digest, idem, _json(packet), max_attempts),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(job_id,event_type,source,source_version,payload)
                    VALUES (%s,'TASK_PACKET_SUBMITTED','DURABLE_TASK_QUEUE',%s,%s::jsonb)
                    """,
                    (
                        job_id,
                        int(source_shared_state_version),
                        _json({
                            "source_execution_room_id": source_execution_room_id,
                            "packet_hash": digest,
                            "specialist_plan": specialist_plan,
                            "submit_lock": "transaction_advisory",
                        }),
                    ),
                )
                return self._snapshot_with_cursor(cur, job_id)

    def heartbeat(self, token, *, lease_seconds: int) -> bool:
        if lease_seconds < 10 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET lease_expires_at=now() + (%s * interval '1 second'),
                        version=version+1,updated_at=now()
                    WHERE job_id=%s AND status='RUNNING' AND lease_owner=%s
                      AND ownership_epoch=%s AND fence_token=%s
                      AND lease_expires_at > now()
                    """,
                    (
                        lease_seconds,
                        token.job_id,
                        token.owner,
                        token.ownership_epoch,
                        token.fence_token,
                    ),
                )
                return int(cur.rowcount or 0) == 1


class ReliableDurableTaskWorker(DurableTaskWorker):
    """Durable worker with lease keepalive around blocking provider calls."""

    def _execute_with_heartbeat(self, token, packet: Any) -> Mapping[str, Any]:
        stop = threading.Event()
        lost_lease = threading.Event()
        interval = max(2.0, min(30.0, float(self.lease_seconds) / 3.0))

        def keepalive() -> None:
            while not stop.wait(interval):
                try:
                    ok = self.queue.heartbeat(token, lease_seconds=self.lease_seconds)
                except Exception:
                    ok = False
                if not ok:
                    lost_lease.set()
                    return

        thread = threading.Thread(
            target=keepalive,
            name=f"jaytec-lease-heartbeat-{token.job_id}",
            daemon=True,
        )
        thread.start()
        try:
            raw = self.execute_packet(_json(packet))
            result = dict(raw)
        finally:
            stop.set()
            thread.join(timeout=1.0)
        if lost_lease.is_set():
            raise DurableTaskFenceError(f"lease_lost_during_execution:{token.job_id}")
        return result

    def run_once(self) -> bool:
        claimed = self.queue.claim_next(
            owner=self.owner,
            execution_room_id=self.execution_room_id,
            lease_seconds=self.lease_seconds,
        )
        if claimed is None:
            return False
        token = self.queue.token_from_claim(claimed)
        attempt_count = int(claimed.get("attempt_count") or 1)
        max_attempts = int(claimed.get("max_attempts") or 1)
        packet = claimed.get("packet")

        # A crashed/fenced worker may leave a packet RUNNING while Guardian
        # safely returns its job to PAUSED. Reclaiming must never execute a
        # provider call beyond the packet's durable attempt budget.
        if attempt_count > max_attempts:
            self.queue.finish(
                token,
                result={
                    "overall_status": "FAILED_CLOSED",
                    "unresolved_items": ["durable_retry_budget_exhausted_before_dispatch"],
                },
                succeeded=False,
            )
            return True

        try:
            result = self._execute_with_heartbeat(token, packet)
        except DurableTaskFenceError:
            # A new owner/fence has already superseded this worker. Never write
            # or retry from the stale execution room; Guardian/new owner handles it.
            return True
        except Exception as exc:
            error = {"error_class": type(exc).__name__, "message": str(exc)[:1000]}
            if attempt_count < max_attempts:
                self.queue.requeue(
                    token,
                    error=error,
                    delay_seconds=retry_delay_seconds(attempt_count),
                )
            else:
                self.queue.finish(
                    token,
                    result={
                        "overall_status": "FAILED_CLOSED",
                        "unresolved_items": [f"durable_worker_exception:{type(exc).__name__}"],
                    },
                    succeeded=False,
                )
            self.queue.record_incident(
                "DURABLE_WORKER_EXCEPTION",
                job_id=token.job_id,
                detail=error,
            )
            return True

        overall = str(result.get("overall_status") or "FAILED_CLOSED")
        transient_statuses = transient_specialist_statuses(result)
        if transient_statuses and attempt_count < max_attempts:
            self.queue.requeue(
                token,
                error={
                    "overall_status": overall,
                    "transient_statuses": sorted(transient_statuses),
                    "result": result,
                },
                delay_seconds=retry_delay_seconds(attempt_count),
            )
            incident = (
                "SPECIALIST_TIMEOUT"
                if "TIMEOUT" in transient_statuses
                else "SPECIALIST_PROVIDER_FAILURE"
            )
            self.queue.record_incident(
                incident,
                job_id=token.job_id,
                detail={
                    "overall_status": overall,
                    "transient_statuses": sorted(transient_statuses),
                    "attempt_count": attempt_count,
                },
            )
            return True

        if transient_statuses:
            terminal = dict(result)
            terminal["overall_status"] = "FAILED_CLOSED"
            unresolved = list(terminal.get("unresolved_items") or [])
            unresolved.append("transient_specialist_retry_budget_exhausted")
            terminal["unresolved_items"] = unresolved
            self.queue.finish(token, result=terminal, succeeded=False)
            return True

        self.queue.finish(
            token,
            result=result,
            succeeded=overall in SUCCESS_OVERALL_STATUSES,
        )
        return True
