import os
import unittest
from datetime import datetime, timedelta, timezone


class TestPostgresRegistryPresence(unittest.TestCase):
    def test_module_importable(self):
        # psycopg2 is installed in CI for staging.
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

    def test_same_key_same_hash_is_safe(self):
        now = datetime.now(timezone.utc)
        self.reg.store("k_same", "h_same", {"n": 1}, now=now)
        # same key + same hash should not crash and should overwrite payload safely
        self.reg.store("k_same", "h_same", {"n": 2}, now=now)
        out = self.reg.lookup("k_same", "h_same", now=now)
        self.assertEqual({"n": 2}, out)

    def test_conflicting_duplicate_atomic_reject(self):
        now = datetime.now(timezone.utc)
        self.reg.store("k2", "h2", {"ok": True}, now=now)
        with self.assertRaises(ValueError):
            self.reg.store("k2", "DIFFERENT", {"ok": False}, now=now)
        # original still present
        out = self.reg.lookup("k2", "h2", now=now)
        self.assertEqual({"ok": True}, out)

    def test_ttl_expiry_allows_replacement(self):
        now = datetime.now(timezone.utc)
        self.reg.store("k3", "h3", {"ok": True}, now=now)
        later = now + timedelta(seconds=11)
        # expired row: lookup returns None and deletes
        self.assertIsNone(self.reg.lookup("k3", "h3", now=later))
        # replacement after expiry is allowed
        self.reg.store("k3", "NEW", {"ok": "new"}, now=later)
        out = self.reg.lookup("k3", "NEW", now=later)
        self.assertEqual({"ok": "new"}, out)


if __name__ == "__main__":
    unittest.main()
