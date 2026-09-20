"""Fail-closed Manus profile routing policy for JAYTEC.

Permanent standing rule:
- Manus Lite is the ONLY allowed Manus profile.
- Standard, Max, unsuffixed paid/default aliases, and every future non-Lite
  profile are prohibited. There is no paid-profile override path.
- A route that cannot explicitly select Lite is not allowed.
- A Manus result cannot count as verified participation unless the observed
  profile is explicitly observable and verifies as Lite.
- Never fall back from Lite to another profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ManusProfile(StrEnum):
    LITE = "lite"
    STANDARD = "standard"
    MAX = "max"


_PROFILE_ALIASES = {
    "lite": ManusProfile.LITE,
    "manus lite": ManusProfile.LITE,
    "standard": ManusProfile.STANDARD,
    "manus standard": ManusProfile.STANDARD,
    "max": ManusProfile.MAX,
    "manus max": ManusProfile.MAX,
}

# Versioned aliases are parsed only so non-Lite variants can be rejected
# deterministically. Unsuffixed versions are treated as paid/default STANDARD.
_VERSIONED_PROFILE = re.compile(
    r"^(?:manus[- ]?)?(?P<version>\d+(?:\.\d+)+)(?:[- ](?P<tier>lite|max))?$",
    re.I,
)


class PaidOverrideAuthority(StrEnum):
    """Legacy compatibility token.

    Kept so older callers fail closed with a policy error instead of crashing
    on import. No value of this enum can authorize a paid Manus profile.
    """

    CURRENT_USER_MESSAGE = "current_user_message"


class ManusProfilePolicyError(RuntimeError):
    """Raised when a Manus route would violate the standing profile policy."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManusRouteDecision:
    requested_profile: ManusProfile
    explicit_paid_override: bool = False

    @property
    def paid_profile(self) -> bool:
        return self.requested_profile is not ManusProfile.LITE


def canonicalize_manus_profile(value: str | ManusProfile | None) -> ManusProfile:
    """Normalize known UI/API aliases so the Lite-only gate can verify them."""

    if value is None:
        return ManusProfile.LITE
    if isinstance(value, ManusProfile):
        return value

    normalized = " ".join(str(value).strip().casefold().split())
    profile = _PROFILE_ALIASES.get(normalized)
    if profile is not None:
        return profile

    versioned = _VERSIONED_PROFILE.fullmatch(normalized)
    if versioned is not None:
        tier = versioned.group("tier")
        if tier is None:
            return ManusProfile.STANDARD
        if tier.casefold() == "lite":
            return ManusProfile.LITE
        if tier.casefold() == "max":
            return ManusProfile.MAX

    raise ManusProfilePolicyError("MANUS_PROFILE_UNKNOWN")


def authorize_manus_route(
    *,
    requested_profile: str | ManusProfile | None = None,
    route_supports_profile_selector: bool,
    explicit_paid_override: bool = False,
    paid_override_authority: str | PaidOverrideAuthority | None = None,
) -> ManusRouteDecision:
    """Authorize one Manus route before any provider call.

    Lite is the only legal result. Override-shaped inputs are retained solely
    for backwards-compatible fail-closed handling and can never grant access.
    """

    if type(route_supports_profile_selector) is not bool:
        raise ManusProfilePolicyError("MANUS_SELECTOR_CAPABILITY_INVALID")
    if type(explicit_paid_override) is not bool:
        raise ManusProfilePolicyError("MANUS_OVERRIDE_FLAG_INVALID")

    if not route_supports_profile_selector:
        raise ManusProfilePolicyError("MANUS_PROFILE_SELECTOR_UNAVAILABLE")

    # Any attempt to invoke legacy paid-override semantics is itself blocked,
    # even when the requested profile string says Lite.
    if explicit_paid_override or paid_override_authority is not None:
        raise ManusProfilePolicyError("MANUS_PAID_OVERRIDE_PERMANENTLY_DISABLED")

    profile = canonicalize_manus_profile(requested_profile)
    if profile is not ManusProfile.LITE:
        raise ManusProfilePolicyError("MANUS_NON_LITE_PROFILE_PERMANENTLY_BLOCKED")

    return ManusRouteDecision(
        requested_profile=ManusProfile.LITE,
        explicit_paid_override=False,
    )


def verify_manus_profile(
    decision: ManusRouteDecision,
    *,
    observed_profile: str | ManusProfile | None,
) -> ManusRouteDecision:
    """Verify the provider-observed profile is explicitly Lite."""

    if decision.requested_profile is not ManusProfile.LITE:
        raise ManusProfilePolicyError("MANUS_ROUTE_DECISION_NOT_LITE")
    if observed_profile is None:
        raise ManusProfilePolicyError("MANUS_PROFILE_UNOBSERVABLE")

    observed = canonicalize_manus_profile(observed_profile)
    if observed is not ManusProfile.LITE:
        raise ManusProfilePolicyError("MANUS_PROFILE_MISMATCH")

    return decision


def allow_manus_fallback(
    *,
    from_profile: str | ManusProfile,
    to_profile: str | ManusProfile,
) -> bool:
    """JAYTEC never falls back between Manus profiles."""

    canonicalize_manus_profile(from_profile)
    canonicalize_manus_profile(to_profile)
    return False
