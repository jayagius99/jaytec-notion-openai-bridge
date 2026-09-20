import json
import unittest

from autorecovery_runtime import (
    assignment_status,
    runtime_status,
    validate_checkpoint_payload,
)


def checkpoint_payload():
    return {
        "task_id": "GOD-PREP-0017",
        "objective": "Prepare GOD Mode safely.",
        "current_phase": "pre-activation audit",
        "completed_work": ["A", "B"],
        "remaining_work": ["C"],
        "last_safe_checkpoint": "checkpoint-42",
        "repo": "jayagius99/jaytec-work-engine-v2-g1",
        "branch": "security/root-owner-control-v1",
        "commit_head": "362face5532ca986223573b47d6c7c247565a788",
        "open_pr": 17,
        "current_files_state": {"dirty": False},
        "tests_completed": ["root-owner-security-validation"],
        "known_failures": [],
        "active_constraints": ["do not activate production"],
        "authority_envelope": {"root_owner": "Jay"},
        "cost_envelope": {"paid_fallback": False},
        "dependencies": ["security keys"],
        "next_intended_action": "Continue exact saved audit.",
        "worker_specialist_preference": ["sol", "gemini"],
        "checkpoint_number": 42,
    }


class AutoRecoveryRuntimeTests(unittest.TestCase):
    def test_default_is_installed_but_disabled(self):
        status = runtime_status(env={}, database_url="")
        self.assertTrue(status.installed)
        self.assertFalse(status.requested_enabled)
        self.assertFalse(status.active)
        self.assertEqual(status.mode, "DISABLED")
        self.assertFalse(status.ui_chat_autoresume_supported)
        self.assertEqual(status.max_recovery_attempts, 3)

    def test_requested_activation_fails_closed_without_all_prerequisites(self):
        status = runtime_status(
            env={"JAYTEC_AUTORECOVERY_ENABLED": "1"},
            database_url="",
        )
        self.assertFalse(status.active)
        self.assertEqual(status.mode, "BLOCKED_FAIL_CLOSED")
        for blocker in (
            "DATABASE_URL_NOT_CONFIGURED",
            "AUTORECOVERY_SCHEMA_NOT_READY",
            "NO_JAYTEC_CALLABLE_WORKER_ROUTE",
            "CHECKPOINT_VERIFIER_NOT_CONFIGURED",
            "HEARTBEAT_MODE_NOT_CONFIGURED",
            "NOTIFICATION_MODE_NOT_CONFIGURED",
        ):
            self.assertIn(blocker, status.blockers)

    def test_all_activation_prerequisites_are_explicit(self):
        status = runtime_status(
            env={
                "JAYTEC_AUTORECOVERY_ENABLED": "1",
                "JAYTEC_AUTORECOVERY_SCHEMA_READY": "1",
                "JAYTEC_AUTORECOVERY_CALLABLE_ROUTES": json.dumps(
                    ["jaytec-worker-v1"]
                ),
                "JAYTEC_AUTORECOVERY_CHECKPOINT_VERIFIER": "github_exact_head_v1",
                "JAYTEC_AUTORECOVERY_HEARTBEAT_MODE": "fenced_postgres_v1",
                "JAYTEC_AUTORECOVERY_NOTIFICATION_MODE": "event_log_v1",
            },
            database_url="postgresql://placeholder/not-used-by-status",
        )
        self.assertTrue(status.active)
        self.assertEqual(status.mode, "ACTIVE_CALLABLE_WORKERS_ONLY")
        self.assertEqual(status.blockers, ())
        self.assertEqual(status.callable_worker_routes, ("jaytec-worker-v1",))

    def test_invalid_boolean_fails_closed(self):
        status = runtime_status(
            env={"JAYTEC_AUTORECOVERY_ENABLED": "yes"},
            database_url="",
        )
        self.assertFalse(status.active)
        self.assertIn(
            "JAYTEC_AUTORECOVERY_ENABLED_INVALID_BOOLEAN",
            status.blockers,
        )

    def test_unknown_runtime_modes_fail_closed(self):
        status = runtime_status(
            env={
                "JAYTEC_AUTORECOVERY_ENABLED": "1",
                "JAYTEC_AUTORECOVERY_CHECKPOINT_VERIFIER": "trust_me",
            },
            database_url="postgres://x",
        )
        self.assertFalse(status.active)
        self.assertIn("CHECKPOINT_VERIFIER_UNSUPPORTED", status.blockers)

    def test_checkpoint_validation_is_non_authorizing(self):
        result = validate_checkpoint_payload(checkpoint_payload())
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["task_id"], "GOD-PREP-0017")
        self.assertEqual(result["checkpoint_number"], 42)
        self.assertEqual(
            result["continuation_instruction"],
            "Resume — do not recreate completed work",
        )
        self.assertFalse(result["automatic_recovery_authorized"])

    def test_checkpoint_validation_rejects_missing_head(self):
        payload = checkpoint_payload()
        payload["commit_head"] = ""
        with self.assertRaisesRegex(Exception, "CHECKPOINT_VALIDATION_FAILED"):
            validate_checkpoint_payload(payload)

    def test_assignment_status_does_not_touch_database_until_schema_declared(self):
        result = assignment_status(
            "postgresql://definitely-not-contacted",
            "GOD-PREP-0017",
            schema_ready_declared=False,
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "AUTORECOVERY_SCHEMA_NOT_READY")


if __name__ == "__main__":
    unittest.main()
