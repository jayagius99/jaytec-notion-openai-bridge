from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

import proving_grounds as pg


HEAD = "a" * 40


class ProvingGroundsTests(unittest.TestCase):
    def request(self, **overrides):
        value = {
            "task_id": "FORGE-GENESIS-ACTIVATION-001",
            "run_id": "pg-proof-1",
            "suite_id": "forge-cognition-core",
            "expected_runtime_commit": HEAD,
            "operation": "test",
        }
        value.update(overrides)
        return json.dumps(value, sort_keys=True)

    def test_roster_entry_has_zero_authority(self):
        entry = pg.roster_entry()
        self.assertEqual(entry["kind"], "JAYTEC_RESOURCE")
        self.assertEqual(entry["resource_id"], "PROVING_GROUNDS")
        self.assertEqual(entry["authority"], "NONE")
        self.assertFalse(entry["can_plan"])
        self.assertFalse(entry["can_route"])
        self.assertFalse(entry["can_authorize"])
        self.assertFalse(entry["can_modify_production"])
        self.assertFalse(entry["arbitrary_command_execution"])

    def test_roster_entry_validation_fails_closed_on_authority_drift(self):
        entry = pg.roster_entry()
        entry["can_authorize"] = True
        with self.assertRaisesRegex(pg.ProvingGroundsError, "ROSTER_MISMATCH"):
            pg.validate_roster_entry(entry)

    def test_unknown_suite_rejected(self):
        result = pg.run_registered_suite(
            self.request(suite_id="shell-whatever"),
            env={"RENDER_GIT_COMMIT": HEAD},
        )
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertIn("SUITE_NOT_REGISTERED", result["reason"])

    def test_runtime_commit_mismatch_rejected_before_subprocess(self):
        with patch.object(pg.subprocess, "run") as run:
            result = pg.run_registered_suite(
                self.request(),
                env={"RENDER_GIT_COMMIT": "b" * 40},
            )
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertIn("RUNTIME_COMMIT_MISMATCH", result["reason"])
        run.assert_not_called()

    def test_exact_registered_suite_uses_no_shell_and_fixed_modules(self):
        fake = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "OK", "stderr": ""},
        )()
        with patch.object(pg.subprocess, "run", return_value=fake) as run:
            result = pg.run_registered_suite(
                self.request(),
                env={"RENDER_GIT_COMMIT": HEAD},
            )
        self.assertEqual(result["status"], "PASS")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [sys.executable, "-m", "unittest", "-q"])
        self.assertEqual(
            argv[4:],
            list(pg.PROVING_GROUNDS_SUITES["forge-cognition-core"]),
        )
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertFalse(result["side_effects_authorized"])
        self.assertFalse(result["arbitrary_command_execution"])

    def test_context_or_command_injection_fields_rejected(self):
        payload = json.loads(self.request())
        payload["command"] = "rm -rf /"
        result = pg.run_registered_suite(
            json.dumps(payload),
            env={"RENDER_GIT_COMMIT": HEAD},
        )
        self.assertEqual(result["status"], "FAILED_CLOSED")
        self.assertIn("UNKNOWN_FIELDS:command", result["reason"])

    def test_catalog_reports_only_registered_suites(self):
        catalog = pg.catalog({"RENDER_GIT_COMMIT": HEAD})
        self.assertTrue(catalog["runtime_commit_verified"])
        self.assertEqual(
            set(catalog["suites"]),
            set(pg.PROVING_GROUNDS_SUITES),
        )


if __name__ == "__main__":
    unittest.main()
