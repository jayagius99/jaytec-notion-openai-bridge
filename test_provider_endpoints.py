import unittest

from provider_endpoints import (
    MANUS_API_BASE,
    OPENAI_API_BASE,
    OPENROUTER_API_BASE,
    ProviderEndpointPolicyError,
    validate_manus_endpoint,
    validate_openai_endpoint,
    validate_openrouter_endpoint,
)


class ProviderEndpointPolicyTests(unittest.TestCase):
    def test_canonical_endpoints_pass(self):
        self.assertEqual(validate_openai_endpoint(OPENAI_API_BASE), OPENAI_API_BASE)
        self.assertEqual(
            validate_openrouter_endpoint(OPENROUTER_API_BASE),
            OPENROUTER_API_BASE,
        )
        self.assertEqual(validate_manus_endpoint(MANUS_API_BASE), MANUS_API_BASE)

    def test_single_trailing_slash_normalizes(self):
        self.assertEqual(
            validate_openrouter_endpoint(OPENROUTER_API_BASE + "/"),
            OPENROUTER_API_BASE,
        )

    def test_attacker_hosts_fail_closed(self):
        cases = (
            (validate_openai_endpoint, "https://evil.example/v1"),
            (validate_openrouter_endpoint, "https://evil.example/api/v1"),
            (validate_manus_endpoint, "https://evil.example/v2"),
        )
        for validator, value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ProviderEndpointPolicyError):
                    validator(value)

    def test_http_endpoints_fail_closed(self):
        for validator, value in (
            (validate_openai_endpoint, "http://api.openai.com/v1"),
            (validate_openrouter_endpoint, "http://openrouter.ai/api/v1"),
            (validate_manus_endpoint, "http://api.manus.ai/v2"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ProviderEndpointPolicyError):
                    validator(value)

    def test_userinfo_query_fragment_or_wrong_path_fail_closed(self):
        values = (
            "https://user@openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1?x=1",
            "https://openrouter.ai/api/v1#fragment",
            "https://openrouter.ai/api/v2",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ProviderEndpointPolicyError):
                    validate_openrouter_endpoint(value)


if __name__ == "__main__":
    unittest.main()
