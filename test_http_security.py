import unittest
from pathlib import Path

from http_security import (
    HttpSecurityConfigError,
    load_host_origin_policy,
)

ROOT = Path(__file__).resolve().parent


class HttpSecurityTests(unittest.TestCase):
    def test_exact_host_and_empty_browser_origins_are_valid(self):
        policy = load_host_origin_policy(
            {
                "JAYTEC_MCP_ALLOWED_HOSTS_JSON": '["jaytec-orchestration-staging.onrender.com"]',
                "JAYTEC_MCP_ALLOWED_ORIGINS_JSON": "[]",
            }
        )
        self.assertEqual(
            policy.allowed_hosts,
            ("jaytec-orchestration-staging.onrender.com",),
        )
        self.assertEqual(policy.allowed_origins, ())

    def test_missing_host_allowlist_fails_closed(self):
        with self.assertRaisesRegex(
            HttpSecurityConfigError,
            "HTTP_ALLOWED_HOSTS_REQUIRED",
        ):
            load_host_origin_policy({})

    def test_wildcard_host_is_forbidden(self):
        with self.assertRaisesRegex(
            HttpSecurityConfigError,
            "HTTP_ALLOWED_HOST_INVALID",
        ):
            load_host_origin_policy(
                {"JAYTEC_MCP_ALLOWED_HOSTS_JSON": '["*"]'}
            )

    def test_scheme_in_host_is_forbidden(self):
        with self.assertRaisesRegex(
            HttpSecurityConfigError,
            "HTTP_ALLOWED_HOST_INVALID",
        ):
            load_host_origin_policy(
                {
                    "JAYTEC_MCP_ALLOWED_HOSTS_JSON":
                    '["https://jaytec-orchestration-staging.onrender.com"]'
                }
            )

    def test_non_https_browser_origin_is_forbidden(self):
        with self.assertRaisesRegex(
            HttpSecurityConfigError,
            "HTTP_ALLOWED_ORIGIN_INVALID",
        ):
            load_host_origin_policy(
                {
                    "JAYTEC_MCP_ALLOWED_HOSTS_JSON": '["example.com"]',
                    "JAYTEC_MCP_ALLOWED_ORIGINS_JSON": '["http://example.com"]',
                }
            )

    def test_server_sources_never_disable_host_origin_protection(self):
        for name in ("server.py", "staging_server.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("host_origin_protection=False", source)
            self.assertIn("host_origin_protection=True", source)
            self.assertIn("allowed_hosts=", source)


if __name__ == "__main__":
    unittest.main()
