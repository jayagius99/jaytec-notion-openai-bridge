import unittest

from worker_json import WorkerJsonError, json_object, json_object_with_diagnostics


class TestWorkerOutputParsing(unittest.TestCase):
    def test_plain_json(self):
        value = json_object('{"status": "SUCCESS"}')
        self.assertEqual("SUCCESS", value["status"])

    def test_code_fenced_json(self):
        value = json_object("```json\n{\"status\": \"SUCCESS\"}\n```")
        self.assertEqual("SUCCESS", value["status"])

    def test_surrounding_prose_extracts_one_complete_object(self):
        value, diagnostics = json_object_with_diagnostics(
            'Here is the result: {"status":"SUCCESS","note":"brace } inside string"} trailing text'
        )
        self.assertEqual("SUCCESS", value["status"])
        self.assertTrue(diagnostics.extracted_object)

    def test_non_object_rejected(self):
        with self.assertRaises(WorkerJsonError) as ctx:
            json_object("[]")
        self.assertEqual("ROOT_NOT_OBJECT", ctx.exception.code)

    def test_unterminated_string_fails_closed(self):
        with self.assertRaises(WorkerJsonError) as ctx:
            json_object('{"status":"SUCCESS","detail":"unterminated}')
        self.assertEqual("INCOMPLETE_OR_INVALID_JSON", ctx.exception.code)

    def test_unexpected_end_fails_closed(self):
        with self.assertRaises(WorkerJsonError) as ctx:
            json_object('{"status":"SUCCESS","findings":["a"]')
        self.assertEqual("INCOMPLETE_OR_INVALID_JSON", ctx.exception.code)

    def test_missing_quoted_property_fails_closed(self):
        with self.assertRaises(WorkerJsonError) as ctx:
            json_object('{"status":"SUCCESS", findings: []}')
        self.assertEqual("MALFORMED_JSON", ctx.exception.code)

    def test_balanced_but_malformed_json_is_not_repaired(self):
        with self.assertRaises(WorkerJsonError) as ctx:
            json_object('{"status":"SUCCESS",}')
        self.assertEqual("MALFORMED_JSON", ctx.exception.code)

    def test_empty_response_fails_closed(self):
        with self.assertRaises(WorkerJsonError) as ctx:
            json_object("   ")
        self.assertEqual("EMPTY_RESPONSE", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
