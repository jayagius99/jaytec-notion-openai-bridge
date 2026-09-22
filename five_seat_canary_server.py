from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

from five_seat_adapters import AdapterRegistry
from five_seat_guardian import FiveSeatGuardian
from five_seat_queue import PostgresFabricQueue
from five_seat_runtime import PostgresFiveSeatScheduler
from five_seat_signals import PostgresFabricSignal
from five_seat_watch import PostgresWatchController, WatchStaleLeader
from five_seat_worker import FiveSeatWorker


CANARY_PROJECT_ID = "FIVE_SEAT_CANARY"
CANARY_CAPABILITY = "test.run"
WATCH_OWNER = "fs08-canary-watch"


def _worker_count() -> int:
    value = int(os.environ.get("FIVE_SEAT_CANARY_WORKERS", "1"))
    if value < 1 or value > 5:
        raise RuntimeError("FIVE_SEAT_CANARY_WORKERS must be between 1 and 5")
    return value


def _run_id() -> str:
    value = str(os.environ.get("FIVE_SEAT_CANARY_RUN_ID", "")).strip()
    if not value:
        raise RuntimeError("FIVE_SEAT_CANARY_RUN_ID is required")
    if len(value) > 80 or not all(ch.isalnum() or ch in "-_." for ch in value):
        raise RuntimeError("invalid FIVE_SEAT_CANARY_RUN_ID")
    return value


def _canary_worker_kind(run_id: str, stage: int) -> str:
    suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10].upper()
    return f"CANARY_{stage}_{suffix}"


class CanaryAdapter:
    def __init__(self, worker_count: int, run_id: str):
        self.worker_count = worker_count
        self.run_id = run_id
        self.barrier = threading.Barrier(worker_count, timeout=20.0)

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        stage = int(payload.get("stage") or 0)
        index = int(payload.get("index") or 0)
        run_id = str(payload.get("run_id") or "")
        if stage != self.worker_count or run_id != self.run_id:
            raise RuntimeError("canary_payload_mismatch")

        started = time.time()
        self.barrier.wait()
        # Keep all seats occupied briefly after the simultaneous barrier so
        # status polling and durable event evidence can observe concurrency.
        time.sleep(1.0)
        return {
            "operations": [],
            "artifacts": [],
            "tests": [
                {
                    "name": "simultaneous-seat-barrier",
                    "result": "PASS",
                    "stage": stage,
                    "index": index,
                }
            ],
            "evidence": [
                {
                    "type": "fs08_canary",
                    "run_id": run_id,
                    "stage": stage,
                    "index": index,
                    "barrier_wait_seconds": round(time.time() - started, 3),
                }
            ],
            "provider_identity": "LOCAL_CANARY_NO_PROVIDER",
            "unresolved_items": [],
            "partial_side_effect_status": "NONE",
            "proposed_next_action": "WATCH_REVIEW",
            "worker_completion_classification": "CANDIDATE_COMPLETE",
            "canary_run_id": run_id,
            "canary_stage": stage,
            "canary_index": index,
        }


