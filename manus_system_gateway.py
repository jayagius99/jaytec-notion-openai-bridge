"""System-wide JAYTEC -> Manus Lite access gateway.

All JAYTEC participants reach Manus through this one JAYTEC-owned boundary.
The caller never talks to Manus directly and never selects a Manus profile.
Manus remains isolated: this module adds inbound reachability only; it grants
no Manus -> participant edge and no provider/spend/ROOT authority.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from manus_runtime import ManusLiteRuntime, runtime_error_payload

SCHEMA_VERSION = "JAYTEC_MANUS_SYSTEM_GATEWAY_V1"
MAX_GATEWAY_BYTES = 48_000

# Logical JAYTEC participants/services that may REQUEST Manus through JAYTEC.
# Resources (GitHub/Neon/Render) and Manus itself are deliberately excluded.
ALLOWED_GATEWAY_CALLERS = frozenset({
    "chatgpt",
    "jaytec",
    "watch",
    "forge",
    "core_triad",
    "engineering_specialist",
    "research_specialist",
    "deepseek",
    "nemo",
    "gemini",
    "notion_courier",
})

# A specialist/courier may ask JAYTEC to use Manus, but it cannot grant Manus
# connector mutation authority. Only the supervising control-plane identities
# may carry an already-authorized mutation grant into the existing runtime.
MUTATION_AUTHORITY_CALLERS = frozenset({"chatgpt", "jaytec"})
NON_CONTROL_ALLOWED_ACTIONS = frozenset({
    "read",
    "inspect",
    "diagnose",
    "test",
    "analyze",
    "research",
    "review",
    "report",
})
NON_CONTROL_CONNECTOR_PURPOSES = frozenset({
    "read",
    "inspect",
    "diagnose",
    "test",
})


class ManusSystemGatewayError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _object(raw: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise ManusSystemGatewayError("MANUS_GATEWAY_REQUEST_NOT_STRING")
    if len(raw.encode("utf-8")) > MAX_GATEWAY_BYTES:
        raise ManusSystemGatewayError("MANUS_GATEWAY_REQUEST_TOO_LARGE")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManusSystemGatewayError("MANUS_GATEWAY_REQUEST_INVALID_JSON") from exc
    if not isinstance(value, Mapping):
        raise ManusSystemGatewayError("MANUS_GATEWAY_REQUEST_NOT_OBJECT")
    return value


def normalize_gateway_caller(value: Any) -> str:
    caller = str(value or "").strip().casefold()
    if caller not in ALLOWED_GATEWAY_CALLERS:
        raise ManusSystemGatewayError("MANUS_GATEWAY_CALLER_NOT_ALLOWLISTED")
    return caller


def _mutation_guard(caller: str, mutation_authorized: Any) -> bool:
    if type(mutation_authorized) is not bool:
        raise ManusSystemGatewayError("MANUS_GATEWAY_MUTATION_AUTH_INVALID")
    if mutation_authorized and caller not in MUTATION_AUTHORITY_CALLERS:
        raise ManusSystemGatewayError("MANUS_GATEWAY_MUTATION_AUTH_CALLER_BLOCKED")
    return mutation_authorized


def _non_control_scope_guard(caller: str, payload: Mapping[str, Any]) -> None:
    """Let every participant request Manus without letting it mint authority."""

    if caller in MUTATION_AUTHORITY_CALLERS:
        return

    actions = payload.get("allowed_actions")
    if not isinstance(actions, list) or not actions:
        raise ManusSystemGatewayError("MANUS_GATEWAY_ALLOWED_ACTIONS_INVALID")
    normalized_actions = {
        str(action or "").strip().casefold()
        for action in actions
    }
    if (
        "" in normalized_actions
        or not normalized_actions.issubset(NON_CONTROL_ALLOWED_ACTIONS)
    ):
        raise ManusSystemGatewayError(
            "MANUS_GATEWAY_NON_CONTROL_ACTION_BLOCKED"
        )

    purposes = payload.get("connector_purposes", {})
    if not isinstance(purposes, Mapping):
        raise ManusSystemGatewayError(
            "MANUS_GATEWAY_CONNECTOR_PURPOSES_INVALID"
        )
    for purpose in purposes.values():
        normalized = str(purpose or "").strip().casefold()
        if normalized not in NON_CONTROL_CONNECTOR_PURPOSES:
            raise ManusSystemGatewayError(
                "MANUS_GATEWAY_NON_CONTROL_CONNECTOR_PURPOSE_BLOCKED"
            )


def _normalized_start(raw: str) -> tuple[str, str]:
    outer = _object(raw)
    if set(outer) != {"caller", "task"}:
        raise ManusSystemGatewayError("MANUS_GATEWAY_START_FIELDS_INVALID")
    caller = normalize_gateway_caller(outer.get("caller"))
    task = outer.get("task")
    if not isinstance(task, Mapping):
        raise ManusSystemGatewayError("MANUS_GATEWAY_TASK_INVALID")

    payload = dict(task)
    if str(payload.get("scope") or "").strip().casefold() != "jaytec_delegated_task":
        raise ManusSystemGatewayError("MANUS_GATEWAY_SCOPE_BLOCKED")
    if payload.get("current_task_authorized") is not True:
        raise ManusSystemGatewayError("MANUS_GATEWAY_CURRENT_AUTH_REQUIRED")

    supplied_authority = str(payload.get("authority_source") or "jaytec").strip().casefold()
    if supplied_authority != "jaytec":
        raise ManusSystemGatewayError("MANUS_GATEWAY_AUTHORITY_SOURCE_BLOCKED")
    payload["authority_source"] = "jaytec"

    mutation_authorized = _mutation_guard(
        caller, payload.get("connector_mutation_authorized", False)
    )
    payload["connector_mutation_authorized"] = mutation_authorized
    _non_control_scope_guard(caller, payload)

    context = payload.get("required_context", {})
    if not isinstance(context, Mapping):
        raise ManusSystemGatewayError("MANUS_GATEWAY_CONTEXT_INVALID")
    context = dict(context)
    existing_caller = context.get("manus_gateway_caller")
    if existing_caller not in (None, caller):
        raise ManusSystemGatewayError("MANUS_GATEWAY_CALLER_CONTEXT_MISMATCH")
    context["manus_gateway_caller"] = caller
    context["manus_gateway_boundary"] = SCHEMA_VERSION
    payload["required_context"] = context

    return caller, json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _normalized_continue(raw: str) -> dict[str, Any]:
    outer = _object(raw)
    allowed = {
        "caller",
        "provider_task_id",
        "handoff_id",
        "handoff_context",
        "connector_purposes",
        "connector_mutation_authorized",
    }
    if set(outer) - allowed:
        raise ManusSystemGatewayError("MANUS_GATEWAY_CONTINUE_FIELDS_INVALID")
    required = {"caller", "provider_task_id", "handoff_id", "handoff_context"}
    if required - set(outer):
        raise ManusSystemGatewayError("MANUS_GATEWAY_CONTINUE_FIELDS_MISSING")

    caller = normalize_gateway_caller(outer.get("caller"))
    mutation_authorized = _mutation_guard(
        caller, outer.get("connector_mutation_authorized", False)
    )
    context = outer.get("handoff_context")
    if not isinstance(context, Mapping):
        raise ManusSystemGatewayError("MANUS_GATEWAY_HANDOFF_CONTEXT_INVALID")
    context = dict(context)
    existing_caller = context.get("manus_gateway_caller")
    if existing_caller not in (None, caller):
        raise ManusSystemGatewayError("MANUS_GATEWAY_CALLER_CONTEXT_MISMATCH")
    context["manus_gateway_caller"] = caller
    context["manus_gateway_boundary"] = SCHEMA_VERSION

    purposes = outer.get("connector_purposes", {})
    if not isinstance(purposes, Mapping):
        raise ManusSystemGatewayError("MANUS_GATEWAY_CONNECTOR_PURPOSES_INVALID")
    if caller not in MUTATION_AUTHORITY_CALLERS:
        for purpose in purposes.values():
            normalized = str(purpose or "").strip().casefold()
            if normalized not in NON_CONTROL_CONNECTOR_PURPOSES:
                raise ManusSystemGatewayError(
                    "MANUS_GATEWAY_NON_CONTROL_CONNECTOR_PURPOSE_BLOCKED"
                )

    return {
        "caller": caller,
        "provider_task_id": str(outer.get("provider_task_id") or "").strip(),
        "handoff_id": str(outer.get("handoff_id") or "").strip(),
        "handoff_context": context,
        "connector_purposes": dict(purposes),
        "connector_mutation_authorized": mutation_authorized,
    }


class ManusSystemGateway:
    def __init__(self, runtime: ManusLiteRuntime, registry: Any):
        self.runtime = runtime
        self.registry = registry

    def start_task(self, request_json: str) -> dict[str, Any]:
        caller, task_json = _normalized_start(request_json)
        result = self.runtime.start_task_idempotent(task_json, self.registry)
        return {
            "schema_version": SCHEMA_VERSION,
            "gateway_caller": caller,
            "manus_route": "JAYTEC_MANUS_LITE_RUNTIME_V1",
            "result": result,
        }

    def continue_task(self, request_json: str) -> dict[str, Any]:
        req = _normalized_continue(request_json)
        result = self.runtime.continue_task_handoff(
            req["provider_task_id"],
            scope="jaytec_delegated_task",
            authority_source="jaytec",
            current_task_authorized=True,
            connector_purposes=req["connector_purposes"],
            connector_mutation_authorized=req["connector_mutation_authorized"],
            handoff_id=req["handoff_id"],
            handoff_context=req["handoff_context"],
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "gateway_caller": req["caller"],
            "manus_route": "JAYTEC_MANUS_LITE_RUNTIME_V1",
            "result": result,
        }

    def task_status(self, caller: str, provider_task_id: str) -> dict[str, Any]:
        normalized = normalize_gateway_caller(caller)
        # System-wide status is intentionally read-only. It never stops or
        # resumes a Manus task and therefore cannot become a side-effect path.
        result = self.runtime.task_status_readonly(provider_task_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "gateway_caller": normalized,
            "manus_route": "JAYTEC_MANUS_LITE_RUNTIME_V1",
            "read_only": True,
            "result": result,
        }


def gateway_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ManusSystemGatewayError):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED_CLOSED",
            "error": str(exc),
        }
    nested = runtime_error_payload(exc)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAILED_CLOSED",
        "error": nested.get("error", type(exc).__name__),
    }
