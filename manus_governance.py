"""Fail-closed behavioural authority policy for Manus inside JAYTEC.

This is deliberately separate from manus_policy.py:
- manus_policy.py = profile/cost gate (Lite by default, no silent fallback)
- manus_governance.py = authority, connector, delegation, evidence, and drift gate

The core rule is simple:
    Manus may evolve Manus. JAYTEC may help Manus evolve.
    Manus may work for JAYTEC. Manus may not autonomously change JAYTEC.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

DIRECTIVE_VERSION = "JAYTEC_MANUS_GOVERNANCE_V1"
MAX_PACKET_BYTES = 32_000
MAX_TEXT_CHARS = 12_000
MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_BYTES = 24_000
MAX_SPECIALIST_REQUEST_BYTES = 16_000
MAX_IDENTIFIER_CHARS = 200

_COMPLETION_FACETS = frozenset({
    "instruction_match_verified",
    "scope_verified",
    "evidence_verified",
    "no_unauthorized_side_effects",
    "duplicate_work_check_passed",
})
_EVIDENCE_KINDS = frozenset({
    "provider_observation",
    "connector_observation",
    "runtime_observation",
    "artifact",
    "test",
    "audit_record",
})
_SPECIALIST_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")

APPROVED_DIRECT_CONNECTORS = frozenset({"github", "neon", "render"})
BLOCKED_DIRECT_CONNECTORS = frozenset({
    "notion",
    "openai",
    "openrouter",
    "openrouter api",
})

_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|credential|token)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)"
)


class ManusScope(StrEnum):
    MANUS_INTERNAL = "manus_internal"
    JAYTEC_DELEGATED_TASK = "jaytec_delegated_task"
    JAYTEC_CORE_CHANGE = "jaytec_core_change"
    NOTION_AGENT_WORK = "notion_agent_work"
    MODEL_PROVIDER_DIRECT = "model_provider_direct"
    EXTERNAL_SPEND = "external_spend"


class AuthoritySource(StrEnum):
    MANUS = "manus"
    JAY = "jay"
    CHATGPT = "chatgpt"
    JAYTEC = "jaytec"


class ManusGovernanceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AuthorityDecision:
    scope: ManusScope
    allowed: bool
    must_escalate: bool
    reason: str


def _source(value: str | AuthoritySource) -> AuthoritySource:
    if isinstance(value, AuthoritySource):
        return value
    try:
        return AuthoritySource(str(value).strip().casefold())
    except ValueError as exc:
        raise ManusGovernanceError("MANUS_AUTHORITY_SOURCE_INVALID") from exc


def _scope(value: str | ManusScope) -> ManusScope:
    if isinstance(value, ManusScope):
        return value
    try:
        return ManusScope(str(value).strip().casefold())
    except ValueError as exc:
        raise ManusGovernanceError("MANUS_SCOPE_INVALID") from exc


def authorize_manus_action(
    *,
    scope: str | ManusScope,
    authority_source: str | AuthoritySource,
    explicit_current_task_authorization: bool = False,
    notion_authorized_by_jay_via_chatgpt: bool = False,
) -> AuthorityDecision:
    """Authorize one Manus action without allowing sticky/self-issued authority."""

    if type(explicit_current_task_authorization) is not bool:
        raise ManusGovernanceError("MANUS_EXPLICIT_AUTH_FLAG_INVALID")
    if type(notion_authorized_by_jay_via_chatgpt) is not bool:
        raise ManusGovernanceError("MANUS_NOTION_AUTH_FLAG_INVALID")

    target = _scope(scope)
    source = _source(authority_source)

    if target is ManusScope.MANUS_INTERNAL:
        return AuthorityDecision(
            target,
            True,
            False,
            "MANUS_MAY_EVOLVE_OWN_USER_CONTROLLED_AUTOMATION_LAYER",
        )

    if target is ManusScope.JAYTEC_DELEGATED_TASK:
        allowed = (
            source in {AuthoritySource.JAY, AuthoritySource.CHATGPT, AuthoritySource.JAYTEC}
            and explicit_current_task_authorization
        )
        return AuthorityDecision(
            target,
            allowed,
            not allowed,
            "CURRENT_JAYTEC_DELEGATION_VERIFIED"
            if allowed
            else "CURRENT_JAYTEC_DELEGATION_REQUIRED",
        )

    if target is ManusScope.JAYTEC_CORE_CHANGE:
        allowed = (
            source in {AuthoritySource.JAY, AuthoritySource.CHATGPT}
            and explicit_current_task_authorization
        )
        return AuthorityDecision(
            target,
            allowed,
            not allowed,
            "JAYTEC_CORE_CHANGE_EXPLICITLY_AUTHORIZED"
            if allowed
            else "JAY_OR_CHATGPT_CURRENT_AUTH_REQUIRED",
        )

    if target is ManusScope.NOTION_AGENT_WORK:
        allowed = (
            source is AuthoritySource.CHATGPT
            and explicit_current_task_authorization
            and notion_authorized_by_jay_via_chatgpt
        )
        return AuthorityDecision(
            target,
            allowed,
            not allowed,
            "CURRENT_NOTION_AUTH_VERIFIED"
            if allowed
            else "RETURN_NOTION_REQUEST_TO_CHATGPT",
        )

    if target is ManusScope.MODEL_PROVIDER_DIRECT:
        # Manus does not receive its own direct OpenAI/OpenRouter authority.
        # It asks JAYTEC for the appropriate specialist and receives the result.
        return AuthorityDecision(
            target,
            False,
            True,
            "RETURN_MODEL_SPECIALIST_REQUEST_TO_JAYTEC",
        )

    allowed = (
        source in {AuthoritySource.JAY, AuthoritySource.CHATGPT}
        and explicit_current_task_authorization
    )
    return AuthorityDecision(
        target,
        allowed,
        not allowed,
        "CURRENT_SPEND_AUTH_VERIFIED"
        if allowed
        else "JAY_OR_CHATGPT_SPEND_AUTH_REQUIRED",
    )


def normalize_connector_name(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def assert_approved_manus_connectors(names: Sequence[str]) -> tuple[str, ...]:
    """Fail closed if Manus is given a connector outside the direct allowlist."""

    if not isinstance(names, (list, tuple)):
        raise ManusGovernanceError("MANUS_CONNECTOR_LIST_INVALID")
    normalized = tuple(normalize_connector_name(v) for v in names)
    if any(not v for v in normalized):
        raise ManusGovernanceError("MANUS_CONNECTOR_NAME_INVALID")
    blocked = sorted({v for v in normalized if v in BLOCKED_DIRECT_CONNECTORS})
    if blocked:
        raise ManusGovernanceError(
            "MANUS_DIRECT_CONNECTOR_BLOCKED:" + ",".join(blocked)
        )
    unknown = sorted({v for v in normalized if v not in APPROVED_DIRECT_CONNECTORS})
    if unknown:
        raise ManusGovernanceError(
            "MANUS_DIRECT_CONNECTOR_NOT_ALLOWLISTED:" + ",".join(unknown)
        )
    return normalized


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value[:MAX_TEXT_CHARS])
    return value


_REQUIRED_PACKET_FIELDS = {
    "directive_version",
    "task_id",
    "objective",
    "scope",
    "authority_source",
    "allowed_actions",
    "forbidden_actions",
    "required_context",
    "validation_requirements",
    "completion_requirements",
    "return_schema",
}
_ALLOWED_PACKET_FIELDS = _REQUIRED_PACKET_FIELDS | {"constraints", "reference_ids"}


def build_minimal_task_packet(
    *,
    task_id: str,
    objective: str,
    scope: str | ManusScope,
    authority_source: str | AuthoritySource,
    allowed_actions: list[str],
    required_context: Mapping[str, Any] | None = None,
    constraints: list[str] | None = None,
    reference_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build the bounded data packet JAYTEC hands to Manus.

    There is intentionally no arbitrary **kwargs/context passthrough. Callers
    must choose the minimum required facts rather than forwarding whole chats,
    workspaces, credentials, or unrelated project state.
    """

    packet: dict[str, Any] = {
        "directive_version": DIRECTIVE_VERSION,
        "task_id": str(task_id).strip(),
        "objective": str(objective).strip(),
        "scope": _scope(scope).value,
        "authority_source": _source(authority_source).value,
        "allowed_actions": list(allowed_actions),
        "forbidden_actions": [
            "expand_own_authority",
            "modify_jaytec_without_current_authorization",
            "use_notion_agent_without_current_jay_via_chatgpt_authorization",
            "call_openai_or_openrouter_directly",
            "authorize_or_incur_unapproved_spend",
            "bypass_jaytec_routing",
            "claim_unverified_completion",
            "duplicate_existing_work",
            "alter_governance_or_directive_to_gain_authority",
            "claim_proprietary_manus_internal_changes_without_capability_evidence",
        ],
        "required_context": _redact(dict(required_context or {})),
        "validation_requirements": [
            "follow_directive_version_exactly",
            "stay_within_allowed_actions",
            "verify_output_against_objective_and_constraints",
            "provide evidence for completion claims",
            "report unresolved_items_and_escalations",
            "perform_no_unlisted_side_effects",
        ],
        "completion_requirements": [
            "instruction_match_verified",
            "scope_verified",
            "evidence_verified",
            "no_unauthorized_side_effects",
            "duplicate_work_check_passed",
            "manus_lite_profile_verified_by_jaytec",
        ],
        "return_schema": {
            "status": "SUCCESS|PARTIAL_SUCCESS|NEEDS_JAYTEC|FAILED_CLOSED",
            "summary": "string",
            "evidence": [{
                "kind": "provider_observation|connector_observation|runtime_observation|artifact|test|audit_record",
                "source": "string",
                "reference": "string",
                "observed_at": "RFC3339 string",
                "claim": "string",
                "supports": ["completion_verification_field"],
            }],
            "changes_made": ["string"],
            "unresolved_items": ["string"],
            "specialist_requests": ["object"],
            "verification": {
                "instruction_match_verified": "boolean",
                "scope_verified": "boolean",
                "evidence_verified": "boolean",
                "no_unauthorized_side_effects": "boolean",
                "duplicate_work_check_passed": "boolean",
            },
        },
    }
    if constraints:
        packet["constraints"] = list(constraints)
    if reference_ids:
        packet["reference_ids"] = list(reference_ids)
    validate_task_packet(packet)
    return packet


