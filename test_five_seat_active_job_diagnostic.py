import unittest
from unittest import mock

import five_seat_active_job_diagnostic as diagnostic


class _Cursor:
    def __init__(self):
        self._mode = ""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params=None):
        text = " ".join(str(sql).split()).lower()
        if text.startswith("set transaction read only"):
            self._mode = "readonly"
        elif "current_database()" in text:
            self._mode = "database"
        elif "from jaytec_jobs" in text:
            self._mode = "jobs"
    def fetchone(self):
        if self._mode == "database":
            return {"db": "jaytec_orchestration_prod"}
        return {}
    def fetchall(self):
        if self._mode != "jobs":
            return []
        return [{
            "job_id": "job-1",
            "task_id": "task-1",
            "subtask_id": None,
            "assignment_type": "JAYTEC_CALLABLE",
            "status": "RUNNING",
            "health": "HEALTHY",
            "lease_owner": None,
            "lease_expires_at": None,
            "ownership_epoch": 2,
            "fence_token": 15,
            "checkpoint_ref": "cp-1",
            "source_shared_state_version": 54,
            "created_at": None,
            "updated_at": None,
        }]


class _Connection:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def cursor(self, **kwargs):
        return _Cursor()


class ActiveJobDiagnosticTests(unittest.TestCase):
    def test_disabled_main_never_connects(self):
        with mock.patch.dict(diagnostic.os.environ, {}, clear=True):
            with mock.patch.object(
                diagnostic.psycopg2, "connect"
            ) as connect:
                diagnostic.main()
                connect.assert_not_called()

    def test_wrong_identity_refuses_before_connect(self):
        with mock.patch.object(
            diagnostic, "assert_url_identity",
            side_effect=diagnostic.ProductionMigrationRefused(
                "DATABASE_HOST_FINGERPRINT_MISMATCH"
            ),
        ):
            with mock.patch.object(
                diagnostic.psycopg2, "connect"
            ) as connect:
                with self.assertRaises(
                    diagnostic.ProductionMigrationRefused
                ):
                    diagnostic.active_job_snapshot(
                        "postgresql://u:p@example/db", "0" * 64
                    )
                connect.assert_not_called()

    def test_snapshot_is_read_only_and_redacted(self):
        with mock.patch.object(
            diagnostic,
            "assert_url_identity",
            return_value={"host_sha256": "a" * 64},
        ):
            with mock.patch.object(
                diagnostic.psycopg2,
                "connect",
                return_value=_Connection(),
            ):
                result = diagnostic.active_job_snapshot(
                    "postgresql://hidden", "a" * 64
                )
        self.assertTrue(result["read_only"])
        self.assertEqual(result["active_job_count"], 1)
        self.assertEqual(result["jobs"][0]["job_id"], "job-1")
        self.assertNotIn("objective", result["jobs"][0])
        self.assertNotIn("payload", result["jobs"][0])
        self.assertNotIn("database_url", result)


if __name__ == "__main__":
    unittest.main()