class CanaryRuntime:
    def __init__(self):
        self.database_url = os.environ["DATABASE_URL"]
        self.worker_count = _worker_count()
        self.run_id = _run_id()
        self.worker_kind = _canary_worker_kind(self.run_id, self.worker_count)
        self.scheduler = PostgresFiveSeatScheduler(self.database_url)
        self.queue = PostgresFabricQueue(self.database_url)
        self.signal = PostgresFabricSignal(self.database_url)
        self.watch = PostgresWatchController(self.database_url)
        self.guardian = FiveSeatGuardian(self.database_url)
        self.registry = AdapterRegistry()
        self.adapter = CanaryAdapter(self.worker_count, self.run_id)
        self.registry.register(
            self.worker_kind,
            capabilities={CANARY_CAPABILITY},
            execute=self.adapter.execute,
        )
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.errors: list[str] = []

    def start(self) -> None:
        self.scheduler.verify_schema_ready()

        watch_thread = threading.Thread(
            target=self._watch_loop,
            name="fs08-watch",
            daemon=True,
        )
        watch_thread.start()
        self.threads.append(watch_thread)

        guardian_thread = threading.Thread(
            target=self._guardian_loop,
            name="fs08-guardian",
            daemon=True,
        )
        guardian_thread.start()
        self.threads.append(guardian_thread)

        for index in range(1, self.worker_count + 1):
            worker = FiveSeatWorker(
                self.scheduler,
                self.registry,
                self.signal,
                owner=f"{self.run_id}-worker-{index}",
                execution_room_id=f"{self.run_id}-room-{index}",
                lease_seconds=60,
                idle_fallback_seconds=1.0,
            )
            thread = threading.Thread(
                target=worker.run_forever,
                name=f"fs08-worker-{index}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        self._submit_stage()

    def _submit_stage(self) -> None:
        for index in range(1, self.worker_count + 1):
            self.queue.submit(
                task_id=f"{self.run_id}-{index}",
                objective=f"FS08 canary stage {self.worker_count} worker {index}",
                worker_kind=self.worker_kind,
                idempotency_key=f"{self.run_id}:{index}",
                source_shared_state_version=1,
                authority_class="READ_ONLY",
                concurrency_class="A",
                priority=index,
                project_id=CANARY_PROJECT_ID,
                required_capabilities={CANARY_CAPABILITY},
                read_scope={f"canary/{self.run_id}/{index}"},
                mutation_scope=set(),
                resource_scope={},
                dependencies=set(),
                collision_key=f"canary:{self.run_id}:{index}",
                cost_policy={
                    "mode": "ZERO_SPEND",
                    "provider_calls_allowed": False,
                },
                evidence_standard={
                    "watch_review_required": True,
                    "simultaneous_barrier_required": True,
                },
                stop_conditions={
                    "external_side_effect": "FAIL_CLOSED",
                },
                result_destination={
                    "type": "CANARY_LEDGER",
                    "run_id": self.run_id,
                },
                payload={
                    "run_id": self.run_id,
                    "stage": self.worker_count,
                    "index": index,
                },
            )

    def _watch_loop(self) -> None:
        token = None
        last_heartbeat = 0.0
        while not self.stop_event.is_set():
            try:
                if token is None:
                    token = self.watch.claim_leader(
                        owner=WATCH_OWNER,
                        lease_seconds=60,
                    )
                    last_heartbeat = time.time()
                elif time.time() - last_heartbeat >= 15:
                    token = self.watch.heartbeat(token, lease_seconds=60)
                    last_heartbeat = time.time()

                for handoff in self.watch.pending_reviews(limit=100):
                    payload = handoff.get("payload") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    result = dict(payload.get("result") or {})
                    if str(result.get("canary_run_id") or "") != self.run_id:
                        continue

                    partial = str(
                        payload.get("partial_side_effect_status") or ""
                    ).upper()
                    tests = list(payload.get("tests") or [])
                    barrier_pass = any(
                        item.get("name") == "simultaneous-seat-barrier"
                        and item.get("result") == "PASS"
                        for item in tests
                        if isinstance(item, dict)
                    )
                    if partial == "NONE" and barrier_pass:
                        decision = "ACCEPT"
                        reason = "FS08 harmless canary evidence verified"
                        evidence = {
                            "run_id": self.run_id,
                            "stage": self.worker_count,
                            "partial_side_effect_status": partial,
                            "barrier_pass": True,
                        }
                    else:
                        decision = "BLOCK"
                        reason = "FS08 canary evidence failed validation"
                        evidence = {
                            "run_id": self.run_id,
                            "stage": self.worker_count,
                            "partial_side_effect_status": partial,
                            "barrier_pass": barrier_pass,
                        }

                    self.watch.review(
                        token,
                        review_id=f"fs08-review-{handoff['handoff_id']}",
                        handoff_id=str(handoff["handoff_id"]),
                        decision=decision,
                        reason=reason,
                        evidence=evidence,
                    )
            except WatchStaleLeader:
                token = None
            except Exception as exc:
                self.errors.append(f"watch:{type(exc).__name__}:{exc}")
                token = None
            self.stop_event.wait(0.25)

    def _guardian_loop(self) -> None:
        while not self.stop_event.wait(2.0):
            try:
                result = self.guardian.run_once()
                critical = [
                    item
                    for item in result.get("findings", [])
                    if item.get("severity") == "CRITICAL"
                ]
                if critical:
                    self.errors.append(
                        "guardian_critical:" + json.dumps(critical, sort_keys=True)
                    )
            except Exception as exc:
                self.errors.append(f"guardian:{type(exc).__name__}:{exc}")

    def status(self) -> dict[str, Any]:
        with psycopg2.connect(self.database_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id,task_id,status,fabric_state,seat_id,
                           ownership_epoch,fence_token,created_at,updated_at
                    FROM jaytec_jobs
                    WHERE project_id=%s AND task_id LIKE %s
                    ORDER BY task_id
                    """,
                    (CANARY_PROJECT_ID, self.run_id + "-%"),
                )
                jobs = [dict(row) for row in cur.fetchall()]

                job_ids = [str(row["job_id"]) for row in jobs]
                seat_ids: set[str] = set()
                review_count = 0
                if job_ids:
                    cur.execute(
                        """
                        SELECT payload
                        FROM jaytec_job_events
                        WHERE job_id = ANY(%s)
                          AND event_type='WORKER_SEAT_CLAIMED'
                        """,
                        (job_ids,),
                    )
                    for row in cur.fetchall():
                        payload = row["payload"] or {}
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        seat_id = str(payload.get("seat_id") or "")
                        if seat_id:
                            seat_ids.add(seat_id)

                    cur.execute(
                        """
                        SELECT count(*)::int AS n
                        FROM jaytec_watch_reviews
                        WHERE job_id = ANY(%s) AND decision='ACCEPT'
                        """,
                        (job_ids,),
                    )
                    review_count = int((cur.fetchone() or {}).get("n") or 0)

        succeeded = sum(1 for row in jobs if row.get("fabric_state") == "SUCCEEDED")
        blocked = [
            row
            for row in jobs
            if row.get("fabric_state")
            in {"QUARANTINED", "ESCALATED", "FAILED_SAFE", "BLOCKED_OWNER"}
        ]
        pass_state = (
            len(jobs) == self.worker_count
            and succeeded == self.worker_count
            and review_count == self.worker_count
            and len(seat_ids) == self.worker_count
            and not blocked
            and not self.errors
        )
        return {
            "schema_version": "JAYTEC_FS08_CANARY_STATUS_V1",
            "run_id": self.run_id,
            "stage_workers": self.worker_count,
            "worker_kind": self.worker_kind,
            "expected_jobs": self.worker_count,
            "jobs_seen": len(jobs),
            "succeeded": succeeded,
            "accepted_reviews": review_count,
            "distinct_claimed_seats": sorted(seat_ids),
            "blocked_jobs": [row["job_id"] for row in blocked],
            "errors": list(self.errors[-20:]),
            "pass": pass_state,
        }


RUNTIME = CanaryRuntime()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(
                200,
                {
                    "status": "ok",
                    "mode": "FS08_CANARY",
                    "run_id": RUNTIME.run_id,
                    "stage_workers": RUNTIME.worker_count,
                },
            )
            return
        if path == "/status":
            payload = RUNTIME.status()
            self._send(200 if payload.get("pass") else 202, payload)
            return
        self._send(404, {"error": "not_found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    if os.environ.get("FIVE_SEAT_CANARY_MODE") != "1":
        raise RuntimeError("FIVE_SEAT_CANARY_MODE=1 is required")
    RUNTIME.start()
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    finally:
        RUNTIME.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
