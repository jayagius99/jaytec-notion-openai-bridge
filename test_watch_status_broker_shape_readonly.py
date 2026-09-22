import json
import unittest

from manus_runtime import specialist_request_shape_diagnostics


class WatchStatusBrokerShapeReadonlyTests(unittest.TestCase):
    def test_reports_only_broker_operation_and_field_names(self):
        secret_value = "DO-NOT-LEAK-THIS-VALUE"
        result = {
            "specialist_requests": [
                json.dumps(
                    {
                        "type": "SPECIALIST_REQUEST",
                        "request_id": "hidden-request-id",
                        "parent_task_id": "FORGE-GENESIS-ACTIVATION-001",
                        "directive_version": "JAYTEC_MANUS_GOVERNANCE_V1",
                        "specialist": "github_broker",
                        "objective": "hidden objective",
                        "reason": "hidden reason",
                        "required_context": {
                            "operation": "read_file",
                            "path": secret_value,
                            "ref": "main",
                            "repository": secret_value,
                        },
                        "authority": "REQUEST_ONLY_NO_SELF_DISPATCH",
                        "packet_sha256": "0" * 64,
                    },
                    sort_keys=True,
                )
            ]
        }

        rows = specialist_request_shape_diagnostics(result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["specialist"], "github_broker")
        self.assertEqual(rows[0]["operation"], "read_file")
        self.assertEqual(
            rows[0]["required_context_fields"],
            ["operation", "path", "ref", "repository"],
        )
        self.assertFalse(rows[0]["values_included"])
        encoded = json.dumps(rows, sort_keys=True)
        self.assertNotIn(secret_value, encoded)
        self.assertNotIn("hidden-request-id", encoded)
        self.assertNotIn("hidden objective", encoded)
        self.assertNotIn("hidden reason", encoded)

    def test_ignores_non_broker_specialist_requests(self):
        result = {
            "specialist_requests": [
                json.dumps(
                    {
                        "specialist": "nemo",
                        "required_context": {"secret": "not-exposed"},
                    }
                )
            ]
        }
        self.assertEqual(specialist_request_shape_diagnostics(result), [])

    def test_hashes_unusual_field_names(self):
        result = {
            "specialist_requests": [
                json.dumps(
                    {
                        "specialist": "github_broker",
                        "required_context": {
                            "operation": "read_issue",
                            "number": 59,
                            "bad field with spaces": "hidden",
                        },
                    }
                )
            ]
        }
        rows = specialist_request_shape_diagnostics(result)
        fields = rows[0]["required_context_fields"]
        self.assertIn("operation", fields)
        self.assertIn("number", fields)
        self.assertTrue(any(name.startswith("sha256-") for name in fields))
        self.assertNotIn("bad field with spaces", fields)


if __name__ == "__main__":
    unittest.main()
