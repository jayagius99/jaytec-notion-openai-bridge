import json
import os
import uuid
import unittest

import psycopg2
import psycopg2.extras

from five_seat_remedies import PostgresFabricRemedies
from five_seat_runtime import FiveSeatStaleLease, PostgresFiveSeatScheduler
from five_seat_watch import (
    PostgresWatchController,
    WatchLeaderUnavailable,
    WatchStaleLeader,
)


def _uid(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]


def _handoff_payload(partial="NONE"):
    return {
        "starting_checkpoint": None,
        "operations": [],
        "artifacts": [],
        "tests": [{"name": "integration", "result": "PASS"}],
        "evidence": [{"type": "integration"}],
        "provider_identity": "TEST",
        "unresolved_items": [],
        "partial_side_effect_status": partial,
        "proposed_next_action": "WATCH_REVIEW",
        "worker_completion_classification": "CANDIDATE_COMPLETE",
    }


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required")
class TestFiveSeatRecoveryPostgres(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["DATABASE_URL"]
        self.jobs = []
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jaytec_fabric_authority_state
                    SET current_shared_state_version=1,
                        authority_epoch=authority_epoch+1,
                        fence_token=fence_token+1,
                        updated_by='FS06_RECOVERY_TEST',
                        updated_at=now()
                    WHERE authority_id='FABRIC'
                    """
                )
                cur.execute(
                    """UPDATE jaytec_watch_leader
                       SET lease_owner=NULL,lease_expires_at=NULL,
                           version=version+1,updated_at=now()
                       WHERE controller_id='WATCH'"""
                )

    def tearDown(self):
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jaytec_watch_leader
                       SET lease_owner=NULL,lease_expires_at=NULL,
                           version=version+1,updated_at=now()
                       WHERE controller_id='WATCH'"""
                )
                if self.jobs:
                    cur.execute(
                        """UPDATE jaytec_worker_seats
                           SET state='FREE',current_job_id=NULL,worker_id=NULL,
                               lease_owner=NULL,lease_expires_at=NULL
                           WHERE current_job_id = ANY(%s)""",
                        (self.jobs,),
                    )
                    for table in (
                        "jaytec_watch_reviews",
                        "jaytec_worker_handoffs",
                        "jaytec_fabric_envelopes",
                        "jaytec_job_events",
                        "jaytec_operations",
                        "jaytec_job_steps",
                        "jaytec_task_packets",
                    ):
                        cur.execute(f"DELETE FROM {table} WHERE job_id = ANY(%s)", (self.jobs,))
                    cur.execute("DELETE FROM jaytec_jobs WHERE job_id = ANY(%s)", (self.jobs,))

    def _insert(self, priority=1):
        job_id = _uid("recover")
        self.jobs.append(job_id)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jaytec_jobs(
                       job_id,project_id,task_id,assignment_type,objective,status,
                       fabric_state,priority,source_shared_state_version,
                       concurrency_class,mutation_scope,read_scope,dependencies,
                       resource_scope,fabric_attempt_count,fabric_max_attempts
                       ) VALUES (
                       %s,'CI',%s,'FIVE_SEAT_FABRIC','recovery','QUEUED','QUEUED',
                       %s,1,'B',%s::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,0,4)""",
                    (job_id, job_id, priority, psycopg2.extras.Json([f"scope/{job_id}"])),
                )
                cur.execute(
                    """INSERT INTO jaytec_fabric_envelopes(
                       job_id,envelope_hash,idempotency_key,worker_kind,
                       required_capabilities,authority_class,payload
                       ) VALUES (%s,%s,%s,'TEST','["test.run"]'::jsonb,
                       'SCOPED_MUTATION','{}'::jsonb)""",
                    (job_id, "hash-"+job_id, "idem-"+job_id),
                )
        return job_id

    def _claim(self, owner):
        return PostgresFiveSeatScheduler(self.url).claim_next(
            owner=owner,
            execution_room_id="recovery-room",
            lease_seconds=60,
            supported_worker_kinds={"TEST"},
            capabilities={"test.run"},
        )

    def test_worker_loss_requeues_with_new_fence_and_rejects_old_worker(self):
        job = self._insert()
        scheduler = PostgresFiveSeatScheduler(self.url)
        first = self._claim("worker-old")
        token = scheduler.token_from_claim(first)

        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jaytec_jobs SET lease_expires_at=now()-interval '1 second' WHERE job_id=%s",
                    (job,),
                )
                cur.execute(
                    "UPDATE jaytec_worker_seats SET lease_expires_at=now()-interval '1 second' WHERE seat_id=%s",
                    (token.seat_id,),
                )

        actions = PostgresFabricRemedies(self.url).reconcile_expired_leases()
        self.assertTrue(any(row.get("job_id") == job for row in actions))

        self.assertIsNone(self._claim("worker-too-early"))
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jaytec_jobs SET next_attempt_at=now() WHERE job_id=%s",
                    (job,),
                )
        second = self._claim("worker-new")
        self.assertIsNotNone(second)
        self.assertEqual(second["job_id"], job)
        self.assertGreater(int(second["ownership_epoch"]), token.ownership_epoch)
        self.assertGreater(int(second["fence_token"]), token.job_fence_token)

        with self.assertRaises(FiveSeatStaleLease):
            scheduler.release_for_review(
                token,
                handoff_ref=_uid("stale"),
                handoff_payload=_handoff_payload(),
            )

    def test_expired_zero_retry_task_packet_fails_safe_without_requeue(self):
        job_id = _uid("zero-retry")
        self.jobs.append(job_id)
        packet_json = json.dumps({"max_retries": 0})
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jaytec_jobs(
                       job_id,project_id,task_id,assignment_type,objective,status,
                       fabric_state,priority,source_shared_state_version,
                       concurrency_class,mutation_scope,read_scope,dependencies,
                       resource_scope,fabric_attempt_count,fabric_max_attempts
                       ) VALUES (
                       %s,'CI',%s,'FIVE_SEAT_FABRIC','zero retry','QUEUED','QUEUED',
                       1,1,'A','[]'::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,0,3)""",
                    (job_id, job_id),
                )
                cur.execute(
                    """INSERT INTO jaytec_fabric_envelopes(
                       job_id,envelope_hash,idempotency_key,worker_kind,
                       required_capabilities,authority_class,payload
                       ) VALUES (%s,%s,%s,'TASK_PACKET','[]'::jsonb,
                       'READ_ONLY',%s::jsonb)""",
                    (
                        job_id,
                        "hash-" + job_id,
                        "idem-" + job_id,
                        json.dumps({"packet_json": packet_json}),
                    ),
                )

        scheduler = PostgresFiveSeatScheduler(self.url)
        claim = scheduler.claim_next(
            owner="worker-zero-retry",
            execution_room_id="zero-retry-room",
            lease_seconds=60,
            supported_worker_kinds={"TASK_PACKET"},
            capabilities=set(),
        )
        self.assertIsNotNone(claim)
        token = scheduler.token_from_claim(claim)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jaytec_jobs SET lease_expires_at=now()-interval '1 second' WHERE job_id=%s",
                    (job_id,),
                )
                cur.execute(
                    "UPDATE jaytec_worker_seats SET lease_expires_at=now()-interval '1 second' WHERE seat_id=%s",
                    (token.seat_id,),
                )

        actions = PostgresFabricRemedies(self.url).reconcile_expired_leases()
        matching = [row for row in actions if row.get("job_id") == job_id]
        self.assertEqual(
            matching[0]["action"],
            "EXPIRED_WORKER_RETRY_BUDGET_EXHAUSTED",
        )
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status,fabric_state FROM jaytec_jobs WHERE job_id=%s",
                    (job_id,),
                )
                self.assertEqual(cur.fetchone(), ("FAILED_SAFE", "FAILED_SAFE"))

    def test_handoff_is_idempotent_and_seat_refills_immediately(self):
        first_job = self._insert(priority=1)
        second_job = self._insert(priority=2)
        scheduler = PostgresFiveSeatScheduler(self.url)
        first = self._claim("worker-one")
        self.assertEqual(first["job_id"], first_job)
        token = scheduler.token_from_claim(first)
        handoff_id = _uid("handoff")
        payload = _handoff_payload()

        released = scheduler.release_for_review(
            token,
            handoff_ref=handoff_id,
            handoff_payload=payload,
        )
        self.assertFalse(released["idempotent_replay"])

        replay = scheduler.release_for_review(
            token,
            handoff_ref=handoff_id,
            handoff_payload=payload,
        )
        self.assertTrue(replay["idempotent_replay"])

        next_claim = self._claim("worker-two")
        self.assertEqual(next_claim["job_id"], second_job)

    def test_uncertain_partial_quarantines_job_but_frees_seat(self):
        job = self._insert()
        scheduler = PostgresFiveSeatScheduler(self.url)
        claim = self._claim("worker-uncertain")
        token = scheduler.token_from_claim(claim)
        result = scheduler.release_for_review(
            token,
            handoff_ref=_uid("uncertain"),
            handoff_payload=_handoff_payload("UNCERTAIN_PARTIAL"),
        )
        self.assertEqual(result["job"]["fabric_state"], "QUARANTINED")
        self.assertEqual(result["seat"]["state"], "FREE")
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT fabric_state FROM jaytec_jobs WHERE job_id=%s", (job,))
                self.assertEqual(cur.fetchone()[0], "QUARANTINED")

    def test_cancel_request_releases_seat_without_execution(self):
        job = self._insert()
        scheduler = PostgresFiveSeatScheduler(self.url)
        claim = self._claim("worker-cancel")
        token = scheduler.token_from_claim(claim)
        remedies = PostgresFabricRemedies(self.url)
        remedies.request_cancel(job, reason="integration-test")
        result = remedies.acknowledge_cancel(token)
        self.assertEqual(result["fabric_state"], "CANCELLED")
        seats = scheduler.list_seats()
        self.assertFalse(any(row["current_job_id"] == job for row in seats))

    def test_watch_singleton_failover_and_stale_leader_rejection(self):
        watch = PostgresWatchController(self.url)
        first = watch.claim_leader(owner="watch-one", lease_seconds=60)
        with self.assertRaises(WatchLeaderUnavailable):
            watch.claim_leader(owner="watch-two", lease_seconds=60)

        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jaytec_watch_leader
                       SET lease_expires_at=now()-interval '1 second'
                       WHERE controller_id='WATCH'"""
                )

        second = watch.claim_leader(owner="watch-two", lease_seconds=60)
        self.assertGreater(second.leader_epoch, first.leader_epoch)
        self.assertGreater(second.fence_token, first.fence_token)
        with self.assertRaises(WatchStaleLeader):
            watch.heartbeat(first, lease_seconds=60)

    def test_watch_accept_emits_durable_owner_completion_event(self):
        job = self._insert()
        scheduler = PostgresFiveSeatScheduler(self.url)
        claim = self._claim("worker-owner-event")
        token = scheduler.token_from_claim(claim)
        handoff_id = _uid("owner-event")
        scheduler.release_for_review(
            token,
            handoff_ref=handoff_id,
            handoff_payload=_handoff_payload(),
        )

        watch = PostgresWatchController(self.url)
        leader = watch.claim_leader(owner="watch-owner-event", lease_seconds=60)
        watch.review(
            leader,
            review_id=_uid("owner-event-review"),
            handoff_id=handoff_id,
            decision="ACCEPT",
            reason="whole assignment accepted",
            evidence={"independent": True},
        )

        with psycopg2.connect(self.url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT event_type,payload
                    FROM jaytec_job_events
                    WHERE job_id=%s
                      AND event_type='OWNER_NOTIFICATION_REQUIRED'
                    ORDER BY event_id DESC
                    LIMIT 1
                    """,
                    (job,),
                )
                event = cur.fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["event_type"], "OWNER_NOTIFICATION_REQUIRED")
        self.assertEqual(event["payload"]["kind"], "WHOLE_JOB_COMPLETE")
        self.assertEqual(event["payload"]["decision"], "ACCEPT")

    def test_rework_gets_fresh_worker_ownership(self):
        job = self._insert()
        scheduler = PostgresFiveSeatScheduler(self.url)
        claim = self._claim("worker-first")
        token = scheduler.token_from_claim(claim)
        handoff_id = _uid("review")
        scheduler.release_for_review(
            token,
            handoff_ref=handoff_id,
            handoff_payload=_handoff_payload(),
        )

        watch = PostgresWatchController(self.url)
        leader = watch.claim_leader(owner="watch-reviewer", lease_seconds=60)
        reviewed = watch.review(
            leader,
            review_id=_uid("review-id"),
            handoff_id=handoff_id,
            decision="REWORK",
            reason="integration hardening",
            evidence={"checked": True},
        )
        self.assertEqual(reviewed["job"]["fabric_state"], "REWORK_QUEUED")

        second = self._claim("worker-second")
        self.assertEqual(second["job_id"], job)
        self.assertGreater(int(second["ownership_epoch"]), token.ownership_epoch)
        self.assertGreater(int(second["fence_token"]), token.job_fence_token)


if __name__ == "__main__":
    unittest.main()
