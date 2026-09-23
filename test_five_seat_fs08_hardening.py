import os
import unittest
import uuid

import psycopg2

from five_seat_authority import (
    FabricApprovalRequired,
    PostgresFabricAuthority,
    authority_requires_approval,
    normalize_cost_policy,
)
from five_seat_queue import FabricQueueError, PostgresFabricQueue
from five_seat_remedies import PostgresFabricRemedies
from five_seat_runtime import PostgresFiveSeatScheduler
from five_seat_watch import PostgresWatchController, WatchReviewConflict


def _uid(prefix):
    return prefix + "-" + uuid.uuid4().hex[:12]


def _handoff_payload(source_version):
    return {
        "task_packet_hash": "packet",
        "starting_checkpoint": "start",
        "operations": [],
        "artifacts": [],
        "tests": [{"name": "hardening", "result": "PASS"}],
        "evidence": [{"source_shared_state_version": source_version}],
        "provider_identity": "LOCAL_TEST_NO_PROVIDER",
        "unresolved_items": [],
        "partial_side_effect_status": "UNCERTAIN_PARTIAL",
        "proposed_next_action": "WATCH_REVIEW",
        "worker_completion_classification": "CANDIDATE_COMPLETE",
    }


class TestFiveSeatAuthorityPolicy(unittest.TestCase):
    def test_zero_spend_is_strict(self):
        with self.assertRaises(ValueError):
            normalize_cost_policy(
                {
                    "mode": "ZERO_SPEND",
                    "allow_paid": True,
                    "max_cost_usd": 1,
                }
            )

    def test_high_risk_requires_approval(self):
        zero = normalize_cost_policy(None)
        self.assertFalse(authority_requires_approval("SCOPED_MUTATION", zero))
        self.assertTrue(authority_requires_approval("CANONICAL_SHARED", zero))
        self.assertTrue(authority_requires_approval("EXTERNAL_SIDE_EFFECT", zero))
        self.assertTrue(authority_requires_approval("GLOBAL_EXCLUSIVE", zero))


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required")
class TestFiveSeatFS08HardeningPostgres(unittest.TestCase):
    SOURCE_VERSION = 100

    def setUp(self):
        self.url = os.environ["DATABASE_URL"]
        self.jobs = []
        self.worker_kinds = set()
        self.authority = PostgresFabricAuthority(self.url)
        self.queue = PostgresFabricQueue(self.url)
        self.scheduler = PostgresFiveSeatScheduler(self.url)
        self.remedies = PostgresFabricRemedies(self.url)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jaytec_fabric_authority_state
                    SET current_shared_state_version=%s,
                        authority_epoch=authority_epoch+1,
                        fence_token=fence_token+1,
                        updated_by='FS08_HARDENING_TEST',
                        updated_at=now()
                    WHERE authority_id='FABRIC'
                    """,
                    (self.SOURCE_VERSION,),
                )
                cur.execute(
                    """
                    UPDATE jaytec_watch_leader
                    SET lease_owner=NULL,lease_expires_at=NULL,
                        version=version+1,updated_at=now()
                    WHERE controller_id='WATCH'
                    """
                )

    def tearDown(self):
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                if self.jobs:
                    cur.execute(
                        """
                        UPDATE jaytec_worker_seats
                        SET state='FREE',current_job_id=NULL,worker_id=NULL,
                            lease_owner=NULL,lease_expires_at=NULL
                        WHERE current_job_id = ANY(%s)
                        """,
                        (self.jobs,),
                    )
                    for table in (
                        "jaytec_watch_reviews",
                        "jaytec_fabric_approvals",
                        "jaytec_worker_handoffs",
                        "jaytec_job_events",
                        "jaytec_operations",
                        "jaytec_fabric_envelopes",
                        "jaytec_job_steps",
                        "jaytec_task_packets",
                    ):
                        cur.execute(
                            f"DELETE FROM {table} WHERE job_id = ANY(%s)",
                            (self.jobs,),
                        )
                    cur.execute(
                        "DELETE FROM jaytec_jobs WHERE job_id = ANY(%s)",
                        (self.jobs,),
                    )
                for worker_kind in self.worker_kinds:
                    cur.execute(
                        "DELETE FROM jaytec_fabric_circuits WHERE worker_kind=%s",
                        (worker_kind,),
                    )
                cur.execute(
                    """
                    UPDATE jaytec_watch_leader
                    SET lease_owner=NULL,lease_expires_at=NULL,
                        version=version+1,updated_at=now()
                    WHERE controller_id='WATCH'
                    """
                )

    def _submit(
        self,
        *,
        source_version=None,
        authority_class="SCOPED_MUTATION",
        cost_policy=None,
        worker_kind=None,
    ):
        worker_kind = worker_kind or ("TEST_" + uuid.uuid4().hex[:8].upper())
        self.worker_kinds.add(worker_kind)
        job = self.queue.submit(
            task_id=_uid("task"),
            objective="FS08 hardening proof",
            worker_kind=worker_kind,
            idempotency_key=_uid("idem"),
            source_shared_state_version=(
                self.SOURCE_VERSION if source_version is None else source_version
            ),
            authority_class=authority_class,
            concurrency_class="B",
            required_capabilities={"test.run"},
            read_scope=set(),
            mutation_scope={_uid("scope")},
            resource_scope={},
            dependencies=set(),
            collision_key=_uid("collision"),
            cost_policy=cost_policy or {"mode": "ZERO_SPEND"},
            evidence_standard={"watch_review_required": True},
            stop_conditions={"unsafe": "FAIL_CLOSED"},
            result_destination={"type": "TEST"},
            payload={},
        )
        self.jobs.append(job["job_id"])
        return job, worker_kind

    def _claim(self, worker_kind, owner=None):
        return self.scheduler.claim_next(
            owner=owner or _uid("worker"),
            execution_room_id=_uid("room"),
            lease_seconds=60,
            supported_worker_kinds={worker_kind},
            capabilities={"test.run"},
        )

    def test_stale_submit_and_queued_version_advance_fail_closed(self):
        with self.assertRaises(FabricQueueError):
            self._submit(source_version=self.SOURCE_VERSION - 1)

        job, kind = self._submit()
        self.authority.set_current_shared_state_version(
            self.SOURCE_VERSION + 1,
            updated_by="FS08_HARDENING_TEST_ADVANCE",
        )
        self.assertIsNone(self._claim(kind))
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,fabric_state FROM jaytec_jobs WHERE job_id=%s",
                    (job["job_id"],),
                )
                self.assertEqual(cur.fetchone(), ("BLOCKED", "STALE"))

    def test_high_risk_and_paid_work_require_durable_approval(self):
        job, kind = self._submit(authority_class="CANONICAL_SHARED")
        self.assertEqual(job["fabric_state"], "BLOCKED_OWNER")
        self.assertIsNone(self._claim(kind))
        self.authority.approve_job(
            job["job_id"],
            approved_by="CHATGPT_OWNER",
            approval_ref="TEST_APPROVAL",
            max_cost_usd=0,
        )
        self.assertIsNotNone(self._claim(kind))

        paid, paid_kind = self._submit(
            cost_policy={
                "mode": "OWNER_APPROVED",
                "allow_paid": True,
                "max_cost_usd": 2,
            }
        )
        with self.assertRaises(FabricApprovalRequired):
            self.authority.approve_job(
                paid["job_id"],
                approved_by="CHATGPT_OWNER",
                approval_ref="LOW_CAP",
                max_cost_usd=1,
            )
        self.assertIsNone(self._claim(paid_kind))
        self.authority.approve_job(
            paid["job_id"],
            approved_by="CHATGPT_OWNER",
            approval_ref="PAID_CAP",
            max_cost_usd=2,
        )
        self.assertIsNotNone(self._claim(paid_kind))

    def test_cancel_requested_wins_retry_race(self):
        job, kind = self._submit()
        claim = self._claim(kind, owner="retry-worker")
        token = self.scheduler.token_from_claim(claim)
        self.remedies.request_cancel(job["job_id"], reason="owner_cancel")
        result = self.remedies.safe_retry(
            token,
            worker_kind=kind,
            error={"error_class": "RetryableAdapterError"},
            delay_seconds=5,
        )
        self.assertEqual(result["reason"], "CANCEL_REQUESTED")
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fabric_state FROM jaytec_jobs WHERE job_id=%s",
                    (job["job_id"],),
                )
                self.assertEqual(cur.fetchone()[0], "CANCEL_REQUESTED")
        self.remedies.acknowledge_cancel(token)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fabric_state,seat_id FROM jaytec_jobs WHERE job_id=%s",
                    (job["job_id"],),
                )
                self.assertEqual(cur.fetchone(), ("CANCELLED", None))

    def test_watch_cannot_accept_quarantine_with_unresolved_operation(self):
        job, kind = self._submit(authority_class="READ_ONLY")
        claim = self._claim(kind, owner="handoff-worker")
        token = self.scheduler.token_from_claim(claim)
        operation_id = _uid("op")
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_operations(
                      operation_id,job_id,operation_type,target,intended_effect,
                      source_shared_state_version,execution_room_id,
                      ownership_epoch,fence_token,idempotency_key,status
                    ) VALUES (
                      %s,%s,'TEST','target','effect',%s,%s,%s,%s,%s,
                      'UNCERTAIN_PARTIAL'
                    )
                    """,
                    (
                        operation_id,
                        job["job_id"],
                        self.SOURCE_VERSION,
                        claim["execution_room_id"],
                        claim["ownership_epoch"],
                        claim["fence_token"],
                        _uid("op-idem"),
                    ),
                )
        handoff_id = _uid("handoff")
        self.scheduler.release_for_review(
            token,
            handoff_ref=handoff_id,
            handoff_payload=_handoff_payload(self.SOURCE_VERSION),
        )
        watch = PostgresWatchController(self.url)
        leader = watch.claim_leader(owner="watch-reviewer", lease_seconds=60)
        review_id = _uid("review")
        with self.assertRaises(WatchReviewConflict):
            watch.review(
                leader,
                review_id=review_id,
                handoff_id=handoff_id,
                decision="ACCEPT",
                reason="should fail while operation unresolved",
                evidence={"quarantine_reconciled": True},
            )
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM jaytec_watch_reviews WHERE handoff_id=%s",
                    (handoff_id,),
                )
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute(
                    "SELECT fabric_state FROM jaytec_jobs WHERE job_id=%s",
                    (job["job_id"],),
                )
                self.assertEqual(cur.fetchone()[0], "QUARANTINED")
                cur.execute(
                    """
                    UPDATE jaytec_operations
                    SET status='VERIFIED_NOT_DONE',verified_at=now(),updated_at=now()
                    WHERE operation_id=%s
                    """,
                    (operation_id,),
                )
        accepted = watch.review(
            leader,
            review_id=review_id,
            handoff_id=handoff_id,
            decision="ACCEPT",
            reason="ledger now mechanically reconciled",
            evidence={"quarantine_reconciled": True, "operation_id": operation_id},
        )
        self.assertEqual(accepted["job"]["fabric_state"], "SUCCEEDED")

    def test_expired_half_open_probe_returns_to_open_cooldown(self):
        worker_kind = "PROBE_" + uuid.uuid4().hex[:8].upper()
        self.worker_kinds.add(worker_kind)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jaytec_fabric_circuits(
                      worker_kind,state,consecutive_failures,failure_threshold,
                      open_until
                    ) VALUES (%s,'OPEN',3,3,now()-interval '1 second')
                    """,
                    (worker_kind,),
                )
        job, _ = self._submit(worker_kind=worker_kind)
        claim = self._claim(worker_kind, owner="probe-worker")
        self.assertIsNotNone(claim)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state,probe_job_id
                    FROM jaytec_fabric_circuits
                    WHERE worker_kind=%s
                    """,
                    (worker_kind,),
                )
                self.assertEqual(cur.fetchone(), ("HALF_OPEN", job["job_id"]))
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET lease_expires_at=now()-interval '2 seconds'
                    WHERE job_id=%s
                    """,
                    (job["job_id"],),
                )
                cur.execute(
                    """
                    UPDATE jaytec_worker_seats
                    SET lease_expires_at=now()-interval '2 seconds'
                    WHERE current_job_id=%s
                    """,
                    (job["job_id"],),
                )
        self.remedies.reconcile_expired_leases()
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT state,probe_job_id,(open_until > now())
                    FROM jaytec_fabric_circuits
                    WHERE worker_kind=%s
                    """,
                    (worker_kind,),
                )
                self.assertEqual(cur.fetchone(), ("OPEN", None, True))


if __name__ == "__main__":
    unittest.main()
