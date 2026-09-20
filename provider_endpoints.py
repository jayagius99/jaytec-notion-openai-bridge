"""Canonical provider endpoint policy for JAYTEC.

Model identity checks are not enough if a credential can be redirected to an
arbitrary host. Production/staging provider routes must therefore use the exact
provider endpoint owned by that provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


class ProviderEndpointPolicyError(RuntimeError):
    pass


OPENAI_API_BASE = "https://api.openai.com/v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
MANUS_API_BASE = "https://api.manus.ai/v2"


@dataclass(frozen=True)
class ProviderEndpoint:
    provider: str
    url: str


def _canonical_endpoint(value: str, *, provider: str, expected: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderEndpointPolicyError(f"{provider}_ENDPOINT_INVALID")

    normalized = value[:-1] if value.endswith("/") else value
    parsed = urlparse(normalized)
    expected_parsed = urlparse(expected)

    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
        or parsed.hostname != expected_parsed.hostname
        or parsed.path != expected_parsed.path
    ):
        raise ProviderEndpointPolicyError(
            f"{provider}_ENDPOINT_NOT_CANONICAL"
        )
    return expected


def validate_openai_endpoint(value: str) -> str:
    return _canonical_endpoint(
        value,
        provider="OPENAI",
        expected=OPENAI_API_BASE,
    )


def validate_openrouter_endpoint(value: str) -> str:
    return _canonical_endpoint(
        value,
        provider="OPENROUTER",
        expected=OPENROUTER_API_BASE,
    )


def validate_manus_endpoint(value: str) -> str:
    return _canonical_endpoint(
        value,
        provider="MANUS",
        expected=MANUS_API_BASE,
    )
