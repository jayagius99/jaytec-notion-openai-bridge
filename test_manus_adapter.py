import io
import json
import unittest
import urllib.error
from unittest import mock

import manus_adapter as ma


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _Response:
    status = 200
    headers = _Headers({"x-request-id": "req-safe"})
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, _limit=-1):
        return self.payload


class ManusAdapterTests(unittest.TestCase):
    def test_key_required(self):
        with self.assertRaises(ma.ManusError):
            ma.ManusClient(api_key="")

    def test_secret_only_in_request_header_not_result(self):
        seen = {}
        def fake(req, timeout):
            seen["key"] = req.get_header("X-manus-api-key")
            return _Response({"ok": True, "data": {"id": "u1", "credit_usage": 2}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            client = ma.ManusClient(api_key="secret-test-key")
            result = client._request("GET", "user.me")
        self.assertEqual(seen["key"], "secret-test-key")
        self.assertNotIn("secret-test-key", repr(result))
        self.assertEqual(result.credit_usage, 2)

    def test_credit_error_10091_fails_closed_without_retry(self):
        payload = json.dumps({"ok": False, "error": {"code": 10091, "message": "You don't have enough credits"}}).encode()
        error = urllib.error.HTTPError("https://example.invalid", 400, "bad", _Headers(), io.BytesIO(payload))
        with mock.patch("urllib.request.urlopen", side_effect=error) as call:
            with self.assertRaises(ma.ManusInsufficientCredits):
                ma.ManusClient(api_key="x").task_detail("t1")
        self.assertEqual(call.call_count, 1)

    def test_message_size_is_bounded_before_network(self):
        with mock.patch("urllib.request.urlopen") as call:
            with self.assertRaises(ma.ManusError) as ctx:
                ma.ManusClient(api_key="x").send_message("t1", "x" * (ma.MANUS_MAX_MESSAGE_CHARS + 1))
        self.assertEqual(str(ctx.exception), "MESSAGE_TOO_LARGE")
        call.assert_not_called()

    def test_invalid_json_is_rejected(self):
        class Bad(_Response):
            def read(self, _limit=-1):
                return b"not-json"
        with mock.patch("urllib.request.urlopen", return_value=Bad({})):
            with self.assertRaises(ma.ManusError) as ctx:
                ma.ManusClient(api_key="x").user_me()
        self.assertEqual(str(ctx.exception), "MANUS_RESPONSE_INVALID_JSON")


if __name__ == "__main__":
    unittest.main()
