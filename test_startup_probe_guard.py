import unittest

from orchestration import ExecutionRegistry
from startup_probe_guard import (
    AUTH_EXPIRES_AT_ENV,
    AUTH_ID_ENV,
    AUTH_PAYLOAD_SHA256_ENV,
    AUTH_PROBE_ENV,
    authorize_startup_probe,
    sha256_text,
)


class StartupProbeGuardTests(unittest.TestCase):
    def _env(self, probe: str, payload: str, now: int = 1000):
        return {
            AUTH_ID_ENV: "a" * 32,
            AUTH_PROBE_ENV: probe,
            AUTH_EXPIRES_AT_ENV: str(now + 300),
            AUTH_PAYLOAD_SHA256_ENV: payload,
        }

    def test_exact_authorization_is_consumed_once(self):
        now = 1000
        probe = "jaytec_read"
        payload = sha256_text("https://example.test/share")
        env = self._env(probe, payload, now)
        reg = ExecutionRegistry(ttl_seconds=86400)

        first = authorize_startup_probe(
            env=env,
            registry=reg,
            idempotency_store="postgres",
            probe_name=probe,
            payload_sha256=payload,
            now_epoch=now,
        )
        second = authorize_startup_probe(
            env=env,
            registry=reg,
            idempotency_store="postgres",
            probe_name=probe,
            payload_sha256=payload,
            now_epoch=now,
        )

        self.assertTrue(first.allowed)
        self.assertEqual("STARTUP_PROBE_AUTHORIZED_ONCE", first.reason)
        self.assertFalse(second.allowed)
        self.assertEqual("STARTUP_PROBE_AUTH_REPLAY", second.reason)

    def test_expired_authorization_fails_closed(self):
        probe = "god_project_review"
        payload = sha256_text("packet")
        env = self._env(probe, payload, now=1000)
        env[AUTH_EXPIRES_AT_ENV] = "999"
        out = authorize_startup_probe(
            env=env,
            registry=ExecutionRegistry(),
            idempotency_store="postgres",
            probe_name=probe,
            payload_sha256=payload,
            now_epoch=1000,
        )
        self.assertFalse(out.allowed)
        self.assertEqual("STARTUP_PROBE_AUTH_EXPIRED", out.reason)

    def test_overlong_authorization_fails_closed(self):
        probe = "deepseek_security_review"
        payload = sha256_text("packet")
        env = self._env(probe, payload, now=1000)
        env[AUTH_EXPIRES_AT_ENV] = str(1000 + 901)
        out = authorize_startup_probe(
            env=env,
            registry=ExecutionRegistry(),
            idempotency_store="postgres",
            probe_name=probe,
            payload_sha256=payload,
            now_epoch=1000,
        )
        self.assertFalse(out.allowed)
        self.assertEqual("STARTUP_PROBE_AUTH_LIFETIME_TOO_LONG", out.reason)

    def test_probe_mismatch_fails_closed(self):
        payload = sha256_text("packet")
        env = self._env("jaytec_read", payload, now=1000)
        out = authorize_startup_probe(
            env=env,
            registry=ExecutionRegistry(),
            idempotency_store="postgres",
            probe_name="god_project_review",
            payload_sha256=payload,
            now_epoch=1000,
        )
        self.assertFalse(out.allowed)
        self.assertEqual("STARTUP_PROBE_AUTH_PROBE_MISMATCH", out.reason)

    def test_payload_mismatch_fails_closed(self):
        probe = "jaytec_read"
        payload = sha256_text("packet-a")
        env = self._env(probe, payload, now=1000)
        out = authorize_startup_probe(
            env=env,
            registry=ExecutionRegistry(),
            idempotency_store="postgres",
            probe_name=probe,
            payload_sha256=sha256_text("packet-b"),
            now_epoch=1000,
        )
        self.assertFalse(out.allowed)
        self.assertEqual("STARTUP_PROBE_AUTH_PAYLOAD_MISMATCH", out.reason)

    def test_non_durable_store_fails_closed(self):
        probe = "jaytec_read"
        payload = sha256_text("packet")
        env = self._env(probe, payload, now=1000)
        out = authorize_startup_probe(
            env=env,
            registry=ExecutionRegistry(),
            idempotency_store="process_memory_staging_only",
            probe_name=probe,
            payload_sha256=payload,
            now_epoch=1000,
        )
        self.assertFalse(out.allowed)
        self.assertEqual("STARTUP_PROBE_DURABLE_STATE_REQUIRED", out.reason)


if __name__ == "__main__":
    unittest.main()
