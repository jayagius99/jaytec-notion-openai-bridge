"""Composed pre/post dispatch contract for JAYTEC -> Manus.

No caller should talk to Manus without passing all three gates:
1. profile/cost policy,
2. behavioural authority policy,
3. relationship topology policy.

The contract also blocks implicit connector inheritance. JAYTEC must explicitly
bind the connector set for every Manus task so account/project defaults cannot
silently introduce a new path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from manus_governance import (
    AuthorityDecision,
    AuthoritySource,
    ManusScope,
    assert_approved_manus_connectors,
    authorize_manus_action,
    verify_manus_completion,
)
from manus_policy import (
    ManusRouteDecision,
    authorize_manus_route,
    verify_manus_profile,
)
from relationship_policy import Actor, Purpose, authorize_relationship


class ManusDispatchContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManusDispatchAuthorization:
    project_id: str
    project_name: str
    connectors: tuple[str, ...]
    profile: ManusRouteDecision
    authority: AuthorityDecision


def authorize_manus_dispatch(
    *,
    project_id: str,
    expected_project_id: str,
    project_name: str,
    connectors: Sequence[str],
    connectors_explicit: bool,
    scope: str | ManusScope,
    authority_source: str | AuthoritySource,
    current_task_authorized: bool,
    requested_profile: str | None = "lite",
    route_supports_profile_selector: bool = True,
    explicit_paid_override: bool = False,
    paid_override_authority: str | None = None,
    notion_authorized_by_jay_via_chatgpt: bool = False,
) -> ManusDispatchAuthorization:
    """Authorize a single JAYTEC -> Manus dispatch before any provider call."""

    if not isinstance(project_id, str) or not project_id.strip():
        raise ManusDispatchContractError("MANUS_PROJECT_ID_REQUIRED")
    if not isinstance(expected_project_id, str) or not expected_project_id.strip():
        raise ManusDispatchContractError("MANUS_EXPECTED_PROJECT_ID_REQUIRED")
    if project_id.strip() != expected_project_id.strip():
        raise ManusDispatchContractError("MANUS_PROJECT_ID_MISMATCH")
    if str(project_name).strip().casefold() != "manus":
        raise ManusDispatchContractError("MANUS_PROJECT_NAME_MISMATCH")

    if type(connectors_explicit) is not bool or connectors_explicit is not True:
        # Manus API can inherit project/user defaults when connector IDs are
        # omitted. JAYTEC forbids that hidden route.
        raise ManusDispatchContractError("MANUS_CONNECTORS_MUST_BE_EXPLICIT")

    if not isinstance(connectors, (list, tuple)):
        raise ManusDispatchContractError("MANUS_CONNECTORS_INVALID")
    approved = assert_approved_manus_connectors(list(connectors))

    profile = authorize_manus_route(
        requested_profile=requested_profile,
        route_supports_profile_selector=route_supports_profile_selector,
        explicit_paid_override=explicit_paid_override,
        paid_override_authority=paid_override_authority,
    )

    authority = authorize_manus_action(
        scope=scope,
        authority_source=authority_source,
        explicit_current_task_authorization=current_task_authorized,
        notion_authorized_by_jay_via_chatgpt=notion_authorized_by_jay_via_chatgpt,
    )
    if not authority.allowed:
        raise ManusDispatchContractError("MANUS_ACTION_NOT_AUTHORIZED")

    authorize_relationship(
        source=Actor.JAYTEC,
        destination=Actor.MANUS,
        purpose=Purpose.TASK_PACKET,
        current_task_authorized=current_task_authorized,
    )

    return ManusDispatchAuthorization(
        project_id=project_id.strip(),
        project_name="MANUS",
        connectors=approved,
        profile=profile,
        authority=authority,
    )


def verify_manus_dispatch_result(
    authorization: ManusDispatchAuthorization,
    *,
    observed_profile: str | None,
    result: Mapping[str, object],
) -> None:
    """Accept Manus work only after profile and behavioural verification."""

    verify_manus_profile(authorization.profile, observed_profile=observed_profile)
    verify_manus_completion(result)
