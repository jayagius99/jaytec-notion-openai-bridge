import unittest
from unittest import mock

import five_seat_orphaned_job_reconcile as reconcile


def _row(job_id, task_id, assignment_type):
    return {
        "job_id": job_id,
        "task_id": task_id,
        "assignment_type": assignment_type,
        "status": "RUNNING",
        "health": "HEALTHY",
        "lease_owner": None,
        "lease_expires_at": None,
        "ownership_epoch": 1,
        "fence_token": 1,
        "source_shared_state_version": 55,
        "checkpoint_ref": None,
    }


ROWS = [
    _row(
        "watch-FORGE-GENESIS-ACTIVATION-001",
        "FORGE-GENESIS-ACTIVATION-001",
        "OWNER_CHAT_WATCH",
    ),
    _row(
        "watch-e1a11f3e7e2e3eea408a971a",
        "FORGE-COGNITION-PERFORMANCE-REVIEW-001",
        "ARCHITECTURE_REVIEW",
    ),
]


class _Cursor:
    def __init__(self, unresolved=False):
        self.sql = ""
        self.unresolved = unresolved
        self.updates = 0
        self.events = 0
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params=None):
        self.sql = " ".join(str(sql).split())
        upper = self.sql.upper()
        if upper.startswith("UPDATE JAYTEC_JOBS"):
            self.updates += 1
        if "INSERT INTO JAYTEC_JOB_EVENTS" in upper:
            self.events += 1
    def fetchall(self):
        if "FROM jaytec_jobs" in self.sql:
            return ROWS
        if "FROM jaytec_operations" in self.sql:
            return (
                [{"job_id": ROWS[0]["job_id"], "operation_id": "op-1", "status": "IN_FLIGHT"}]
                if self.unresolved
                else []
            )
        return []
    def fetchone(self):
        if "current_database()" in self.sql:
            return {"db": "jaytec_orchestration_prod"}
        if self.sql.upper().startswith("UPDATE JAYTEC_JOBS"):
            row = ROWS[self.updates - 1]
            return {
                "job_id": row["job_id"],
                "task_id": row["task_id"],
                "status": "PAUSED",
                "health": "DEGRADED",
                "ownership_epoch": 2,
                "fence_token": 2,
                "checkpoint_ref": row["checkpoint_ref"],
            }
        return {}


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def cursor(self, **kwargs):
        return self._cursor


class ReconcileTests(unittest.TestCase):
    def test_disabled_main_never_connects(self):
        with mock.patch.dict(reconcile.os.environ, {}, clear=True):
            with mock.patch.object(reconcile.psycopg2, "connect") as connect:
                reconcile.main()
                connect.assert_not_called()

    def test_metadata_drift_fails_closed(self):
        row = dict(ROWS[0])
        row["fence_token"] = 99
        with self.assertRaises(reconcile.ProductionMigrationRefused):
            reconcile._assert_expected(row)

    def test_unresolved_operation_aborts_before_update(self):
        cursor = _Cursor(unresolved=True)
        with mock.patch.object(
            reconcile,
            "assert_url_identity",
            return_value={"host_sha256": "a" * 64},
        ), mock.patch.object(
            reconcile.psycopg2,
            "connect",
            return_value=_Connection(cursor),
        ):
            with self.assertRaises(reconcile.ProductionMigrationRefused):
                reconcile.reconcile("postgresql://hidden", "a" * 64)
        self.assertEqual(cursor.updates, 0)
        self.assertEqual(cursor.events, 0)

    def test_exact_orphans_are_parked_and_fenced(self):
        cursor = _Cursor(unresolved=False)
        with mock.patch.object(
            reconcile,
            "assert_url_identity",
            return_value={"host_sha256": "a" * 64},
        ), mock.patch.object(
            reconcile.psycopg2,
            "connect",
            return_value=_Connection(cursor),
        ):
            result = reconcile.reconcile(
                "postgresql://hidden",
                "a" * 64,
            )
        self.assertEqual(result["status"], "PARKED_SAFE")
        self.assertEqual(len(result["parked"]), 2)
        self.assertEqual(cursor.updates, 2)
        self.assertEqual(cursor.events, 2)


if __name__ == "__main__":
    unittest.main()
