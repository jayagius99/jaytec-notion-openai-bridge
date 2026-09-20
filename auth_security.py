"""Fail-closed authentication policy for JAYTEC MCP bearer tokens."""
from __future__ import annotations


class McpAuthPolicyError(RuntimeError):
    pass


_BANNED = frozenset(
    {
        "test",
        "password",
        "changeme",
        "change-me",
        "secret",
        "token",
        "default",
    }
)


def is_strong_mcp_auth_token(token: str) -> bool:
    if not isinstance(token, str) or token != token.strip():
        return False
    if len(token) < 32 or any(ch.isspace() for ch in token):
        return False
    if token.casefold() in _BANNED:
        return False
    # Reject trivial repeated material while allowing normal random hex/base64/url-safe secrets.
    if len(set(token)) < 8:
        return False
    return True


def require_mcp_auth_token(token: str, *, production: bool) -> None:
    if not isinstance(token, str) or not token:
        raise McpAuthPolicyError("MCP_AUTH_TOKEN_REQUIRED")
    if production and not is_strong_mcp_auth_token(token):
        raise McpAuthPolicyError("MCP_AUTH_TOKEN_WEAK")
