"""Authentication policy for JAYTEC MCP bearer tokens."""
from __future__ import annotations


class McpAuthPolicyError(RuntimeError):
    pass


_BANNED = {
    "test",
    "password",
    "changeme",
    "change-me",
    "secret",
    "token",
    "default",
}


def is_strong_mcp_auth_token(token: str) -> bool:
    if not isinstance(token, str):
        return False
    if token != token.strip():
        return False
    if len(token) < 32:
        return False
    if any(ch.isspace() for ch in token):
        return False
    lowered = token.casefold()
    if lowered in _BANNED:
        return False
    # Reject trivially repeated material while allowing normal hex/base64/url-safe tokens.
    if len(set(token)) < 8:
        return False
    return True


def require_mcp_auth_token(token: str, *, production: bool) -> None:
    if not isinstance(token, str) or not token:
        raise McpAuthPolicyError("MCP_AUTH_TOKEN_REQUIRED")
    if production and not is_strong_mcp_auth_token(token):
        raise McpAuthPolicyError("MCP_AUTH_TOKEN_WEAK")
