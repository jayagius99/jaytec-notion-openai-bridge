"""JAYTEC-owned Manus Lite runtime surface.

This module is the only supported direct JAYTEC -> Manus execution wrapper.
It deliberately exposes NO profile selector: every dispatch is hard-pinned to
Lite before the first Manus network call and re-verified from provider-observed
task metadata.

The runtime is asynchronous:
- start_task() creates one bounded private task and returns a task id;
- task_status() performs read-only status/result verification.

It does not bypass the production V2 dispatch-authority boundary; server.py
must apply that boundary before invoking this module.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from manus_adapter import (
    MANUS_MAX_MESSAGE_CHARS,
    ManusClient,
    ManusError,
    ManusInsufficientCredits,
    safe_task_summary,
)
from manus_governance import (
    ManusGovernanceError,
    build_minimal_task_packet,
    packet_digest,
    validate_specialist_request,
    verify_manus_completion,
)
from manus_policy import (
    ManusProfilePolicyError,
    authorize_manus_route,
    verify_manus_profile,
)

SCHEMA_VERSION = "JAYTEC_MANUS_LITE_RUNTIME_V1"
MAX_REQUEST_BYTES = 32_000
MAX_STATUS_TASK_ID = 200

# Deliberately excludes profile/override fields. Unknown fields fail closed.
_ALLOWED_START_FIELDS = frozenset({
    "task_id",
    "objective",
    "scope",
    "authority_source",
    "current_task_authorized",
    "allowed_actions",
    "connector_purposes",
    "connector_mutation_authorized",
    "required_context",
    "constraints",
    "reference_ids",
    "title",
})

_FORBIDDEN_PROFILE_FIELDS = frozenset({
    "profile",
    "requested_profile",
    "agent_profile",
    "paid_profile",
    "explicit_paid_override",
    "paid_override_authority",
    "model",
})

MANUS_RESULT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["SUCCESS", "PARTIAL_SUCCESS", "NEEDS_JAYTEC", "NEEDS_OWNER", "FAILED_CLOSED"],
        },
        "summary": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "source": {"type": "string"},
                    "reference": {"type": "string"},
                    "observed_at": {"type": "string"},
                    "claim": {"type": "string"},
                    "supports": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "kind",
                    "source",
                    "reference",
                    "observed_at",
                    "claim",
                    "supports",
                ],
                "additionalProperties": False,
            },
        },
        "changes_made": {"type": "array", "items": {"type": "string"}},
        "unresolved_items": {"type": "array", "items": {"type": "string"}},
        "specialist_requests": {
            "type": "array",
            "items": {
                "type": "string",
                "description": (
                    "Optional canonical JSON SPECIALIST_REQUEST packets. "
                    "Each string is parsed and validated by JAYTEC before use."
                ),
            },
        },
        "verification": {
            "type": "object",
            "properties": {
                "instruction_match_verified": {"type": "boolean"},
                "scope_verified": {"type": "boolean"},
                "evidence_verified": {"type": "boolean"},
                "no_unauthorized_side_effects": {"type": "boolean"},
                "duplicate_work_check_passed": {"type": "boolean"},
            },
            "required": [
                "instruction_match_verified",
                "scope_verified",
                "evidence_verified",
                "no_unauthorized_side_effects",
                "duplicate_work_check_passed",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "status",
        "summary",
        "evidence",
        "changes_made",
        "unresolved_items",
        "specialist_requests",
        "verification",
    ],
    "additionalProperties": False,
}


class ManusRuntimeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_MANUS_SCHEMA_ALLOWED_BY_TYPE = {
    "object": frozenset({"type", "properties", "required", "additionalProperties", "description"}),
    "array": frozenset({"type", "items", "description"}),
    "string": frozenset({"type", "enum", "description"}),
    "number": frozenset({"type", "enum", "description"}),
    "integer": frozenset({"type", "enum", "description"}),
    "boolean": frozenset({"type", "description"}),
    "null": frozenset({"type", "description"}),
}


def validate_manus_structured_output_schema(
    schema: Mapping[str, Any],
    *,
    _path: str = "$",
    _root: bool = True,
) -> None:
    """Fail closed against Manus v2's strict structured-output subset.

    The provider rejects invalid schemas as HTTP 400 invalid_argument. Validate
    locally before any Manus network access so recovery attempts are not burned
    on deterministic contract errors.
    """

    if not isinstance(schema, Mapping):
        raise ManusRuntimeError("MANUS_STRUCTURED_SCHEMA_NODE_INVALID:" + _path)

    node_type = schema.get("type")
    if not isinstance(node_type, str) or node_type not in _MANUS_SCHEMA_ALLOWED_BY_TYPE:
        raise ManusRuntimeError("MANUS_STRUCTURED_SCHEMA_TYPE_INVALID:" + _path)
    if _root and node_type != "object":
        raise ManusRuntimeError("MANUS_STRUCTURED_SCHEMA_ROOT_NOT_OBJECT")

    unknown = sorted(set(schema) - _MANUS_SCHEMA_ALLOWED_BY_TYPE[node_type])
    if unknown:
        raise ManusRuntimeError(
            "MANUS_STRUCTURED_SCHEMA_UNSUPPORTED_KEYWORD:"
            + _path
            + ":"
            + ",".join(unknown)
        )

    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise ManusRuntimeError("MANUS_STRUCTURED_SCHEMA_ENUM_INVALID:" + _path)

    if node_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping):
            raise ManusRuntimeError(
                "MANUS_STRUCTURED_SCHEMA_OBJECT_PROPERTIES_INVALID:" + _path
            )
        if schema.get("additionalProperties") is not False:
            raise ManusRuntimeError(
                "MANUS_STRUCTURED_SCHEMA_OBJECT_ADDITIONAL_PROPERTIES_REQUIRED:"
                + _path
            )
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or set(required) != set(properties)
            or len(required) != len(set(required))
        ):
            raise ManusRuntimeError(
                "MANUS_STRUCTURED_SCHEMA_OBJECT_REQUIRED_MISMATCH:" + _path
            )
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ManusRuntimeError(
                    "MANUS_STRUCTURED_SCHEMA_PROPERTY_NAME_INVALID:" + _path
                )
            validate_manus_structured_output_schema(
                child,
                _path=_path + "." + name,
                _root=False,
            )
    elif node_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise ManusRuntimeError("MANUS_STRUCTURED_SCHEMA_ARRAY_ITEMS_INVALID:" + _path)
        validate_manus_structured_output_schema(
            items,
            _path=_path + "[]",
            _root=False,
        )


def _decode_specialist_request(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ManusRuntimeError("MANUS_RUNTIME_SPECIALIST_REQUEST_INVALID")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ManusRuntimeError(
            "MANUS_RUNTIME_SPECIALIST_REQUEST_JSON_INVALID"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise ManusRuntimeError("MANUS_RUNTIME_SPECIALIST_REQUEST_INVALID")
    return decoded


@dataclass(frozen=True)
class StartRequest:
    task_id: str
    objective: str
    scope: str
    authority_source: str
    current_task_authorized: bool
    allowed_actions: tuple[str, ...]
    connector_purposes: Mapping[str, str]
    connector_mutation_authorized: bool
    required_context: Mapping[str, Any]
    constraints: tuple[str, ...]
    reference_ids: tuple[str, ...]
    title: str


def _json_object(raw: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise ManusRuntimeError("MANUS_RUNTIME_REQUEST_NOT_STRING")
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ManusRuntimeError("MANUS_RUNTIME_REQUEST_TOO_LARGE")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManusRuntimeError("MANUS_RUNTIME_REQUEST_INVALID_JSON") from exc
    if not isinstance(value, Mapping):
        raise ManusRuntimeError("MANUS_RUNTIME_REQUEST_NOT_OBJECT")
    return value


def _strings(value: Any, *, field: str, allow_empty: bool = True) -> tuple[str, ...]:
    if value is None and allow_empty:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ManusRuntimeError(f"MANUS_RUNTIME_{field.upper()}_INVALID")
    return tuple(v.strip() for v in value)


def parse_start_request(raw: str) -> StartRequest:
    value = _json_object(raw)

    forbidden = sorted(set(value) & _FORBIDDEN_PROFILE_FIELDS)
    if forbidden:
        raise ManusRuntimeError(
            "MANUS_RUNTIME_PROFILE_SELECTION_FORBIDDEN:" + ",".join(forbidden)
        )

    unknown = sorted(set(value) - _ALLOWED_START_FIELDS)
    if unknown:
        raise ManusRuntimeError("MANUS_RUNTIME_UNKNOWN_FIELDS:" + ",".join(unknown))

    required = {
        "task_id",
        "objective",
        "scope",
        "authority_source",
        "current_task_authorized",
        "allowed_actions",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ManusRuntimeError("MANUS_RUNTIME_MISSING:" + ",".join(missing))

    task_id = str(value.get("task_id") or "").strip()
    objective = str(value.get("objective") or "").strip()
    scope = str(value.get("scope") or "").strip()
    authority_source = str(value.get("authority_source") or "").strip()
    if not task_id or len(task_id) > 200:
        raise ManusRuntimeError("MANUS_RUNTIME_TASK_ID_INVALID")
    if not objective:
        raise ManusRuntimeError("MANUS_RUNTIME_OBJECTIVE_INVALID")
    if type(value.get("current_task_authorized")) is not bool:
        raise ManusRuntimeError("MANUS_RUNTIME_CURRENT_AUTH_INVALID")
    if value.get("current_task_authorized") is not True:
        raise ManusRuntimeError("MANUS_RUNTIME_CURRENT_AUTH_REQUIRED")

    allowed_actions = _strings(
        value.get("allowed_actions"),
        field="allowed_actions",
        allow_empty=False,
    )

    connector_purposes = value.get("connector_purposes", {})
    if not isinstance(connector_purposes, Mapping):
        raise ManusRuntimeError("MANUS_RUNTIME_CONNECTOR_PURPOSES_INVALID")
    normalized_connectors: dict[str, str] = {}
    for key, purpose in connector_purposes.items():
        if not isinstance(key, str) or not key.strip():
            raise ManusRuntimeError("MANUS_RUNTIME_CONNECTOR_NAME_INVALID")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ManusRuntimeError("MANUS_RUNTIME_CONNECTOR_PURPOSE_INVALID")
        normalized_connectors[key.strip()] = purpose.strip()

    mutation_authorized = value.get("connector_mutation_authorized", False)
    if type(mutation_authorized) is not bool:
        raise ManusRuntimeError("MANUS_RUNTIME_CONNECTOR_MUTATION_AUTH_INVALID")

    required_context = value.get("required_context", {})
    if not isinstance(required_context, Mapping):
        raise ManusRuntimeError("MANUS_RUNTIME_CONTEXT_INVALID")

    title = str(value.get("title") or f"JAYTEC Manus task {task_id}").strip()
    if not title:
        raise ManusRuntimeError("MANUS_RUNTIME_TITLE_INVALID")

    return StartRequest(
        task_id=task_id,
        objective=objective,
        scope=scope,
        authority_source=authority_source,
        current_task_authorized=True,
        allowed_actions=allowed_actions,
        connector_purposes=normalized_connectors,
        connector_mutation_authorized=mutation_authorized,
        required_context=dict(required_context),
        constraints=_strings(value.get("constraints", []), field="constraints"),
        reference_ids=_strings(value.get("reference_ids", []), field="reference_ids"),
        title=title[:200],
    )


def start_request_identity(raw: str) -> tuple[str, str]:
    """Return stable idempotency key/digest without touching Manus."""
    req = parse_start_request(raw)
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "task_id": req.task_id,
        "objective": req.objective,
        "scope": req.scope,
        "authority_source": req.authority_source,
        "current_task_authorized": req.current_task_authorized,
        "allowed_actions": list(req.allowed_actions),
        "connector_purposes": dict(sorted(req.connector_purposes.items())),
        "connector_mutation_authorized": req.connector_mutation_authorized,
        "required_context": dict(req.required_context),
        "constraints": list(req.constraints),
        "reference_ids": list(req.reference_ids),
        "title": req.title,
        "profile": "lite",
    }
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (
        "manus:" + req.task_id,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _task(body: Mapping[str, Any]) -> Mapping[str, Any]:
    value = body.get("task")
    if isinstance(value, Mapping):
        return value
    value = body.get("data")
    return value if isinstance(value, Mapping) else {}


def _message_rows(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = body.get("messages")
    if not isinstance(value, list):
        value = body.get("data")
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _latest_structured_value(body: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for row in _message_rows(body):
        if str(row.get("type") or "") != "structured_output_result":
            continue
        result = row.get("structured_output_result")
        if not isinstance(result, Mapping) or result.get("success") is not True:
            continue
        value = result.get("value")
        if isinstance(value, Mapping):
            return value
    return None


def _prompt(packet: Mapping[str, Any]) -> str:
    return (
        "JAYTEC MANUS TASK PACKET\n"
        "Execute only this bounded packet. Do not expand scope or authority. "
        "Return the required structured result only. If JAYTEC can resolve the "
        "blocker, return NEEDS_JAYTEC. Use NEEDS_OWNER only for a genuine owner, "
        "physical, credential, spend, or irreversible-decision boundary. Use "
        "FAILED_CLOSED for bounded execution failure rather than guessing.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


_WATCH_RECOVERY_CONTEXT_KEYS = frozenset({
    "parent_task_id",
    "checkpoint_number",
    "repo",
    "branch",
    "verified_head",
    "open_pr",
    "current_phase",
    "last_safe_checkpoint",
    "next_intended_action",
    "completed_work_count",
    "remaining_work_count",
    "known_failures_count",
    "dependencies_count",
    "continuation_packet_sha256",
    "canonical_objective_sha256",
    "canonical_objective_excerpt",
    "active_constraints_sha256",
    "fencing_token",
    "recovery_route",
    "jaytec_private_github_broker",
})


def _fits_manus_raw_message(content: str) -> bool:
    """Match the adapter's ceiling, with a UTF-8 byte guard for non-ASCII text."""
    return (
        len(content) <= MANUS_MAX_MESSAGE_CHARS
        and len(content.encode("utf-8")) <= MANUS_MAX_MESSAGE_CHARS
    )