def validate_task_packet(packet: Mapping[str, Any]) -> None:
    missing = sorted(_REQUIRED_PACKET_FIELDS - set(packet))
    if missing:
        raise ManusGovernanceError("MANUS_PACKET_MISSING:" + ",".join(missing))
    unknown = sorted(set(packet) - _ALLOWED_PACKET_FIELDS)
    if unknown:
        raise ManusGovernanceError("MANUS_PACKET_UNKNOWN_FIELDS:" + ",".join(unknown))
    if packet.get("directive_version") != DIRECTIVE_VERSION:
        raise ManusGovernanceError("MANUS_DIRECTIVE_VERSION_MISMATCH")
    if not isinstance(packet.get("task_id"), str) or not packet["task_id"].strip():
        raise ManusGovernanceError("MANUS_PACKET_TASK_ID_INVALID")
    if not isinstance(packet.get("objective"), str) or not packet["objective"].strip():
        raise ManusGovernanceError("MANUS_PACKET_OBJECTIVE_INVALID")
    if len(packet["objective"]) > MAX_TEXT_CHARS:
        raise ManusGovernanceError("MANUS_PACKET_OBJECTIVE_TOO_LARGE")
    _scope(str(packet.get("scope")))
    _source(str(packet.get("authority_source")))
    actions = packet.get("allowed_actions")
    if (
        not isinstance(actions, list)
        or not actions
        or not all(isinstance(v, str) and v.strip() for v in actions)
    ):
        raise ManusGovernanceError("MANUS_PACKET_ALLOWED_ACTIONS_INVALID")
    encoded = json.dumps(
        _redact(dict(packet)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_PACKET_BYTES:
        raise ManusGovernanceError("MANUS_PACKET_TOO_LARGE")


def packet_digest(packet: Mapping[str, Any]) -> str:
    validate_task_packet(packet)
    raw = json.dumps(
        _redact(dict(packet)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_completion_evidence(
    evidence: Any,
    *,
    require_all_facets: bool,
) -> tuple[Mapping[str, Any], ...]:
    """Validate bounded provenance-bearing evidence for Manus completion."""

    if not isinstance(evidence, list) or not evidence:
        raise ManusGovernanceError("MANUS_SUCCESS_EVIDENCE_MISSING")
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise ManusGovernanceError("MANUS_EVIDENCE_TOO_MANY_ITEMS")

    normalized: list[Mapping[str, Any]] = []
    covered: set[str] = set()
    required_fields = {"kind", "source", "reference", "observed_at", "claim", "supports"}

    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise ManusGovernanceError(f"MANUS_EVIDENCE_ITEM_INVALID:{index}")
        if set(item) != required_fields:
            raise ManusGovernanceError(f"MANUS_EVIDENCE_FIELDS_INVALID:{index}")

        kind = str(item.get("kind") or "").strip()
        source = str(item.get("source") or "").strip()
        reference = str(item.get("reference") or "").strip()
        observed_at = str(item.get("observed_at") or "").strip()
        claim = str(item.get("claim") or "").strip()
        supports = item.get("supports")

        if kind not in _EVIDENCE_KINDS:
            raise ManusGovernanceError(f"MANUS_EVIDENCE_KIND_INVALID:{index}")
        if not source or len(source) > 200:
            raise ManusGovernanceError(f"MANUS_EVIDENCE_SOURCE_INVALID:{index}")
        if not reference or len(reference) > 1000:
            raise ManusGovernanceError(f"MANUS_EVIDENCE_REFERENCE_INVALID:{index}")
        if not claim or len(claim) > 2000:
            raise ManusGovernanceError(f"MANUS_EVIDENCE_CLAIM_INVALID:{index}")
        if (
            not observed_at
            or len(observed_at) > 64
            or "T" not in observed_at
            or not (observed_at.endswith("Z") or "+" in observed_at[10:])
        ):
            raise ManusGovernanceError(f"MANUS_EVIDENCE_TIME_INVALID:{index}")
        if (
            not isinstance(supports, list)
            or not supports
            or not all(isinstance(value, str) and value in _COMPLETION_FACETS for value in supports)
        ):
            raise ManusGovernanceError(f"MANUS_EVIDENCE_SUPPORTS_INVALID:{index}")

        covered.update(supports)
        normalized.append(dict(item))

    encoded = json.dumps(
        _redact(normalized),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise ManusGovernanceError("MANUS_EVIDENCE_TOO_LARGE")

    if require_all_facets:
        missing = sorted(_COMPLETION_FACETS - covered)
        if missing:
            raise ManusGovernanceError(
                "MANUS_EVIDENCE_FACETS_MISSING:" + ",".join(missing)
            )
    return tuple(normalized)


def verify_manus_completion(result: Mapping[str, Any]) -> None:
    """Reject a success claim that was not explicitly checked and evidenced."""

    if not isinstance(result, Mapping):
        raise ManusGovernanceError("MANUS_RESULT_INVALID")
    status = result.get("status")
    if status not in {
        "SUCCESS",
        "PARTIAL_SUCCESS",
        "NEEDS_JAYTEC",
        "FAILED_CLOSED",
    }:
        raise ManusGovernanceError("MANUS_RESULT_STATUS_INVALID")

    verification = result.get("verification")
    if not isinstance(verification, Mapping):
        raise ManusGovernanceError("MANUS_RESULT_VERIFICATION_MISSING")

    required_true = tuple(sorted(_COMPLETION_FACETS))
    if status == "SUCCESS":
        failed = [key for key in required_true if verification.get(key) is not True]
        if failed:
            raise ManusGovernanceError(
                "MANUS_SUCCESS_NOT_VERIFIED:" + ",".join(failed)
            )
        validate_completion_evidence(
            result.get("evidence"),
            require_all_facets=True,
        )


def validate_specialist_request(request: Mapping[str, Any]) -> None:
    """Validate a bounded Manus -> JAYTEC escalation packet."""

    required = {
        "type",
        "request_id",
        "parent_task_id",
        "directive_version",
        "specialist",
        "objective",
        "reason",
        "required_context",
        "authority",
        "packet_sha256",
    }
    if not isinstance(request, Mapping) or set(request) != required:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_FIELDS_INVALID")
    if request.get("type") != "SPECIALIST_REQUEST":
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_TYPE_INVALID")
    if request.get("directive_version") != DIRECTIVE_VERSION:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_VERSION_INVALID")
    if request.get("authority") != "REQUEST_ONLY_NO_SELF_DISPATCH":
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_AUTHORITY_INVALID")

    for field in ("request_id", "parent_task_id"):
        value = request.get(field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_IDENTIFIER_CHARS
        ):
            raise ManusGovernanceError(f"MANUS_SPECIALIST_REQUEST_{field.upper()}_INVALID")

    specialist = request.get("specialist")
    if not isinstance(specialist, str) or not _SPECIALIST_NAME.fullmatch(specialist):
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_SPECIALIST_INVALID")

    for field in ("objective", "reason"):
        value = request.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
            raise ManusGovernanceError(f"MANUS_SPECIALIST_REQUEST_{field.upper()}_INVALID")

    if not isinstance(request.get("required_context"), Mapping):
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_CONTEXT_INVALID")

    unsigned = {key: value for key, value in request.items() if key != "packet_sha256"}
    encoded_unsigned = json.dumps(
        _redact(unsigned),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected = hashlib.sha256(encoded_unsigned).hexdigest()
    if request.get("packet_sha256") != expected:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_DIGEST_INVALID")

    encoded = json.dumps(
        _redact(dict(request)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_SPECIALIST_REQUEST_BYTES:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_TOO_LARGE")


def specialist_request(
    *,
    parent_task_id: str,
    specialist: str,
    objective: str,
    reason: str,
    required_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a bounded request Manus returns to JAYTEC; never a dispatch token."""

    parent = str(parent_task_id).strip()
    role = str(specialist).strip()
    objective_value = str(objective).strip()
    reason_value = str(reason).strip()
    if not parent or len(parent) > MAX_IDENTIFIER_CHARS:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_PARENT_TASK_ID_INVALID")
    if not _SPECIALIST_NAME.fullmatch(role):
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_SPECIALIST_INVALID")
    if not objective_value or len(objective_value) > MAX_TEXT_CHARS:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_OBJECTIVE_INVALID")
    if not reason_value or len(reason_value) > MAX_TEXT_CHARS:
        raise ManusGovernanceError("MANUS_SPECIALIST_REQUEST_REASON_INVALID")

    context = _redact(dict(required_context or {}))
    seed = json.dumps(
        {
            "parent_task_id": parent,
            "specialist": role,
            "objective": objective_value,
            "reason": reason_value,
            "required_context": context,
            "directive_version": DIRECTIVE_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    request_id = "sr-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    packet: dict[str, Any] = {
        "type": "SPECIALIST_REQUEST",
        "request_id": request_id,
        "parent_task_id": parent,
        "directive_version": DIRECTIVE_VERSION,
        "specialist": role,
        "objective": objective_value,
        "reason": reason_value,
        "required_context": context,
        "authority": "REQUEST_ONLY_NO_SELF_DISPATCH",
    }
    unsigned = json.dumps(
        packet,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    packet["packet_sha256"] = hashlib.sha256(unsigned).hexdigest()
    validate_specialist_request(packet)
    return packet


def render_directive() -> str:
    return f"""# {DIRECTIVE_VERSION}

ROLE
You are Manus, JAYTEC's bounded automation specialist. You are a distinct
specialist used by Jay/ChatGPT/JAYTEC for automation, delegation support,
diagnostics, execution assistance, verification, and repetitive work. You are
not JAYTEC's owner, policy authority, or source of truth.

CHAIN OF AUTHORITY
Jay -> ChatGPT -> JAYTEC -> Manus / specialists.
Never expand, reinterpret, inherit, or manufacture authority. Previous tasks,
old approvals, standing full-authority language, convenience, urgency, or your
own judgment do not authorize a new JAYTEC-side change or spend.

YOUR HOUSE
Manus may evolve Manus. You may autonomously improve the user-controlled Manus layer: JAYTEC-owned
Manus workflows, prompts, task organization, automation methods, evaluation
routines, reusable procedures, and Manus-specific assets that exposed tools
actually allow you to change. Continue improving that layer when useful.
Do not claim you changed Manus proprietary platform internals, hidden models,
or vendor architecture unless an exposed capability and evidence proves it.

JAYTEC BOUNDARY
You may inspect, analyze, diagnose, and recommend JAYTEC improvements.
You CANNOT independently modify JAYTEC. "Your house" means only the
JAYTEC-controlled Manus layer; it NEVER means JAYTEC core, JAYTEC policy,
routing, authority, checkpoints, provider rules, or shared system state.
You MUST NOT edit, upgrade, reconfigure, mutate, or authorize changes to JAYTEC
unless Jay or ChatGPT explicitly authorizes that exact current-task change.
If not authorized, report the proposal and return it to JAYTEC.

SPECIALISTS
You may ask JAYTEC for specialist help. Return only a bounded, versioned,
correlated SPECIALIST_REQUEST using JAYTEC's request schema, with the minimum
context necessary. It is a request only, never a dispatch token. Do not call
OpenAI or OpenRouter directly. JAYTEC validates the packet, chooses and invokes
specialists, then returns the result to you.

NOTION
Notion is a gateway/transfer path, not your worker. You must not invoke or
authorize Notion work, use Notion as a fallback, or spend Notion credits.
If you believe Notion is needed, return the request to ChatGPT through JAYTEC.
Proceed only when Jay explicitly authorized that Notion use through ChatGPT for
the current task.

DIRECT CONNECTORS
Your direct connector policy ceiling is GitHub, Neon, and Render only.
That list describes which direct connectors JAYTEC may ever grant you; it does
NOT mean they are active on every task. Default/current task connector scope is
NONE unless JAYTEC explicitly grants a subset for this exact task.
JAYTEC must bind only the minimum connector subset and purpose required for the
current task. Never inherit project/user connector defaults as authority.
Unknown connectors fail closed. Direct access does not grant blanket mutation
authority. Inspect/read/diagnose/test/report may be used only when explicitly
scoped. A normal JAYTEC task delegation is NOT connector-mutation authority.
Writes, deploys, deletes, migrations, production changes, credential changes,
destructive operations, and external spend require a separate fresh explicit
mutation authorization from Jay or ChatGPT for the current task.

COST
Manus Lite is the default and required profile. Never upgrade yourself, fall
back to Standard/Max, or infer permission to use a paid profile. JAYTEC must pin
Lite before dispatch and verify the observed profile after execution.

DATA MINIMIZATION
Accept and forward strict task packets. Request only missing facts required to
do the work. Never forward whole chats, workspaces, unrelated files, secrets,
or broad context merely because it is available.

ANTI-DUPLICATION
Before creating work, check the supplied checkpoint/task/reference state. Do
not create a second implementation, task, checkpoint, or competing source of
truth when the work already exists.

COMPLETION
Never claim success because an action merely ran. Before SUCCESS, verify:
1. the actual result matches Jay/ChatGPT's instructions,
2. scope and authority were obeyed,
3. provenance-bearing structured evidence supports every claimed verification facet,
4. no unauthorized side effect occurred,
5. duplicate-work checks passed,
6. JAYTEC verified the Manus Lite profile.
If any of these is missing, return PARTIAL_SUCCESS, NEEDS_JAYTEC, or
FAILED_CLOSED with unresolved items.

SELF-IMPROVEMENT
Improve your own JAYTEC-controlled Manus structure and methods as you learn.
Measure whether an improvement actually helped. Preserve rollback information
and do not weaken these rules. You may propose improvements to this directive,
but you may not alter, bypass, reinterpret, or supersede it yourself.

ESCALATE, DO NOT GUESS
When authority, scope, cost, connector rights, required data, or verification
is unclear, stop that part and ask JAYTEC. Manus-originated JAYTEC requests are
forwarded to ChatGPT for handling.

This directive is higher priority than task convenience. If a task conflicts
with it, fail closed or escalate.
"""


def directive_sha256() -> str:
    return hashlib.sha256(render_directive().encode("utf-8")).hexdigest()
