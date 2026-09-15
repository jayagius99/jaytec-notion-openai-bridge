import unittest

from staging_server import _json_object


class TestWorkerOutputParsing(unittest.TestCase):
    def test_plain_json(self):
        value = _json_object('{"status": "SUCCESS"}')
        self.assertEqual("SUCCESS", value["status"])

    def test_code_fenced_json(self):
        value = _json_object("```json\n{\"status\": \"SUCCESS\"}\n```")
        self.assertEqual("SUCCESS", value["status"])

    def test_non_object_rejected(self):
        with self.assertRaises(ValueError):
            _json_object("[]")


if __name__ == "__main__":
    unittest.main()
