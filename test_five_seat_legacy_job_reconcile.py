import os
import unittest
from unittest import mock

import five_seat_legacy_job_reconcile as reconcile


class LegacyReconcileContractTests(unittest.TestCase):
    def _rows(self):
        return [
            {
                "job_id": job_id,
                "task_id": spec["task_id"],
                "assignment_type": spec["assignment_type"],
                "status": "RUNNING",
                "health": "HEALTHY",
                "checkpoint_ref": spec["checkpoint_ref"],
                "source_shared_state_version": spec["source_shared_state_version"],
                "ownership_epoch": spec["ownership_epoch"],
                "fence_token": spec["fence_token"],
                "lease_owner": None,
                "lease_expires_at": None,
            }
            for job_id, spec in reconcile.EXPECTED_JOBS.items()
        ]

    def test_exact_legacy_rows_validate(self):
        reconcile._validate_exact_active_set(self._rows())

    def test_unexpected_third_active_row_refuses(self):
        rows = self._rows()
        rows.append(
            {
                "job_id": "unexpected-running-job",
                "task_id": "unexpected",
                "assignment_type": "TEST",
                "status": "RUNNING",
                "checkpoint_ref": None,
                "source_shared_state_version": 55,
                "ownership_epoch": 1,
                "fence_token": 1,
                "lease_owner": None,
                "lease_expires_at": None,
            }
        )
        with self.assertRaisesRegex(
            reconcile.LegacyJobReconciliationRefused,
            "ACTIVE_JOB_SET_MISMATCH",
        ):
            reconcile._validate_exact_active_set(rows)

    def test_changed_lineage_refuses(self):
        rows = self._rows()
        rows[0]["fence_token"] = 99
        with self.assertRaisesRegex(
            reconcile.LegacyJobReconciliationRefused,
            "LEGACY_JOB_FIELD_MISMATCH",
        ):
            reconcile._validate_exact_active_set(rows)

    def test_live_lease_refuses(self):
        rows = self._rows()
        rows[0]["lease_owner"] = "some-live-worker"
        with self.assertRaisesRegex(
            reconcile.LegacyJobReconciliationRefused,
            "LEGACY_JOB_FIELD_MISMATCH",
        ):
            reconcile._validate_exact_active_set(rows)

    def test_disabled_main_never_connects(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                reconcile.psycopg2, "connect"
            ) as connect:
                reconcile.main()
                connect.assert_not_called()

    def test_wrong_identity_refuses_before_connect(self):
        with mock.patch.object(
            reconcile,
            "assert_url_identity",
            side_effect=reconcile.ProductionMigrationRefused(
                "DATABASE_HOST_FINGERPRINT_MISMATCH"
            ),
        ):
            with mock.patch.object(
                reconcile.psycopg2, "connect"
            ) as connect:
                with self.assertRaises(
                    reconcile.ProductionMigrationRefused
                ):
                    reconcile.reconcile_legacy_jobs(
                        "postgresql://hidden", "0" * 64
                    )
                connect.assert_not_called()

    def test_freeze_uses_blocked_not_paused(self):
        source = open(
            reconcile.__file__, "r", encoding="utf-8"
        ).read()
        self.assertIn("SET status='BLOCKED'", source)
        self.assertNotIn("SET status='PAUSED'", source)
        self.assertIn("OWNER_FREEZE_PARKED", source)
        self.assertIn("ACTIVE_JOBS_REMAIN_AFTER_PARK", source)


if __name__ == "__main__":
    unittest.main()
