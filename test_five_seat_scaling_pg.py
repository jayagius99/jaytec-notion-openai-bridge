import os
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras

from five_seat_runtime import PostgresFiveSeatScheduler


def _uid(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required")
class TestFiveSeatScalingPostgres(unittest.TestCase):
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
                        updated_by='FS06_SCALING_TEST',
                        updated_at=now()
                    WHERE authority_id='FABRIC'
                    """
                )

    def tearDown(self):
        if not self.jobs:
            return
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
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

    def _insert(self, *, cls="B", scope=None, priority=100):
        job_id = _uid("stress")
        self.jobs.append(job_id)
        scope = scope or [f"scope/{job_id}"]
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jaytec_jobs(
                       job_id,project_id,task_id,assignment_type,objective,status,
                       fabric_state,priority,source_shared_state_version,
                       concurrency_class,mutation_scope,read_scope,dependencies,
                       resource_scope,fabric_attempt_count,fabric_max_attempts
                       ) VALUES (
                       %s,'CI',%s,'FIVE_SEAT_FABRIC','stress','QUEUED','QUEUED',
                       %s,1,%s,%s::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,0,4)""",
                    (job_id, job_id, priority, cls, psycopg2.extras.Json(scope)),
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
            execution_room_id="stress-room",
            lease_seconds=60,
            supported_worker_kinds={"TEST"},
            capabilities={"test.run"},
        )

    def test_six_simultaneous_claimers_get_exactly_five_seats(self):
        for i in range(6):
            self._insert(priority=i)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self._claim, [f"worker-{i}" for i in range(6)]))
        claimed = [row for row in results if row is not None]
        self.assertEqual(len(claimed), 5)
        self.assertEqual(len({row["seat_id"] for row in claimed}), 5)
        self.assertEqual(
            {row["seat_id"] for row in claimed},
            {f"WORKER-SEAT-{i}" for i in range(1, 6)},
        )

    def test_overlapping_b_is_blocked_while_disjoint_b_runs(self):
        first = self._insert(scope=["repo/shared"], priority=1)
        second = self._insert(scope=["repo/shared"], priority=2)
        third = self._insert(scope=["repo/disjoint"], priority=3)
        one = self._claim("worker-a")
        self.assertEqual(one["job_id"], first)
        two = self._claim("worker-b")
        self.assertIsNotNone(two)
        self.assertEqual(two["job_id"], third)
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jaytec_jobs WHERE job_id=%s", (second,))
                self.assertEqual(cur.fetchone()[0], "QUEUED")

    def test_c_d_e_do_not_overlap_each_other(self):
        c_job = self._insert(cls="C", scope=["canonical/state"], priority=1)
        d_job = self._insert(cls="D", scope=["deploy/prod"], priority=2)
        e_job = self._insert(cls="E", scope=["global"], priority=3)
        first = self._claim("worker-c")
        self.assertEqual(first["job_id"], c_job)
        self.assertIsNone(self._claim("worker-other"))
        with psycopg2.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jaytec_jobs
                       SET status='SUCCEEDED',fabric_state='SUCCEEDED',
                           seat_id=NULL,lease_owner=NULL,lease_expires_at=NULL
                       WHERE job_id=%s""",
                    (c_job,),
                )
                cur.execute(
                    """UPDATE jaytec_worker_seats
                       SET state='FREE',current_job_id=NULL,worker_id=NULL,
                           lease_owner=NULL,lease_expires_at=NULL
                       WHERE current_job_id=%s""",
                    (c_job,),
                )
        second = self._claim("worker-d")
        self.assertEqual(second["job_id"], d_job)
        self.assertIsNone(self._claim("worker-e-blocked"))

    def test_queued_work_does_not_need_notify_to_be_claimed(self):
        job = self._insert(priority=1)
        claimed = self._claim("worker-no-notify")
        self.assertEqual(claimed["job_id"], job)


if __name__ == "__main__":
    unittest.main()
