import unittest
from unittest.mock import patch

from autorecovery_watch_ingress import (
    FORGE_TASK_ID,
    WatchIngressError,
    _observed_refs,
    execute_watch_cycle,
)


class WatchIngressPolicyTests(unittest.TestCase):
    def test_only_canonical_forge_task_is_allowed_by_constant(self):
        self.assertEqual(FORGE_TASK_ID, "FORGE-GENESIS-ACTIVATION-001")

    def test_observed_refs_are_bounded_and_nonempty(self):
        with self.assertRaisesRegex(WatchIngressError, "REQUIRED"):
            _observed_refs({})
        refs = _observed_refs({"main": "a" * 40})
        self.assertEqual(refs["main"], "a" * 40)

    def test_ref_count_is_bounded(self):
        with self.assertRaisesRegex(WatchIngressError, "TOO_MANY"):
            _observed_refs({f"r-{i}": "a" * 40 for i in range(129)})

    def test_disabled_runtime_does_not_require_manus_components(self):
        with patch(
            "autorecovery_watch_ingress.prepare_schema_if_authorized",
            return_value={"status": "PASS", "schema_present": True},
        ):
            result = execute_watch_cycle(
                {
                    "task_id": FORGE_TASK_ID,
                    "observed_refs": {"main": "a" * 40},
                },
                database_url="postgresql://placeholder/not-contacted",
                manus_runtime=None,
                registry=object(),
                runtime_components_registered=False,
                env={"JAYTEC_AUTORECOVERY_ENABLED": "0"},
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "AUTORECOVERY_RUNTIME_NOT_ACTIVE")

    def test_requested_runtime_fails_closed_when_components_missing(self):
        with patch(
            "autorecovery_watch_ingress.prepare_schema_if_authorized",
            return_value={"status": "PASS", "schema_present": True},
        ):
            result = execute_watch_cycle(
                {
                    "task_id": FORGE_TASK_ID,
                    "observed_refs": {"main": "a" * 40},
                },
                database_url="postgresql://placeholder/not-contacted",
                manus_runtime=None,
                registry=object(),
                runtime_components_registered=False,
                env={
                    "JAYTEC_AUTORECOVERY_ENABLED": "1",
                    "JAYTEC_AUTORECOVERY_SCHEMA_READY": "1",
                    "JAYTEC_AUTORECOVERY_CALLABLE_ROUTES": '["jaytec-manus-lite-v1"]',
                    "JAYTEC_AUTORECOVERY_CHECKPOINT_VERIFIER": "github_exact_head_v1",
                    "JAYTEC_AUTORECOVERY_HEARTBEAT_MODE": "fenced_postgres_v1",
                    "JAYTEC_AUTORECOVERY_NOTIFICATION_MODE": "event_log_v1",
                },
            )
        self.assertEqual(result["status"], "BLOCKED_FAIL_CLOSED")
        self.assertEqual(result["reason"], "AUTORECOVERY_RUNTIME_NOT_ACTIVE")
        self.assertIn(
            "CALLABLE_RUNTIME_COMPONENTS_NOT_REGISTERED",
            result["runtime"]["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
