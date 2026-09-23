import os
import time
import unittest
import uuid
from unittest.mock import patch

import psycopg2


os.environ.setdefault("FIVE_SEAT_CANARY_MODE", "1")
os.environ.setdefault("FIVE_SEAT_CANARY_WORKERS", "2")
os.environ.setdefault("FIVE_SEAT_CANARY_RUN_ID", "unit-import")

from five_seat_canary_server import (
    CanaryAdapter,
    CanaryRuntime,
    _apply_canary_migration,
    _canary_worker_kind,
)


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required")
class TestFiveSeatCanaryRuntime(unittest.TestCase):
    def tearDown(self):
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id FROM jaytec_jobs WHERE project_id='FIVE_SEAT_CANARY'"
                )
                job_ids = [row[0] for row in cur.fetchall()]
                if job_ids:
                    cur.execute(
                        """UPDATE jaytec_worker_seats
                           SET state='FREE',current_job_id=NULL,worker_id=NULL,
                               lease_owner=NULL,lease_expires_at=NULL
                           WHERE current_job_id = ANY(%s)""",
                        (job_ids,),
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
                        cur.execute(
                            f"DELETE FROM {table} WHERE job_id = ANY(%s)",
                            (job_ids,),
                        )
                    cur.execute(
                        "DELETE FROM jaytec_jobs WHERE job_id = ANY(%s)",
                        (job_ids,),
                    )
                cur.execute(
                    """UPDATE jaytec_watch_leader
                       SET lease_owner=NULL,lease_expires_at=NULL,
                           version=version+1,updated_at=now()
                       WHERE controller_id='WATCH'"""
                )

    def test_runtime_migration_flag_fails_before_any_database_connect(self):
        previous = os.environ.get("FIVE_SEAT_CANARY_APPLY_MIGRATION")
        os.environ["FIVE_SEAT_CANARY_APPLY_MIGRATION"] = "1"
        try:
            with patch("five_seat_canary_server.psycopg2.connect") as connect:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "FS08_CANARY_RUNTIME_MIGRATION_FORBIDDEN",
                ):
                    _apply_canary_migration(
                        "postgresql://wrong-target.invalid/canary"
                    )
                connect.assert_not_called()
        finally:
            if previous is None:
                os.environ.pop("FIVE_SEAT_CANARY_APPLY_MIGRATION", None)
            else:
                os.environ["FIVE_SEAT_CANARY_APPLY_MIGRATION"] = previous

    def test_run_specific_worker_kind_prevents_stage_inheritance(self):
        one = _canary_worker_kind("run-one", 2)
        two = _canary_worker_kind("run-two", 2)
        three = _canary_worker_kind("run-one", 3)
        self.assertNotEqual(one, two)
        self.assertNotEqual(one, three)
        self.assertTrue(one.startswith("CANARY_2_"))

    def test_barrier_requires_exact_stage_width(self):
        adapter = CanaryAdapter(2, "barrier-test")
        results = []
        errors = []

        import threading

        def run(index):
            try:
                results.append(
                    adapter.execute(
                        {
                            "run_id": "barrier-test",
                            "stage": 2,
                            "index": index,
                        }
                    )
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertTrue(
            all(
                result["tests"][0]["name"] == "simultaneous-seat-barrier"
                and result["tests"][0]["result"] == "PASS"
                for result in results
            )
        )

    def test_two_worker_runtime_completes_through_watch(self):
        run_id = "ci-canary-" + uuid.uuid4().hex[:10]
        os.environ["FIVE_SEAT_CANARY_WORKERS"] = "2"
        os.environ["FIVE_SEAT_CANARY_RUN_ID"] = run_id
        runtime = CanaryRuntime()
        runtime.start()
        try:
            deadline = time.time() + 30
            status = runtime.status()
            while time.time() < deadline and not status["pass"]:
                time.sleep(0.25)
                status = runtime.status()
            self.assertTrue(status["pass"], status)
            self.assertEqual(status["stage_workers"], 2)
            self.assertEqual(status["succeeded"], 2)
            self.assertEqual(status["accepted_reviews"], 2)
            self.assertEqual(len(status["distinct_claimed_seats"]), 2)
            self.assertEqual(status["blocked_jobs"], [])
            self.assertEqual(status["errors"], [])
        finally:
            runtime.stop_event.set()


if __name__ == "__main__":
    unittest.main()
