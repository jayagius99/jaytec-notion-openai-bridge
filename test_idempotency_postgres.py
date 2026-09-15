import os
import unittest
from datetime import datetime, timedelta, timezone

from orchestration import PacketValidationError


class TestPostgresRegistryPresence(unittest.TestCase):
    def test_module_importable(self):
        # psycopg2 is an optional dependency for staging-only durability.
        import idempotency_postgres  # noqa: F401


class TestPostgresRegistrySemantics(unittest.TestCase):
    def setUp(self):
        self.db = os.environ.get("TEST_DATABASE_URL", "").strip()
        if not self.db:
            self.skipTest("TEST_DATABASE_URL not set")
        from idempotency_postgres import PostgresExecutionRegistry

        self.reg = PostgresExecutionRegistry(database_url=self.db, ttl_seconds=10)
        self.reg.ensure_schema()

    def test_store_and_lookup(self):
        now = datetime.now(timezone.utc)
        self.reg.store("k1", "h1", {"ok": True}, now=now)
        out = self.reg.lookup("k1", "h1", now=now)
        self.assertEqual({"ok": True}, out)

    def test_conflicting_duplicate(self):
        now = datetime.now(timezone.utc)
        self.reg.store("k2", "h2", {"ok": True}, now=now)
        with self.assertRaises(ValueError):
            self.reg.lookup("k2", "DIFFERENT", now=now)

    def test_ttl_expiry(self):
        now = datetime.now(timezone.utc)
        self.reg.store("k3", "h3", {"ok": True}, now=now)
        out = self.reg.lookup("k3", "h3", now=now + timedelta(seconds=11))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
