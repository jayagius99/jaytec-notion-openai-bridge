import unittest

from auth_security import (
    McpAuthPolicyError,
    is_strong_mcp_auth_token,
    require_mcp_auth_token,
)


class McpAuthSecurityTests(unittest.TestCase):
    def test_strong_random_like_token_is_accepted(self):
        token = "Ab9_xY7-Qp2Lm8Nv4Rs6Tu1Wx3Za5BcD"
        self.assertTrue(is_strong_mcp_auth_token(token))
        require_mcp_auth_token(token, production=True)

    def test_short_token_is_rejected_for_production(self):
        self.assertFalse(is_strong_mcp_auth_token("test"))
        with self.assertRaisesRegex(McpAuthPolicyError, "MCP_AUTH_TOKEN_WEAK"):
            require_mcp_auth_token("test", production=True)

    def test_short_token_is_allowed_only_for_nonproduction_test_paths(self):
        require_mcp_auth_token("test", production=False)

    def test_repeated_material_is_rejected(self):
        token = "A" * 64
        self.assertFalse(is_strong_mcp_auth_token(token))

    def test_whitespace_is_rejected(self):
        token = "Ab9_xY7-Qp2Lm8Nv4Rs6Tu1Wx3Za5BcD "
        self.assertFalse(is_strong_mcp_auth_token(token))


if __name__ == "__main__":
    unittest.main()
