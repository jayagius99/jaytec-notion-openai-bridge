"""Fail-closed Manus profile routing policy for JAYTEC.

Standing rule:
- Manus Lite is the default and only allowed profile.
- Paid Manus profiles require an explicit, per-task Jay override.
- A route that cannot explicitly select a Manus profile is not allowed.
- A Manus result cannot be counted as verified participation unless the
  observed profile can be verified against the requested profile.
- Never fall back from Lite to a paid profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


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
_VERSIONED_PROFILE = re.compile(
    r"^(?:manus[- ]?)?(?P<version>\\d+(?:\\.\\d+)+)(?:[- ](?P<tier>lite|max))?$",
    re.I,
)


class PaidOverrideAuthority(StrEnum):
    CURRENT_USER_MESSAGE = "current_user_message"


class ManusProfilePolicyError(RuntimeError):
    """Raised when a Manus route would violate the standing profile policy."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManusRouteDecision:
    requested_profile: ManusProfile
    explicit_paid_override: bool

    @property
    def paid_profile(self) -> bool:
        return self.requested_profile is not ManusProfile.LITE


def canonicalize_manus_profile(value: str | ManusProfile | None) -> ManusProfile:
    """Normalize UI/API aliases into JAYTEC's canonical Manus profiles."""

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
    """Authorize a Manus dispatch before any provider call is made.

    The default is always Lite. A route without an explicit profile selector
    fails closed because it could silently land on a paid Manus tier.
    """

    profile = canonicalize_manus_profile(requested_profile)

    if type(route_supports_profile_selector) is not bool:
        raise ManusProfilePolicyError("MANUS_SELECTOR_CAPABILITY_INVALID")
    if type(explicit_paid_override) is not bool:
        raise ManusProfilePolicyError("MANUS_OVERRIDE_FLAG_INVALID")

    if not route_supports_profile_selector:
        raise ManusProfilePolicyError("MANUS_PROFILE_SELECTOR_UNAVAILABLE")

    authority: PaidOverrideAuthority | None = None
    if paid_override_authority is not None:
        try:
            authority = (
                paid_override_authority
                if isinstance(paid_override_authority, PaidOverrideAuthority)
                else PaidOverrideAuthority(str(paid_override_authority).strip().casefold())
            )
        except ValueError as exc:
            raise ManusProfilePolicyError("MANUS_OVERRIDE_AUTHORITY_INVALID") from exc

    if profile is not ManusProfile.LITE:
        if not explicit_paid_override:
            raise ManusProfilePolicyError("MANUS_PAID_PROFILE_BLOCKED")
        if authority is not PaidOverrideAuthority.CURRENT_USER_MESSAGE:
            raise ManusProfilePolicyError("MANUS_PAID_OVERRIDE_NOT_CURRENT")

    return ManusRouteDecision(
        requested_profile=profile,
        explicit_paid_override=explicit_paid_override,
    )


def verify_manus_profile(
    decision: ManusRouteDecision,
    *,
    observed_profile: str | ManusProfile | None,
) -> ManusRouteDecision:
    """Verify the provider-observed Manus profile after dispatch.

    Missing identity is not success. This mirrors JAYTEC's exact-model
    specialist policy: a task may have run, but it cannot be accepted as
    verified Manus participation without observable matching identity.
    """

    if observed_profile is None:
        raise ManusProfilePolicyError("MANUS_PROFILE_UNOBSERVABLE")

    observed = canonicalize_manus_profile(observed_profile)
    if observed is not decision.requested_profile:
        raise ManusProfilePolicyError("MANUS_PROFILE_MISMATCH")

    return decision


def allow_manus_fallback(*, from_profile: str | ManusProfile, to_profile: str | ManusProfile) -> bool:
    """JAYTEC never automatically falls back between Manus profiles."""

    canonicalize_manus_profile(from_profile)
    canonicalize_manus_profile(to_profile)
    return False