def _compact_watch_recovery_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Compact descriptive WATCH recovery state without weakening authority.

    Only Forge WATCH recovery packets are eligible. Allowed/forbidden actions,
    constraints, validation/completion requirements and the return schema remain
    byte-for-byte equivalent. Omitted descriptive checkpoint text remains in the
    durable JAYTEC assignment and is represented by cryptographic digests.
    """

    task_id = str(packet.get("task_id") or "")
    context = packet.get("required_context")
    if ":recovery:" not in task_id or not isinstance(context, Mapping):
        return dict(packet)
    if str(context.get("parent_task_id") or "") != "FORGE-GENESIS-ACTIVATION-001":
        return dict(packet)

    canonical = json.dumps(
        dict(packet),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    objective = str(packet.get("objective") or "")
    context_canonical = json.dumps(
        dict(context),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")

    compact_context = {
        key: context[key]
        for key in _WATCH_RECOVERY_CONTEXT_KEYS
        if key in context
    }
    for key, limit in (
        ("current_phase", 220),
        ("last_safe_checkpoint", 320),
        ("next_intended_action", 320),
    ):
        if key in compact_context:
            compact_context[key] = str(compact_context[key])[:limit]

    compact_context.setdefault("canonical_objective_excerpt", objective[:420])
    compact_context["jaytec_compaction"] = {
        "schema_version": "JAYTEC_WATCH_RECOVERY_COMPACTION_V1",
        "source_packet_sha256": hashlib.sha256(canonical).hexdigest(),
        "source_context_sha256": hashlib.sha256(context_canonical).hexdigest(),
        "policy": "DESCRIPTIVE_TEXT_ONLY_AUTHORITY_AND_CONSTRAINTS_PRESERVED",
        "omitted_detail_action": "RETURN_NEEDS_JAYTEC",
    }

    compact = dict(packet)
    compact["objective"] = (
        "Continue FORGE-GENESIS-ACTIVATION-001 from the exact durable JAYTEC "
        "checkpoint identified in required_context. Preserve completed work and "
        "work only within the supplied allowed_actions, constraints, fencing "
        "token, current phase and next intended action. Do not infer omitted "
        "checkpoint prose; return NEEDS_JAYTEC if exact omitted detail is required."
    )
    compact["required_context"] = compact_context

    # The provider already enforces MANUS_RESULT_JSON_SCHEMA out-of-band on
    # create_task(). Repeating the full human-readable schema inside the prompt
    # wastes ~1 KiB of the 6 KiB provider message budget and caused live WATCH
    # recovery to fail closed even after descriptive checkpoint compaction.
    # Keep an explicit marker/status contract in-band; the exact shape remains
    # enforced by the structured_output_schema argument at the provider boundary.
    compact["return_schema"] = {
        "provider_enforced": "MANUS_RESULT_JSON_SCHEMA",
        "status": (
            "SUCCESS|PARTIAL_SUCCESS|NEEDS_JAYTEC|NEEDS_OWNER|FAILED_CLOSED"
        ),
        "instruction": "Return only the provider-enforced structured result.",
    }
    return compact


class ManusLiteRuntime:
    def __init__(self, client: ManusClient):
        self.client = client

    def start_task(self, request_json: str) -> dict[str, Any]:
        req = parse_start_request(request_json)
        validate_manus_structured_output_schema(MANUS_RESULT_JSON_SCHEMA)

        packet = build_minimal_task_packet(
            task_id=req.task_id,
            objective=req.objective,
            scope=req.scope,
            authority_source=req.authority_source,
            allowed_actions=list(req.allowed_actions),
            required_context=req.required_context,
            constraints=list(req.constraints),
            reference_ids=list(req.reference_ids),
        )

        # Preflight the exact raw message before any provider-backed route lookup.
        # WATCH recovery may compact descriptive checkpoint prose, but it may
        # never remove authority, constraints, validation gates or forbidden actions.
        prompt = _prompt(packet)
        if not _fits_manus_raw_message(prompt):
            packet = _compact_watch_recovery_packet(packet)
            prompt = _prompt(packet)
        if not _fits_manus_raw_message(prompt):
            raise ManusRuntimeError("MANUS_RUNTIME_START_MESSAGE_TOO_LARGE")

        # There is intentionally no caller-controlled profile input here.
        route = self.client.prepare_route(
            scope=req.scope,
            authority_source=req.authority_source,
            current_task_authorized=req.current_task_authorized,
            requested_profile="lite",
            requested_connector_purposes=req.connector_purposes,
            connector_mutation_authorized=req.connector_mutation_authorized,
        )

        created = self.client.create_task(
            route,
            prompt,
            title=req.title,
            structured_output_schema=MANUS_RESULT_JSON_SCHEMA,
        )
        provider_task_id = str(created.get("task_id") or "").strip()
        if not provider_task_id:
            raise ManusRuntimeError("MANUS_RUNTIME_PROVIDER_TASK_ID_MISSING")

        # create_task already verifies the provider-observed profile before
        # returning. Report only safe non-secret routing metadata.
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "STARTED",
            "task_id": req.task_id,
            "provider_task_id": provider_task_id,
            "requested_profile": "lite",
            "observed_profile_verified": True,
            "project_id": route.authorization.project_id,
            "connectors": list(route.authorization.connectors),
            "connector_permissions": [list(v) for v in route.connector_permissions],
            "packet_sha256": packet_digest(packet),
        }

    def start_task_idempotent(
        self,
        request_json: str,
        registry: Any,
    ) -> dict[str, Any]:
        """Start exactly once per normalized task id/request within registry TTL."""
        key, digest = start_request_identity(request_json)
        try:
            existing = registry.lookup(key, digest)
        except Exception as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise ManusRuntimeError("MANUS_RUNTIME_CONFLICTING_DUPLICATE") from exc
            raise

        if isinstance(existing, Mapping):
            replay = dict(existing)
            replay["idempotent_replay"] = True
            return replay

        starting = {
            "schema_version": SCHEMA_VERSION,
            "status": "STARTING",
            "task_id": key.split(":", 1)[1],
            "requested_profile": "lite",
        }
        try:
            claimed = registry.claim_once(key, digest, starting)
        except Exception as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise ManusRuntimeError("MANUS_RUNTIME_CONFLICTING_DUPLICATE") from exc
            raise

        if not claimed:
            try:
                raced = registry.lookup(key, digest)
            except Exception as exc:
                if str(exc) == "CONFLICTING_DUPLICATE":
                    raise ManusRuntimeError("MANUS_RUNTIME_CONFLICTING_DUPLICATE") from exc
                raise
            if not isinstance(raced, Mapping):
                raise ManusRuntimeError("MANUS_RUNTIME_IDEMPOTENCY_RACE_UNRESOLVED")
            replay = dict(raced)
            replay["idempotent_replay"] = True
            return replay

        try:
            result = self.start_task(request_json)
        except Exception as exc:
            result = runtime_error_payload(exc)

        try:
            registry.store(key, digest, result)
        except Exception as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise ManusRuntimeError("MANUS_RUNTIME_CONFLICTING_DUPLICATE") from exc
            raise
        return dict(result)

    def continue_task_handoff(
        self,
        provider_task_id: str,
        *,
        scope: str,
        authority_source: str,
        current_task_authorized: bool,
        connector_purposes: Mapping[str, str],
        connector_mutation_authorized: bool,
        handoff_id: str,
        handoff_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resume one existing Lite task with a bounded JAYTEC handoff.

        This is not a recovery-attempt allocator. The caller must enforce the
        durable fencing token before/after this provider call. The handoff
        contains no provider credential and cannot expand connector authority.
        """

        task_id = str(provider_task_id or "").strip()
        if not task_id or len(task_id) > MAX_STATUS_TASK_ID:
            raise ManusRuntimeError("MANUS_RUNTIME_PROVIDER_TASK_ID_INVALID")
        if type(current_task_authorized) is not bool or current_task_authorized is not True:
            raise ManusRuntimeError("MANUS_RUNTIME_CURRENT_AUTH_REQUIRED")
        hid = str(handoff_id or "").strip()
        if not hid or len(hid) > 200:
            raise ManusRuntimeError("MANUS_RUNTIME_HANDOFF_ID_INVALID")
        if not isinstance(connector_purposes, Mapping):
            raise ManusRuntimeError("MANUS_RUNTIME_CONNECTOR_PURPOSES_INVALID")
        if type(connector_mutation_authorized) is not bool:
            raise ManusRuntimeError("MANUS_RUNTIME_CONNECTOR_MUTATION_AUTH_INVALID")
        if not isinstance(handoff_context, Mapping):
            raise ManusRuntimeError("MANUS_RUNTIME_HANDOFF_CONTEXT_INVALID")

        context_json = json.dumps(
            dict(handoff_context),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(context_json.encode("utf-8")) > 4500:
            raise ManusRuntimeError("MANUS_RUNTIME_HANDOFF_CONTEXT_TOO_LARGE")

        validate_manus_structured_output_schema(MANUS_RESULT_JSON_SCHEMA)

        # Keep the same-task handoff deliberately terse. The signed/validated
        # context carries the evidence; repeated protocol prose must not crowd
        # out that evidence or trigger the Manus message ceiling.
        content = (
            "JAYTEC INTERNAL HANDOFF\n"
            "handoff_id=" + hid + "\n"
            "Same bounded task, same authority, Lite only. Context does not "
            "expand authority. Use only the connector scope supplied by JAYTEC. "
            "For additional private GitHub evidence/operations return NEEDS_JAYTEC "
            "with at most TWO github_broker specialist_requests. Never merge, "
            "delete, force-push, access secrets/credentials, change visibility, "
            "spend, escalate ROOT_OWNER, or activate Forge.\n"
            "JAYTEC_BROKER_CONTEXT=" + context_json
        )
        if not _fits_manus_raw_message(content):
            raise ManusRuntimeError("MANUS_RUNTIME_HANDOFF_MESSAGE_TOO_LARGE")

        # Handoff idempotency: if a prior send reached Manus but the caller
        # lost the response, detect the deterministic handoff id in task
        # messages and treat it as already delivered instead of duplicating it.
        try:
            existing_messages = self.client.list_messages(task_id, limit=100)
            existing_text = json.dumps(
                existing_messages,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            existing_text = ""
        if hid in existing_text:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "CONTINUED",
                "provider_task_id": task_id,
                "handoff_id": hid,
                "requested_profile": "lite",
                "observed_profile_verified": True,
                "idempotent_replay": True,
            }

        route = self.client.prepare_route(
            scope=scope,
            authority_source=authority_source,
            current_task_authorized=True,
            requested_profile="lite",
            requested_connector_purposes=dict(connector_purposes),
            connector_mutation_authorized=connector_mutation_authorized,
        )

        self.client.send_message(
            route,
            task_id,
            content,
            structured_output_schema=MANUS_RESULT_JSON_SCHEMA,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "CONTINUED",
            "provider_task_id": task_id,
            "handoff_id": hid,
            "requested_profile": "lite",
            "observed_profile_verified": True,
            "connectors": list(route.authorization.connectors),
            "connector_permissions": [list(v) for v in route.connector_permissions],
        }

    def task_status_readonly(self, provider_task_id: str) -> dict[str, Any]:
        """Read and verify one Manus task without mutating provider state.

        This diagnostic path never calls stop_task. Any profile/project/result
        mismatch returns FAILED_CLOSED so inspection cannot become authority.
        """

        task_id = str(provider_task_id or "").strip()
        if not task_id or len(task_id) > MAX_STATUS_TASK_ID:
            raise ManusRuntimeError("MANUS_RUNTIME_PROVIDER_TASK_ID_INVALID")

        lite = authorize_manus_route(
            requested_profile="lite",
            route_supports_profile_selector=True,
        )

        detail = self.client.task_detail(task_id)
        task = _task(detail)
        observed = task.get("agent_profile")
        try:
            verify_manus_profile(
                lite,
                observed_profile=str(observed) if observed not in (None, "") else None,
            )
        except ManusProfilePolicyError:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED_CLOSED",
                "provider_task_id": task_id,
                "reason": "MANUS_RUNTIME_PROFILE_MISMATCH",
            }

        try:
            project_id, _project_name = self.client.resolve_manus_project()
        except Exception as exc:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED_CLOSED",
                "provider_task_id": task_id,
                "reason": "MANUS_RUNTIME_PROJECT_LOOKUP_FAILED:" + type(exc).__name__,
            }

        observed_project = task.get("project_id")
        if observed_project not in (None, "") and str(observed_project) != project_id:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED_CLOSED",
                "provider_task_id": task_id,
                "reason": "MANUS_RUNTIME_PROJECT_MISMATCH",
            }

        provider_status = str(task.get("status") or "").strip().casefold()
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "PENDING",
            "provider_task_id": task_id,
            "requested_profile": "lite",
            "observed_profile": "lite",
            "task": safe_task_summary(detail),
            "read_only": True,
        }

        if provider_status in {"error", "failed", "cancelled", "canceled"}:
            out["status"] = "FAILED_CLOSED"
            out["reason"] = "MANUS_PROVIDER_TASK_FAILED"
            return out
        if provider_status not in {"stopped", "completed", "success", "succeeded"}:
            return out

        messages = self.client.list_messages(task_id, limit=100)
        result = _latest_structured_value(messages)
        if result is None:
            out["status"] = "FAILED_CLOSED"
            out["reason"] = "MANUS_STRUCTURED_RESULT_MISSING"
            return out

        requests = result.get("specialist_requests")
        if isinstance(requests, list):
            for request in requests:
                validate_specialist_request(_decode_specialist_request(request))

        verify_manus_completion(result)
        out["status"] = "VERIFIED_COMPLETE"
        out["result"] = dict(result)
        return out

    def task_status(self, provider_task_id: str) -> dict[str, Any]:
        task_id = str(provider_task_id or "").strip()
        if not task_id or len(task_id) > MAX_STATUS_TASK_ID:
            raise ManusRuntimeError("MANUS_RUNTIME_PROVIDER_TASK_ID_INVALID")

        # Build a Lite-only decision locally before reading Manus. This contains
        # no paid-profile override path.
        lite = authorize_manus_route(
            requested_profile="lite",
            route_supports_profile_selector=True,
        )

        detail = self.client.task_detail(task_id)
        task = _task(detail)
        observed = task.get("agent_profile")
        try:
            verify_manus_profile(
                lite,
                observed_profile=str(observed) if observed not in (None, "") else None,
            )
        except ManusProfilePolicyError:
            try:
                self.client.stop_task(task_id)
            except Exception:
                pass
            raise

        project_id, _project_name = self.client.resolve_manus_project()
        observed_project = task.get("project_id")
        if observed_project not in (None, "") and str(observed_project) != project_id:
            try:
                self.client.stop_task(task_id)
            except Exception:
                pass
            raise ManusRuntimeError("MANUS_RUNTIME_PROJECT_MISMATCH")

        status = str(task.get("status") or "").strip().casefold()
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "PENDING",
            "provider_task_id": task_id,
            "requested_profile": "lite",
            "observed_profile": "lite",
            "task": safe_task_summary(detail),
        }

        if status in {"error", "failed", "cancelled", "canceled"}:
            out["status"] = "FAILED_CLOSED"
            out["reason"] = "MANUS_PROVIDER_TASK_FAILED"
            return out
        if status not in {"stopped", "completed", "success", "succeeded"}:
            return out

        messages = self.client.list_messages(task_id, limit=100)
        result = _latest_structured_value(messages)
        if result is None:
            out["status"] = "FAILED_CLOSED"
            out["reason"] = "MANUS_STRUCTURED_RESULT_MISSING"
            return out

        # Validate any Manus -> JAYTEC specialist request as request-only
        # metadata. It never self-dispatches another provider.
        requests = result.get("specialist_requests")
        if isinstance(requests, list):
            for request in requests:
                validate_specialist_request(_decode_specialist_request(request))

        verify_manus_completion(result)
        out["status"] = "VERIFIED_COMPLETE"
        out["result"] = dict(result)
        return out


def runtime_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ManusInsufficientCredits):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "BLOCKED_LITE_AVAILABILITY",
            "error": "MANUS_LITE_VENDOR_QUOTA_OR_AVAILABILITY_BLOCK",
            "monetary_topup_required": False,
        }
    if isinstance(
        exc,
        (ManusRuntimeError, ManusProfilePolicyError, ManusGovernanceError, ManusError),
    ):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED_CLOSED",
            "error": str(exc),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAILED_CLOSED",
        "error": type(exc).__name__,
    }
