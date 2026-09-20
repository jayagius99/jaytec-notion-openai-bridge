import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class WatchOIDCIngressIsolationTests(unittest.TestCase):
    def test_staging_server_keeps_static_mcp_auth_only(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        self.assertIn("from fastmcp.server.auth import StaticTokenVerifier", source)
        self.assertNotIn("MultiAuth", source)
        self.assertNotIn("AuthMiddleware", source)
        self.assertNotIn("get_access_token", source)
        self.assertIn("watch_oidc_auth.verify_token(raw_token)", source)

    def test_watch_route_requires_manual_bearer_verification(self):
        source = (ROOT / "staging_server.py").read_text(encoding="utf-8")
        self.assertIn('@mcp.custom_route("/jaytec/watch-cycle"', source)
        self.assertIn('authorization.startswith("Bearer ")', source)
        self.assertIn('"WATCH_OIDC_BEARER_REQUIRED"', source)
        self.assertIn('"WATCH_OIDC_IDENTITY_INVALID"', source)


if __name__ == "__main__":
    unittest.main()
