"""Fail-closed policy for JAYTEC's paid Gemini reserve.

DeepSeek/reviewer is the normal research/review route. Gemini is never a
default route or a silent fallback. A Gemini TaskPacket is eligible only after
all suitable free routes are exhausted, the task specifically requires Gemini,
and paid-reserve authority is explicitly present in the packet context.

This module is dependency-free so both orchestration validation and the
provider adapter can enforce the same rule independently.
"""

from __future__ import annotations

from typing import Any, Mapping

GEMINI_SPECIALIST = "gemini"
GEMINI_DEFAULT_FREE_SPECIALIST = "reviewer"
GEMINI_PAID_RESERVE_WORKFLOW_PREFIX = "JAYTEC_PAID_GEMINI_RESERVE_"
GEMINI_PAID_RESERVE_COST_POLICY = "PAID_BACKUP_ONLY"

_REQUIRED_CONTEXT_FLAGS = (
    "free_routes_exhausted",
    "gemini_specifically_required",
    "paid_reserve_authorized",
)


def _nonempty_string(value: Any, *, max_len: int = 2000) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_len


def gemini_paid_reserve_errors(packet: Mapping[str, Any]) -> tuple[str, ...]:
    """Return deterministic policy errors for any packet that requests Gemini."""
    plan = packet.get("specialist_plan")
    if not isinstance(plan, list) or GEMINI_SPECIALIST not in plan:
        return ()

    errors: list[str] = []

    # Paid Gemini must be a deliberate terminal reserve step, never mixed into
    # ordinary fan-out where a free specialist is still being attempted.
    if plan != [GEMINI_SPECIALIST]:
        errors.append("gemini_paid_reserve_must_be_only_specialist")

    workflow_id = packet.get("workflow_id")
    if not (
        isinstance(workflow_id, str)
        and workflow_id.startswith(GEMINI_PAID_RESERVE_WORKFLOW_PREFIX)
    ):
        errors.append("gemini_paid_reserve_workflow_required")

    context = packet.get("required_context")
    if not isinstance(context, Mapping):
        errors.append("gemini_paid_reserve_context_required")
        return tuple(errors)

    for flag in _REQUIRED_CONTEXT_FLAGS:
        if context.get(flag) is not True:
            errors.append(f"gemini_paid_reserve_{flag}_required")

    if context.get("cost_policy") != GEMINI_PAID_RESERVE_COST_POLICY:
        errors.append("gemini_paid_reserve_cost_policy_required")
    if context.get("authority_controller") != "CHATGPT_OPENAI_LEAD":
        errors.append("gemini_paid_reserve_chatgpt_authority_required")
    if context.get("specialist_authority") != "SUBORDINATE":
        errors.append("gemini_paid_reserve_specialist_must_be_subordinate")

    attempted = context.get("free_routes_attempted")
    if (
        not isinstance(attempted, list)
        or not attempted
        or not all(_nonempty_string(item, max_len=128) for item in attempted)
    ):
        errors.append("gemini_paid_reserve_free_route_attempts_required")
    else:
        normalized = {str(item).strip().casefold() for item in attempted}
        if not ({"reviewer", "deepseek"} & normalized):
            errors.append("gemini_paid_reserve_deepseek_attempt_required")

    exhaustion_evidence = context.get("free_route_exhaustion_evidence")
    if (
        not isinstance(exhaustion_evidence, list)
        or not exhaustion_evidence
        or not all(_nonempty_string(item) for item in exhaustion_evidence)
    ):
        errors.append("gemini_paid_reserve_exhaustion_evidence_required")

    if not _nonempty_string(context.get("gemini_required_reason")):
        errors.append("gemini_paid_reserve_specific_reason_required")

    # Paid reserve calls are single-attempt and read/review-only at this layer.
    if packet.get("max_retries") != 0:
        errors.append("gemini_paid_reserve_retries_forbidden")
    if packet.get("side_effect_policy") != "none":
        errors.append("gemini_paid_reserve_side_effects_forbidden")

    return tuple(errors)


def require_gemini_paid_reserve(packet: Mapping[str, Any]) -> None:
    """Raise before provider invocation when Gemini reserve policy is not met."""
    errors = gemini_paid_reserve_errors(packet)
    if errors:
        raise RuntimeError(
            "GEMINI_PAID_RESERVE_POLICY_BLOCKED:" + ",".join(errors)
        )
