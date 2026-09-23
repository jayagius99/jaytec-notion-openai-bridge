import json
import os
import unittest
from unittest.mock import patch

import dan_cognition_http as cognition


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DanCognitionPolicyTests(unittest.TestCase):
    def base(self, seat="DEEP", model="openai/gpt-oss-20b:free"):
        return {
            "relay_id": "DAN-CORE-TRIAD-01",
            "request_id": "req-1",
            "seat": seat,
            "model": model,
            "input": '{"proposal":"bounded candidate","evidence":["local test"]}',
            "max_tokens": 700,
        }

    def test_exact_free_deep_allowed(self):
        out = cognition._validate_request(self.base())
        self.assertEqual(out["seat"], "DEEP")
        self.assertTrue(out["model"].endswith(":free"))

    def test_exact_free_critic_allowed(self):
        out = cognition._validate_request(
            self.base(
                "CRITIC",
                "nvidia/nemotron-3-ultra-550b-a55b:free",
            )
        )
        self.assertEqual(out["seat"], "CRITIC")

    def test_nonfree_or_wrong_model_refused(self):
        bad = self.base(model="openai/gpt-oss-20b")
        with self.assertRaises(cognition.DanCognitionPolicyError):
            cognition._validate_request(bad)

        wrong = self.base(
            seat="CRITIC",
            model="openai/gpt-oss-20b:free",
        )
        with self.assertRaises(cognition.DanCognitionPolicyError):
            cognition._validate_request(wrong)

    def test_sensitive_payload_refused(self):
        for value in (
            "api_key=abcd1234",
            "C:\\JAYTEC\\PrivateLibrary\\secret.txt",
            "person@example.com",
            "Bearer abcdefghijklmnop",
        ):
            bad = self.base()
            bad["input"] = value
            with self.subTest(value=value):
                with self.assertRaises(cognition.DanCognitionPolicyError):
                    cognition._validate_request(bad)

    def test_auth_uses_separate_cognition_token(self):
        scope = {"headers": [(b"authorization", b"Bearer cognition-only")]}
        with patch.dict(
            os.environ,
            {"DAN_COGNITION_HTTP_TOKEN": "cognition-only"},
            clear=False,
        ):
            cognition.DanCognitionMiddleware._authenticate(scope)

        wrong_scope = {"headers": [(b"authorization", b"Bearer recovery-token")]}
        with patch.dict(
            os.environ,
            {"DAN_COGNITION_HTTP_TOKEN": "cognition-only"},
            clear=False,
        ):
            with self.assertRaises(cognition.DanCognitionAuthError):
                cognition.DanCognitionMiddleware._authenticate(wrong_scope)

    def test_openrouter_call_is_exact_model_no_provider_fallback(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse(
                {
                    "model": "openai/gpt-oss-20b:free",
                    "choices": [
                        {"message": {"content": '{"conclusion":"ok"}'}}
                    ],
                }
            )

        req = cognition._validate_request(self.base())
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_BASE_URL": "https://openrouter.invalid/api/v1",
            },
            clear=False,
        ), patch.object(cognition.urllib.request, "urlopen", fake_urlopen):
            result = cognition._invoke_openrouter(req)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["body"]["model"], req["model"])
        self.assertTrue(captured["body"]["provider"]["allow_fallbacks"])
        self.assertEqual(result["authority"], "COGNITIVE_CANDIDATE_ONLY")
        self.assertEqual(result["side_effects"], "NONE")


if __name__ == "__main__":
    unittest.main()
