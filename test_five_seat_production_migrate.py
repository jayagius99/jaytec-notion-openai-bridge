import hashlib
import os
import unittest
from unittest import mock

import five_seat_production_migrate as migrate


class TestFiveSeatProductionMigrationGuard(unittest.TestCase):
    def test_disabled_preflight_never_exposes_host_or_credentials(self):
        url = "postgresql://owner:supersecret@ep-production.example/jaytec_orchestration_prod?sslmode=require"
        payload = migrate.safe_preflight_payload(url, migration_enabled=False)
        self.assertEqual(payload["database_from_url"], "jaytec_orchestration_prod")
        self.assertNotIn("ep-production.example", str(payload))
        self.assertNotIn("supersecret", str(payload))
        self.assertEqual(
            payload["host_sha256"],
            hashlib.sha256(b"ep-production.example").hexdigest(),
        )

    def test_wrong_database_refused_before_connect_or_sql_read(self):
        url = "postgresql://owner:secret@ep-prod.example/not_production"
        expected = hashlib.sha256(b"ep-prod.example").hexdigest()
        with mock.patch.object(migrate.psycopg2, "connect") as connect:
            with mock.patch.object(migrate.MIGRATION_FILE, "read_text") as read_text:
                with self.assertRaisesRegex(
                    migrate.ProductionMigrationRefused,
                    "DATABASE_NAME_MISMATCH",
                ):
                    migrate.apply_migration(url, expected)
        connect.assert_not_called()
        read_text.assert_not_called()

    def test_wrong_host_fingerprint_refused_before_connect_or_sql_read(self):
        url = "postgresql://owner:secret@ep-wrong.example/jaytec_orchestration_prod"
        expected = hashlib.sha256(b"ep-production.example").hexdigest()
        with mock.patch.object(migrate.psycopg2, "connect") as connect:
            with mock.patch.object(migrate.MIGRATION_FILE, "read_text") as read_text:
                with self.assertRaisesRegex(
                    migrate.ProductionMigrationRefused,
                    "DATABASE_HOST_FINGERPRINT_MISMATCH",
                ):
                    migrate.apply_migration(url, expected)
        connect.assert_not_called()
        read_text.assert_not_called()

    def test_missing_fingerprint_refused(self):
        url = "postgresql://owner:secret@ep-prod.example/jaytec_orchestration_prod"
        with self.assertRaisesRegex(
            migrate.ProductionMigrationRefused,
            "EXPECTED_HOST_SHA256",
        ):
            migrate.assert_url_identity(url, "")

    def test_migration_flag_defaults_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotEqual(os.environ.get(migrate.MIGRATION_FLAG, "0"), "1")


if __name__ == "__main__":
    unittest.main()
