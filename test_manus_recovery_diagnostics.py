from __future__ import annotations

import unittest

from manus_recovery_diagnostics import (
    ManusRecoveryDiagnosticError,
    recovery_key_prefix,
    safe_result_projection,
)


class ManusRecoveryDiagnosticsTests(unittest.TestCase):
    def test_attempt_prefix_is_exact_and_route_agnostic(self):
        self.assertEqual(
            recovery_key_prefix("FORGE-GENESIS-ACTIVATION-001", 1),
            "manus:FORGE-GENESIS-ACTIVATION-001:recovery:1:",
        )

    def test_invalid_attempt_fails_closed(self):
        for value in (0, -1, 1001, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(ManusRecoveryDiagnosticError):
                    recovery_key_prefix("FORGE-GENESIS-ACTIVATION-001", value)  # type: ignore[arg-type]

    def test_safe_projection_keeps_only_known_nonsecret_fields(self):
        projected = safe_result_projection(
            {
                "schema_version": "JAYTEC_MANUS_LITE_RUNTIME_V1",
                "status": "FAILED_CLOSED",
                "error": "MANUS_APPROVED_CONNECTOR_MISSING:github",
                "requested_profile": "lite",
                "provider_task_id": None,
                "secret": "must-not-leak",
                "request_payload": {"prompt": "must-not-leak"},
                "connectors": ["github"],
            }
        )
        self.assertEqual(
            projected,
            {
                "schema_version": "JAYTEC_MANUS_LITE_RUNTIME_V1",
                "status": "FAILED_CLOSED",
                "error": "MANUS_APPROVED_CONNECTOR_MISSING:github",
                "requested_profile": "lite",
                "provider_task_id": None,
            },
        )
        self.assertNotIn("secret", projected)
        self.assertNotIn("request_payload", projected)
        self.assertNotIn("connectors", projected)


if __name__ == "__main__":
    unittest.main()
