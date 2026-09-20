"""Narrow GitHub Actions OIDC verifier for the JAYTEC WATCH ingress.

This verifier is intentionally NOT installed as the global FastMCP auth source.
It is used only by the dedicated /jaytec/watch-cycle custom HTTP route, so a
GitHub Actions identity cannot access ordinary JAYTEC MCP tools.
"""
from __future__ import annotations

from typing import Any, Mapping

from fastmcp.server.auth.providers.jwt import JWTVerifier

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_JWKS = "https://token.actions.githubusercontent.com/.well-known/jwks"
WATCH_OIDC_AUDIENCE = "jaytec-watch-attestation-v1"
WATCH_REPOSITORY = "jayagius99/jaytec-work-engine-v2-g1"
WATCH_REPOSITORY_ID = "1375381712"
WATCH_REF = "refs/heads/main"
WATCH_WORKFLOW = "JAYTEC WATCH"
WATCH_WORKFLOW_REF = (
    "jayagius99/jaytec-work-engine-v2-g1/"
    ".github/workflows/jaytec-watch.yml@refs/heads/main"
)
ALLOWED_EVENTS = frozenset({"schedule", "issues", "workflow_dispatch"})


def validate_watch_claims(claims: Mapping[str, Any]) -> tuple[bool, str]:
    required = {
        "repository": WATCH_REPOSITORY,
        "repository_id": WATCH_REPOSITORY_ID,
        "ref": WATCH_REF,
        "workflow": WATCH_WORKFLOW,
        "workflow_ref": WATCH_WORKFLOW_REF,
        "repository_visibility": "private",
        "runner_environment": "github-hosted",
    }
    for key, expected in required.items():
        if str(claims.get(key) or "") != expected:
            return False, "OIDC_CLAIM_MISMATCH:" + key

    event_name = str(claims.get("event_name") or "")
    if event_name not in ALLOWED_EVENTS:
        return False, "OIDC_EVENT_NOT_ALLOWED"

    for key in ("run_id", "run_number", "run_attempt", "workflow_sha", "sha"):
        value = str(claims.get(key) or "").strip()
        if not value:
            return False, "OIDC_CLAIM_MISSING:" + key

    sub = str(claims.get("sub") or "")
    if not sub.startswith("repo:" + WATCH_REPOSITORY + ":"):
        return False, "OIDC_SUBJECT_INVALID"

    return True, "WATCH_OIDC_IDENTITY_VERIFIED"


class GitHubActionsWatchOIDCVerifier:
    """Verify GitHub's JWT and then enforce the exact WATCH workflow identity."""

    def __init__(self) -> None:
        self._jwt = JWTVerifier(
            jwks_uri=GITHUB_OIDC_JWKS,
            issuer=GITHUB_OIDC_ISSUER,
            audience=WATCH_OIDC_AUDIENCE,
            algorithm="RS256",
        )

    async def verify(self, token: str):
        access = await self._jwt.verify_token(token)
        if access is None:
            return None, "OIDC_JWT_INVALID"
        ok, reason = validate_watch_claims(access.claims)
        if not ok:
            return None, reason
        return access, reason


__all__ = [
    "ALLOWED_EVENTS",
    "GITHUB_OIDC_ISSUER",
    "GITHUB_OIDC_JWKS",
    "WATCH_OIDC_AUDIENCE",
    "WATCH_REF",
    "WATCH_REPOSITORY",
    "WATCH_REPOSITORY_ID",
    "WATCH_WORKFLOW",
    "WATCH_WORKFLOW_REF",
    "GitHubActionsWatchOIDCVerifier",
    "validate_watch_claims",
]
