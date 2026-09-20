"""STAGING ONLY: unified JAYTEC execute_task_packet MCP surface.

CI contract: the real staging_server.py HTTP entrypoint is smoke-tested before
changes may merge, so Render startup behavior is not inferred from server.py.

This entrypoint is intentionally independent from production server.py so the
staging service can boot and validate packets without production provider keys.
Specialist dispatch fails closed until the corresponding server-side credential
is configured. Do not promote until BRIDGE_CONTRACT_V1.md activation gates pass.

New in C4: optional Postgres-backed idempotency via DATABASE_URL.
- If DATABASE_URL is set, staging uses durable idempotency and MUST fail closed
  if DB is unavailable (no silent durable->memory fallback).
- If DATABASE_URL is not set, staging stays on process memory.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
import threading
from typing import Any, Mapping
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier, require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import AuthMiddleware
from openai import OpenAI
from starlette.requests import Request
from starlette.responses import JSONResponse

from autorecovery_watch_ingress import WatchIngressError, execute_watch_cycle
from github_watch_oidc import GitHubActionsWatchOIDCVerifier, validate_watch_claims
from autorecovery_runtime import (
    assignment_status as read_autorecovery_assignment_status,
    runtime_status as get_autorecovery_runtime_status,
    schema_probe as probe_autorecovery_schema,
    validate_checkpoint_payload,
)
from circuit_breaker import CircuitBreaker
from http_security import load_host_origin_policy
from provider_endpoints import (
    OPENAI_API_BASE,
    OPENROUTER_API_BASE,
    validate_openai_endpoint,
    validate_openrouter_endpoint,
)
from deepseek_reviewer import (
    EXPECTED_DEEPSEEK_REVIEWER_MODEL,
    build_deepseek_security_review_dispatch,
)
from jaytec_read import build_jaytec_read_packet, enforce_orchestrated_read_report, enforce_read_report
from jaytec_protocol_portal import PortalStore, safe_error as portal_safe_error
from forge_cognition import ForgeMindStore, safe_error as forge_cognition_safe_error
from manus_adapter import MANUS_API_KEY, ManusClient
from manus_runtime import ManusLiteRuntime, runtime_error_payload
from orchestration import ExecutionRegistry, PacketValidationError, execute_task_packet_core, parse_packet_json
from startup_probe_guard import authorize_startup_probe, sha256_json, sha256_text
from specialist_adapters import (
    EXPECTED_CODEX_MODEL,
    EXPECTED_GEMINI_MODEL,
    build_codex_dispatch,
    build_engineering_dispatch,
    build_gemini_dispatch,
    ENGINEERING_PROVIDER_ACTIVE,
    OPENROUTER_PROVIDER_ACTIVE,
    resolve_engineering_model,
    resolve_engineering_provider_mode,
    resolve_openrouter_provider_mode,
)

PORT = int(os.environ.get("PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = validate_openai_endpoint(
    os.environ.get("OPENAI_BASE_URL", OPENAI_API_BASE).strip()
)
ENGINEERING_MODEL = resolve_engineering_model()
CODEX_MODEL = ENGINEERING_MODEL
ENGINEERING_PROVIDER_MODE = resolve_engineering_provider_mode()
OPENROUTER_PROVIDER_MODE = resolve_openrouter_provider_mode()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = validate_openrouter_endpoint(
    os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_API_BASE).strip()
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "90"))
DEEPSEEK_REVIEWER_MODEL = os.environ.get(
    "DEEPSEEK_REVIEWER_MODEL",
    EXPECTED_DEEPSEEK_REVIEWER_MODEL,
).strip()
DEEPSEEK_REVIEWER_TIMEOUT_S = float(
    os.environ.get("DEEPSEEK_REVIEWER_TIMEOUT_S", "120")
)
CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("CIRCUIT_FAILURE_THRESHOLD", "3"))
CIRCUIT_RESET_SECONDS = int(os.environ.get("CIRCUIT_RESET_SECONDS", "60"))
DATABASE_URL = (
    os.environ.get("JAYTEC_STAGING_DATABASE_URL", "").strip()
    or os.environ.get("DATABASE_URL", "").strip()
)
PROTOCOL_DATABASE_URL = os.environ.get("JAYTEC_PROTOCOL_DATABASE_URL", "").strip()
FORGE_COGNITION_DATABASE_URL = (
    os.environ.get("FORGE_COGNITION_DATABASE_URL", "").strip()
    or PROTOCOL_DATABASE_URL
)

if not MCP_AUTH_TOKEN:
    raise RuntimeError("MCP_AUTH_TOKEN is required")

static_auth = StaticTokenVerifier(
    tokens={
        MCP_AUTH_TOKEN: {
            "sub": "jaytec-staging-client",
            "client_id": "jaytec-orchestration-staging",
            "scopes": ["jaytec:mcp"],
        }
    }
)
watch_oidc_auth = GitHubActionsWatchOIDCVerifier()
mcp = FastMCP(
    "JAYTEC Orchestration Staging",
    auth=static_auth,
    middleware=[AuthMiddleware(auth=require_scopes("jaytec:mcp"))],
)

def _autorecovery_components_registered() -> bool:
    return bool(
        MANUS_API_KEY
        and os.environ.get("JAYTEC_AUTORECOVERY_RUNTIME_COMPONENTS", "").strip()
        == "manus_lite_v1"
    )

# Safe startup telemetry: never print credentials, only whether the governed
# Lite-only Manus route is configured.
print(
    json.dumps(
        {
            "event": "JAYTEC_MANUS_RUNTIME_STATUS",
            "configured": bool(MANUS_API_KEY),
            "profile_policy": "lite_only_no_exceptions",
        },
        sort_keys=True,
    ),
    flush=True,
)

# --- Idempotency registry selection (staging only) ---
if DATABASE_URL:
    from idempotency_postgres import PostgresExecutionRegistry

    REGISTRY = PostgresExecutionRegistry(database_url=DATABASE_URL, ttl_seconds=ExecutionRegistry().ttl_seconds)
    REGISTRY.ensure_schema()
    IDEMPOTENCY_STORE = "postgres"
else:
    REGISTRY = ExecutionRegistry()
    IDEMPOTENCY_STORE = "process_memory_staging_only"

CODEX_CIRCUIT = CircuitBreaker(
    failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
    reset_after_seconds=CIRCUIT_RESET_SECONDS,
)
GEMINI_CIRCUIT = CircuitBreaker(
    failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
    reset_after_seconds=CIRCUIT_RESET_SECONDS,
)
DEEPSEEK_REVIEWER_CIRCUIT = CircuitBreaker(
    failure_threshold=CIRCUIT_FAILURE_THRESHOLD,
    reset_after_seconds=CIRCUIT_RESET_SECONDS,
)

OPENAI_CLIENT = (
    OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    if ENGINEERING_PROVIDER_MODE == ENGINEERING_PROVIDER_ACTIVE and OPENAI_API_KEY
    else None
)
OPENROUTER_CLIENT = (
    OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
    if OPENROUTER_PROVIDER_MODE == OPENROUTER_PROVIDER_ACTIVE
    and OPENROUTER_API_KEY
    else None
)

# Build dispatchers ONCE to avoid runtime drift and repeated guards.
ENGINEERING_DISPATCH = (
    build_engineering_dispatch(openai_client=OPENAI_CLIENT, engineering_model=ENGINEERING_MODEL, circuit=CODEX_CIRCUIT)
    if OPENAI_CLIENT
    else CODEX_CIRCUIT.guard(
        lambda _packet: (_ for _ in ()).throw(RuntimeError("ENGINEERING_PROVIDER_DOOR_LOCKED_RESERVE"))
    )
)
CODEX_DISPATCH = ENGINEERING_DISPATCH  # TaskPacket v1 wire alias

GEMINI_BLOCK_REASON = (
    "OPENROUTER_PROVIDER_DOOR_LOCKED_RESERVE"
    if OPENROUTER_PROVIDER_MODE != OPENROUTER_PROVIDER_ACTIVE
    else "OPENROUTER_API_KEY is not configured on the staging bridge"
)

GEMINI_DISPATCH = (
    build_gemini_dispatch(
        openrouter_client=OPENROUTER_CLIENT,
        gemini_model=GEMINI_MODEL,
        gemini_timeout_s=GEMINI_TIMEOUT_S,
        circuit=GEMINI_CIRCUIT,
    )
    if OPENROUTER_CLIENT
    else GEMINI_CIRCUIT.guard(
        lambda _packet: (_ for _ in ()).throw(RuntimeError(GEMINI_BLOCK_REASON))
    )
)

DEEPSEEK_REVIEW_DISPATCH = (
    build_deepseek_security_review_dispatch(
        openrouter_client=OPENROUTER_CLIENT,
        model=DEEPSEEK_REVIEWER_MODEL,
        timeout_s=DEEPSEEK_REVIEWER_TIMEOUT_S,
        circuit=DEEPSEEK_REVIEWER_CIRCUIT,
    )
    if OPENROUTER_CLIENT
    else DEEPSEEK_REVIEWER_CIRCUIT.guard(
        lambda _packet: (_ for _ in ()).throw(
            RuntimeError(GEMINI_BLOCK_REASON)
        )
    )
)


@mcp.tool
def orchestration_status() -> str:
    autorecovery = get_autorecovery_runtime_status(
        env=os.environ,
        database_url=DATABASE_URL,
        runtime_components_registered=_autorecovery_components_registered(),
    ).to_dict()
    portal = (
        PortalStore(PROTOCOL_DATABASE_URL).probe()
        if PROTOCOL_DATABASE_URL
        else {
            "schema_version": "JAYTEC_PROTOCOL_PORTAL_V1",
            "status": "BLOCKED",
            "reason": "JAYTEC_PROTOCOL_DATABASE_URL_NOT_CONFIGURED",
        }
    )
    forge_cognition = (
        ForgeMindStore(FORGE_COGNITION_DATABASE_URL).probe()
        if FORGE_COGNITION_DATABASE_URL
        else {
            "schema_version": "FORGE_COGNITIVE_CONTINUITY_V1",
            "status": "BLOCKED",
            "reason": "FORGE_COGNITION_DATABASE_URL_NOT_CONFIGURED",
        }
    )
    return json.dumps(
        {
            "status": "STAGING",
            "operation": "execute_task_packet",
            "engineering_model": ENGINEERING_MODEL,
            "engineering_provider_mode": ENGINEERING_PROVIDER_MODE,
            "openrouter_provider_mode": OPENROUTER_PROVIDER_MODE,
            "codex_model": CODEX_MODEL,  # legacy compatibility field
            "codex_adapter_configured": bool(OPENAI_CLIENT),
            "codex_circuit": CODEX_CIRCUIT.snapshot(),
            "gemini_model": GEMINI_MODEL,
            "gemini_adapter_configured": bool(OPENROUTER_CLIENT),
            "gemini_provider_routing": "price",
            "gemini_circuit": GEMINI_CIRCUIT.snapshot(),
            "deepseek_reviewer_model": DEEPSEEK_REVIEWER_MODEL,
            "deepseek_reviewer_configured": bool(OPENROUTER_CLIENT),
            "deepseek_reviewer_circuit": DEEPSEEK_REVIEWER_CIRCUIT.snapshot(),
            "deepseek_reviewer_route_policy": "exact_model_bounded_same_model_provider_retry",
            "manus_adapter_configured": bool(MANUS_API_KEY),
            "manus_profile_policy": "lite_only_no_exceptions",
            "manus_runtime": "JAYTEC_MANUS_LITE_RUNTIME_V1",
            "idempotency_store": IDEMPOTENCY_STORE,
            "autorecovery": autorecovery,
            "protocol_portal": portal,
            "forge_cognition": forge_cognition,
            "production_ready": False,
        },
        sort_keys=True,
    )


def _manus_runtime() -> ManusLiteRuntime:
    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY_NOT_CONFIGURED")
    return ManusLiteRuntime(ManusClient())


@mcp.tool
def manus_start_task(request_json: str) -> str:
    """Start one bounded JAYTEC-owned Manus task. Profile is permanently Lite."""
    try:
        result = _manus_runtime().start_task_idempotent(request_json, REGISTRY)
    except Exception as exc:
        result = runtime_error_payload(exc)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


@mcp.tool
def manus_task_status(provider_task_id: str) -> str:
    """Read/verify one Manus task. Non-Lite identity fails closed and is stopped."""
    try:
        result = _manus_runtime().task_status(provider_task_id)
    except Exception as exc:
        result = runtime_error_payload(exc)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


@mcp.tool
def autorecovery_runtime_status() -> str:
    """Read fail-closed WATCH + AUTORECOVERY installation/activation state.

    This tool is status-only. It never creates schema, registers an assignment,
    acquires a lease, or invokes a worker.
    """
    status = get_autorecovery_runtime_status(
        env=os.environ,
        database_url=DATABASE_URL,
        runtime_components_registered=_autorecovery_components_registered(),
    )
    result = status.to_dict()
    result["schema_probe"] = probe_autorecovery_schema(DATABASE_URL)
    return json.dumps(result, sort_keys=True)


@mcp.tool
def autorecovery_checkpoint_validate(checkpoint_json: str) -> str:
    """Validate a resurrection checkpoint without registering or executing it."""
    try:
        result = validate_checkpoint_payload(checkpoint_json)
    except Exception as exc:
        result = {
            "status": "INVALID",
            "automatic_recovery_authorized": False,
            "reason": type(exc).__name__ + ":" + str(exc),
        }
    return json.dumps(result, sort_keys=True)


@mcp.tool
def autorecovery_assignment_status(task_id: str) -> str:
    """Read canonical task state only; never mutate/recover the assignment."""
    status = get_autorecovery_runtime_status(
        env=os.environ,
        database_url=DATABASE_URL,
        runtime_components_registered=_autorecovery_components_registered(),
    )
    result = read_autorecovery_assignment_status(
        DATABASE_URL,
        str(task_id or "").strip(),
        schema_ready_declared=status.schema_ready_declared,
    )
    result["autorecovery_active"] = status.active
    result["ui_chat_autoresume_supported"] = False
    return json.dumps(result, sort_keys=True)


@mcp.custom_route("/jaytec/watch-cycle", methods=["POST"])
async def jaytec_watch_cycle(request: Request) -> JSONResponse:
    """OIDC-authenticated, WATCH-only ingress for exactly one recovery cycle."""

    auth_header = str(request.headers.get("authorization") or "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_OIDC_BEARER_REQUIRED"},
            status_code=401,
        )
    raw_token = auth_header[len("Bearer "):].strip()
    if not raw_token:
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_OIDC_BEARER_REQUIRED"},
            status_code=401,
        )
    try:
        access = await watch_oidc_auth.verify_token(raw_token)
    except Exception:
        access = None
    if access is None or "jaytec:watch-cycle" not in set(access.scopes or []):
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_OIDC_VERIFICATION_FAILED"},
            status_code=403,
        )
    ok, reason = validate_watch_claims(access.claims)
    if not ok:
        return JSONResponse(
            {"status": "DENIED", "reason": reason},
            status_code=403,
        )

    raw = await request.body()
    if len(raw) > 96_000:
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_CYCLE_REQUEST_TOO_LARGE"},
            status_code=413,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_CYCLE_JSON_INVALID"},
            status_code=400,
        )
    if not isinstance(payload, Mapping):
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_CYCLE_ROOT_INVALID"},
            status_code=400,
        )

    try:
        components_registered = _autorecovery_components_registered()
        result = execute_watch_cycle(
            payload,
            database_url=DATABASE_URL,
            manus_runtime=(_manus_runtime() if components_registered else None),
            registry=REGISTRY,
            runtime_components_registered=components_registered,
            env=os.environ,
        )
    except WatchIngressError as exc:
        result = {"status": "DENIED", "reason": str(exc)}
        return JSONResponse(result, status_code=403)
    except Exception as exc:
        result = {
            "status": "FAILED_CLOSED",
            "reason": "WATCH_CYCLE_EXCEPTION:" + type(exc).__name__,
        }
        return JSONResponse(result, status_code=503)

    status_code = 200 if result.get("status") == "PASS" else 409
    return JSONResponse(result, status_code=status_code)


@mcp.custom_route("/jaytec/watch-status", methods=["POST"])
async def jaytec_watch_status(request: Request) -> JSONResponse:
    """OIDC-authenticated read-only status for the canonical WATCH assignment."""

    auth_header = str(request.headers.get("authorization") or "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_OIDC_BEARER_REQUIRED"},
            status_code=401,
        )
    raw_token = auth_header[len("Bearer "):].strip()
    if not raw_token:
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_OIDC_BEARER_REQUIRED"},
            status_code=401,
        )
    try:
        access = await watch_oidc_auth.verify_token(raw_token)
    except Exception:
        access = None
    if access is None or "jaytec:watch-cycle" not in set(access.scopes or []):
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_OIDC_VERIFICATION_FAILED"},
            status_code=403,
        )
    ok, reason = validate_watch_claims(access.claims)
    if not ok:
        return JSONResponse(
            {"status": "DENIED", "reason": reason},
            status_code=403,
        )

    raw = await request.body()
    if len(raw) > 8_192:
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_STATUS_REQUEST_TOO_LARGE"},
            status_code=413,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_STATUS_JSON_INVALID"},
            status_code=400,
        )
    if not isinstance(payload, Mapping):
        return JSONResponse(
            {"status": "DENIED", "reason": "WATCH_STATUS_ROOT_INVALID"},
            status_code=400,
        )

    task_id = str(payload.get("task_id") or "").strip()
    if task_id != "FORGE-GENESIS-ACTIVATION-001":
        return JSONResponse(
            {"status": "DENIED", "reason": "TASK_ID_NOT_WATCH_AUTHORIZED"},
            status_code=403,
        )

    runtime = get_autorecovery_runtime_status(
        env=os.environ,
        database_url=DATABASE_URL,
        runtime_components_registered=_autorecovery_components_registered(),
    )
    result = read_autorecovery_assignment_status(
        DATABASE_URL,
        task_id,
        schema_ready_declared=runtime.schema_ready_declared,
    )
    result["autorecovery_active"] = runtime.active
    result["runtime_components_registered"] = runtime.runtime_components_registered

    # If a recovery invocation failed after claiming its fence, the exact
    # fail-closed runtime result is already stored under a deterministic
    # idempotency key. Peek only that existing record; never call Manus here.
    attempt = result.get("recovery_attempts")
    fence = result.get("fencing_token")
    route_by_attempt = {
        1: "same_worker_provider",
        2: "fresh_worker_same_checkpoint",
        3: "alternate_approved_route",
    }
    if (
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and isinstance(fence, int)
        and not isinstance(fence, bool)
        and attempt in route_by_attempt
        and fence >= 1
        and hasattr(REGISTRY, "peek_result")
    ):
        recovery_key = (
            "manus:"
            + task_id
            + ":recovery:"
            + str(fence)
            + ":"
            + route_by_attempt[attempt]
        )
        try:
            stored = REGISTRY.peek_result(recovery_key)
        except Exception as exc:
            result["last_invocation_record"] = {
                "status": "DIAGNOSTIC_READ_FAILED",
                "error": type(exc).__name__,
            }
        else:
            if isinstance(stored, Mapping):
                safe_fields = (
                    "status",
                    "error",
                    "monetary_topup_required",
                    "requested_profile",
                    "observed_profile_verified",
                )
                result["last_invocation_record"] = {
                    key: stored.get(key)
                    for key in safe_fields
                    if key in stored
                }

    result["read_only"] = True
    result["lease_acquired"] = False
    result["worker_invoked"] = False
    return JSONResponse(result, status_code=200)



def _protocol_portal() -> PortalStore:
    if not PROTOCOL_DATABASE_URL:
        raise RuntimeError("JAYTEC_PROTOCOL_DATABASE_URL_NOT_CONFIGURED")
    return PortalStore(PROTOCOL_DATABASE_URL)


@mcp.tool
def jaytec_protocol_portal_status() -> str:
    """Read protocol-portal readiness and active WATCH-addressable tasks."""
    if not PROTOCOL_DATABASE_URL:
        return json.dumps(
            {
                "schema_version": "JAYTEC_PROTOCOL_PORTAL_V1",
                "status": "BLOCKED",
                "reason": "JAYTEC_PROTOCOL_DATABASE_URL_NOT_CONFIGURED",
            },
            sort_keys=True,
        )
    try:
        store = _protocol_portal()
        return json.dumps(
            {
                **store.probe(),
                "watch_tasks": store.list_watch_tasks(include_terminal=False),
                "ui_chat_autoresume_supported": False,
            },
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps(portal_safe_error(exc), sort_keys=True)


@mcp.tool
def jaytec_protocol_register_task(request_json: str) -> str:
    """Register/update one current-authorized task in canonical JAYTEC jobs/events.

    Registration creates continuity/WATCH state only. It grants no specialist,
    connector, provider, spend, ROOT_OWNER, merge, or deployment authority.
    """
    try:
        result = _protocol_portal().register(request_json)
    except Exception as exc:
        result = portal_safe_error(exc)
    return json.dumps(result, default=str, sort_keys=True)


@mcp.tool
def jaytec_protocol_checkpoint_task(
    task_id: str,
    checkpoint_json: str,
    expected_fence_token: int,
) -> str:
    """Append a durable checkpoint for one registered task using current fencing."""
    try:
        result = _protocol_portal().checkpoint(
            task_id,
            checkpoint_json,
            expected_fence_token=expected_fence_token,
        )
    except Exception as exc:
        result = portal_safe_error(exc)
    return json.dumps(result, default=str, sort_keys=True)


@mcp.tool
def jaytec_protocol_task_status(task_id: str) -> str:
    """Read one protocol task and its latest durable checkpoint."""
    try:
        result = _protocol_portal().status(task_id)
    except Exception as exc:
        result = portal_safe_error(exc)
    return json.dumps(result, default=str, sort_keys=True)


@mcp.tool
def jaytec_protocol_list_watch_tasks(include_terminal: bool = False, limit: int = 128) -> str:
    """List WATCH-enabled tasks from all chats without changing them."""
    try:
        result = {
            "schema_version": "JAYTEC_PROTOCOL_PORTAL_V1",
            "tasks": _protocol_portal().list_watch_tasks(
                include_terminal=include_terminal,
                limit=limit,
            ),
        }
    except Exception as exc:
        result = portal_safe_error(exc)
    return json.dumps(result, default=str, sort_keys=True)


@mcp.tool
def jaytec_protocol_transition_task(
    task_id: str,
    action: str,
    expected_fence_token: int,
    reason: str = "",
) -> str:
    """PAUSE, RESUME or COMPLETE a portal task with stale-writer fencing."""
    try:
        result = _protocol_portal().transition(
            task_id,
            action=action,
            expected_fence_token=expected_fence_token,
            reason=reason,
        )
    except Exception as exc:
        result = portal_safe_error(exc)
    return json.dumps(result, default=str, sort_keys=True)


@mcp.tool
def forge_cognition_status() -> str:
    """Read Forge canonical cognition readiness without activating Forge."""
    if not FORGE_COGNITION_DATABASE_URL:
        return json.dumps(
            {
                "schema_version": "FORGE_COGNITIVE_CONTINUITY_V1",
                "status": "BLOCKED",
                "reason": "FORGE_COGNITION_DATABASE_URL_NOT_CONFIGURED",
                "activated": False,
            },
            sort_keys=True,
        )
    try:
        store = ForgeMindStore(FORGE_COGNITION_DATABASE_URL)
        probe = store.probe()
        snapshot = store.snapshot("FORGE") if probe.get("status") == "PASS" else None
        return json.dumps(
            {
                **probe,
                "activated": bool(snapshot and snapshot.get("mode") == "RUNNING"),
                "forge_snapshot": snapshot,
            },
            default=str,
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps(forge_cognition_safe_error(exc), sort_keys=True)


@mcp.tool
def forge_cognition_snapshot() -> str:
    """Read the canonical Forge mind state; no mutation and no model invocation."""
    if not FORGE_COGNITION_DATABASE_URL:
        return json.dumps(
            {
                "schema_version": "FORGE_COGNITIVE_CONTINUITY_V1",
                "status": "BLOCKED",
                "reason": "FORGE_COGNITION_DATABASE_URL_NOT_CONFIGURED",
            },
            sort_keys=True,
        )
    try:
        return json.dumps(
            ForgeMindStore(FORGE_COGNITION_DATABASE_URL).snapshot("FORGE"),
            default=str,
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps(forge_cognition_safe_error(exc), sort_keys=True)


@mcp.tool
def idempotency_persistence_probe() -> str:
    if IDEMPOTENCY_STORE != "postgres":
        return json.dumps({"status": "SKIP", "idempotency_store": IDEMPOTENCY_STORE}, sort_keys=True)
    try:
        pruned = REGISTRY.prune()
        return json.dumps({"status": "PASS", "idempotency_store": IDEMPOTENCY_STORE, "pruned": pruned}, sort_keys=True)
    except Exception as exc:
        return json.dumps({"status": "FAIL", "idempotency_store": IDEMPOTENCY_STORE, "error": type(exc).__name__}, sort_keys=True)


@mcp.tool
def execute_task_packet(packet_json: str) -> str:
    packet, parse_errors = parse_packet_json(packet_json)
    if packet is None:
        return json.dumps(
            {
                "execution_id": "invalid",
                "task_id": "",
                "subtask_id": "",
                "overall_status": "INVALID_PACKET",
                "unresolved_items": list(parse_errors),
                "return_schema_version": "1.0",
            },
            sort_keys=True,
        )

    packet_digest = sha256_json(packet)
    if not _startup_probe_authorized("task_packet_dispatch", packet_digest):
        return json.dumps(
            {
                "execution_id": "blocked",
                "task_id": str(packet.get("task_id", "")),
                "subtask_id": str(packet.get("subtask_id", "")),
                "overall_status": "POLICY_BLOCKED",
                "unresolved_items": ["ONE_SHOT_TASK_PACKET_DISPATCH_AUTH_REQUIRED"],
                "approval_required": True,
                "packet_hash": packet_digest,
                "return_schema_version": "1.0",
            },
            sort_keys=True,
        )

    def _lookup(key: str, digest: str, now=None):
        try:
            return REGISTRY.lookup(key, digest, now=now)
        except ValueError as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            raise

    def _store(key: str, digest: str, result, now=None):
        try:
            return REGISTRY.store(key, digest, result, now=now)
        except ValueError as exc:
            if str(exc) == "CONFLICTING_DUPLICATE":
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            raise

    if IDEMPOTENCY_STORE == "postgres":
        class _Wrapper(ExecutionRegistry):
            def lookup(self, key, packet_hash, *, now=None):
                return _lookup(key, packet_hash, now=now)

            def store(self, key, packet_hash, result, *, now=None):
                return _store(key, packet_hash, result, now=now)

        registry = _Wrapper()
    else:
        registry = REGISTRY

    result = execute_task_packet_core(
        packet,
        {"codex": CODEX_DISPATCH, "gemini": GEMINI_DISPATCH},
        registry,
    )
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _startup_probe_authorized(probe_name: str, payload_sha256: str) -> bool:
    decision = authorize_startup_probe(
        env=os.environ,
        registry=REGISTRY,
        idempotency_store=IDEMPOTENCY_STORE,
        probe_name=probe_name,
        payload_sha256=payload_sha256,
    )
    if not decision.allowed:
        print(
            "JAYTEC_STARTUP_PROBE_AUTH="
            + json.dumps(
                {
                    "status": "BLOCKED",
                    "probe_name": decision.probe_name,
                    "payload_sha256": decision.payload_sha256,
                    "reason": decision.reason,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return False
    print(
        "JAYTEC_STARTUP_PROBE_AUTH="
        + json.dumps(
            {
                "status": "AUTHORIZED_ONCE",
                "probe_name": decision.probe_name,
                "payload_sha256": decision.payload_sha256,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return True


def _run_jaytec_read_bootstrap_probe() -> None:
    """STAGING-ONLY one-shot JAYTEC:READ diagnostic.

    Activated only when JAYTEC_READ_BOOTSTRAP_URL is set in the staging
    environment. It performs no writes and logs only the validated READ result.
    """
    enabled = (
        os.environ.get("JAYTEC_READ_BOOTSTRAP_ENABLED", "0").strip() == "1"
    )
    if not enabled:
        return
    target = os.environ.get("JAYTEC_READ_BOOTSTRAP_URL", "").strip()
    if not target:
        print(
            'JAYTEC_READ_BOOTSTRAP_RESULT={"status":"BLOCKED","reason":"URL_REQUIRED"}',
            flush=True,
        )
        return
    if not _startup_probe_authorized("jaytec_read", sha256_text(target)):
        return
    packet = build_jaytec_read_packet(
        target,
        request_suffix="This is a one-shot staging recovery read requested by Jay. Return source-specific evidence only.",
        validation_suffix=["Return the exact shared-chat content needed to resume the paused JAYTEC security work."],
    )
    result = execute_task_packet_core(
        packet,
        {"gemini": GEMINI_DISPATCH},
        REGISTRY,
    )
    validated = enforce_orchestrated_read_report(result, target)
    print(
        "JAYTEC_READ_BOOTSTRAP_RESULT="
        + json.dumps(validated, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _run_god_project_review_probe() -> None:
    """STAGING-ONLY independent review of the pinned GOD Project v2.0 packet.

    Uses JAYTEC's registered independent reviewer adapter so model identity,
    schema enforcement, bounded format retry, diagnostics and fail-closed
    behavior match the normal JAYTEC specialist contract.
    """
    encoded = os.environ.get("JAYTEC_GOD_PROJECT_REVIEW_PACKET_GZ_B64", "").strip()
    expected_sha = os.environ.get("JAYTEC_GOD_PROJECT_REVIEW_PACKET_SHA256", "").strip()
    enabled = os.environ.get("JAYTEC_GOD_PROJECT_REVIEW_ENABLED", "").strip() == "1"
    if not enabled:
        return
    if not _startup_probe_authorized("god_project_review", expected_sha):
        return

    result = {
        "status": "FAILED_CLOSED",
        "model": GEMINI_MODEL,
        "ready_to_activate": False,
        "findings": [],
        "blockers": [],
        "required_changes": [],
        "confidence": None,
        "packet_sha256": None,
        "bridge_diagnostics": {},
    }

    try:
        if not encoded or not expected_sha:
            raise RuntimeError("review_packet_missing")
        packet = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
        packet_sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()
        result["packet_sha256"] = packet_sha
        if packet_sha != expected_sha:
            raise RuntimeError("review_packet_hash_mismatch")

        review_task = {
            "task_id": "JAYTEC-GOD-PROJECT-V2-FINAL-REVIEW",
            "subtask_id": "JAYTEC-GOD-PROJECT-V2-FINAL-REVIEW-" + packet_sha[:16],
            "workflow_id": "JAYTEC_GOD_PROJECT_FINAL_REVIEW_V2",
            "requested_specialist": "reviewer",
            "allowed_operations": [
                "research",
                "architecture_analysis",
                "adversarial_review",
                "evidence_synthesis",
                "challenge_verification",
            ],
            "max_retries": 1,
            "objective": (
                "Perform an independent hostile review of the exact JAYTEC GOD Project v2.0 "
                "launch packet in context.launch_packet. Challenge authority hierarchy contradictions; "
                "any route that could let ChatGPT, the Project, or specialists impersonate ROOT_OWNER; "
                "conflict with current V2 machine state; stale or unverifiable assumptions; duplicate "
                "or competing authority sources; missing migration, rollback, or completion gates; "
                "fake/impossible background autonomy claims; unsafe root-control activation; weak "
                "evidence/completion language; and setup instructions likely to make the new ChatGPT "
                "Project mis-operate. Do not make changes or use external tools. In conclusion return "
                "an object with exactly these keys: verdict (PASS|PASS_WITH_CHANGES|FAIL), "
                "ready_to_activate (boolean), blockers (array of strings), required_changes "
                "(array of strings), rationale (string). PASS means no material blocker remains. "
                "PASS_WITH_CHANGES means changes are required before activation. FAIL means the "
                "launch design has a material unresolved safety/authority/operability problem."
            ),
            "validation_criteria": [
                "Exact reviewer model identity is preserved.",
                "No specialist side effects or requested writes.",
                "Every material authority/security conflict is surfaced.",
                "Conclusion contains verdict, ready_to_activate, blockers, required_changes, rationale.",
                "The exact packet SHA-256 is preserved by the caller.",
            ],
            "context": {
                "packet_sha256": packet_sha,
                "launch_packet": packet,
            },
        }

        reviewed = dict(GEMINI_DISPATCH(review_task))
        if reviewed.get("model") != GEMINI_MODEL:
            raise RuntimeError("reviewer_model_mismatch")
        if reviewed.get("side_effects_attempted") not in ([], None):
            raise RuntimeError("reviewer_side_effect_violation")
        if reviewed.get("requested_operations") not in ([], None):
            raise RuntimeError("reviewer_requested_operations_nonempty")

        conclusion = reviewed.get("conclusion")
        if not isinstance(conclusion, dict):
            raise RuntimeError("reviewer_conclusion_not_object")
        verdict = conclusion.get("verdict")
        if verdict not in {"PASS", "PASS_WITH_CHANGES", "FAIL"}:
            raise RuntimeError("reviewer_verdict_invalid")
        ready = conclusion.get("ready_to_activate")
        blockers = conclusion.get("blockers")
        required_changes = conclusion.get("required_changes")
        if not isinstance(ready, bool):
            raise RuntimeError("reviewer_ready_flag_invalid")
        if not isinstance(blockers, list) or not all(isinstance(x, str) for x in blockers):
            raise RuntimeError("reviewer_blockers_invalid")
        if not isinstance(required_changes, list) or not all(isinstance(x, str) for x in required_changes):
            raise RuntimeError("reviewer_required_changes_invalid")

        specialist_status = reviewed.get("status")
        if specialist_status != "SUCCESS":
            result["status"] = "FAILED_CLOSED"
            result["blockers"] = [
                "reviewer_specialist_status:" + str(specialist_status),
                *[str(x) for x in reviewed.get("unresolved_items", []) if isinstance(x, str)],
            ]
        else:
            result["status"] = verdict
            result["ready_to_activate"] = bool(ready and verdict == "PASS" and not blockers and not required_changes)
            result["blockers"] = blockers
            result["required_changes"] = required_changes

        result["findings"] = [str(x) for x in reviewed.get("findings", []) if isinstance(x, str)]
        result["confidence"] = reviewed.get("confidence")
        result["model"] = reviewed.get("model")
        result["bridge_diagnostics"] = dict(reviewed.get("bridge_diagnostics") or {})
        result["reviewer_rationale"] = conclusion.get("rationale")
        result["reviewer_evidence"] = [str(x) for x in reviewed.get("evidence", []) if isinstance(x, str)]
        result["reviewer_unresolved_items"] = [
            str(x) for x in reviewed.get("unresolved_items", []) if isinstance(x, str)
        ]
    except Exception as exc:
        result["blockers"] = [f"review_probe_error:{type(exc).__name__}:{str(exc)}"]

    print(
        "JAYTEC_GOD_PROJECT_REVIEW_RESULT="
        + json.dumps(result, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _run_deepseek_security_review_probe() -> None:
    """STAGING-ONLY hash-pinned DeepSeek adversarial security review."""
    enabled = (
        os.environ.get("JAYTEC_DEEPSEEK_SECURITY_REVIEW_ENABLED", "").strip()
        == "1"
    )
    if not enabled:
        return

    encoded = os.environ.get(
        "JAYTEC_DEEPSEEK_SECURITY_REVIEW_PACKET_B64", ""
    ).strip()
    expected_sha = os.environ.get(
        "JAYTEC_DEEPSEEK_SECURITY_REVIEW_PACKET_SHA256", ""
    ).strip()
    if not _startup_probe_authorized("deepseek_security_review", expected_sha):
        return

    out = {
        "status": "FAILED_CLOSED",
        "model": DEEPSEEK_REVIEWER_MODEL,
        "packet_sha256": None,
        "verdict": None,
        "blockers": [],
        "required_changes": [],
        "weaknesses": [],
        "findings": [],
        "evidence": [],
        "confidence": None,
        "bridge_diagnostics": {},
    }

    try:
        if not encoded or not expected_sha:
            raise RuntimeError("deepseek_review_packet_missing")
        raw = base64.b64decode(encoded, validate=True)
        packet_sha = hashlib.sha256(raw).hexdigest()
        out["packet_sha256"] = packet_sha
        if packet_sha != expected_sha:
            raise RuntimeError("deepseek_review_packet_hash_mismatch")

        packet = json.loads(raw.decode("utf-8"))
        if not isinstance(packet, dict):
            raise RuntimeError("deepseek_review_packet_not_object")

        reviewed = dict(DEEPSEEK_REVIEW_DISPATCH(packet))
        if reviewed.get("model") != DEEPSEEK_REVIEWER_MODEL:
            raise RuntimeError("deepseek_review_model_mismatch")
        if reviewed.get("side_effects_attempted") not in ([], None):
            raise RuntimeError("deepseek_review_side_effect_violation")
        if reviewed.get("requested_operations") not in ([], None):
            raise RuntimeError("deepseek_review_requested_operations_nonempty")

        conclusion = reviewed.get("conclusion")
        if not isinstance(conclusion, dict):
            raise RuntimeError("deepseek_review_conclusion_not_object")

        verdict = conclusion.get("verdict")
        blockers = conclusion.get("blockers")
        required_changes = conclusion.get("required_changes")
        weaknesses = conclusion.get("weaknesses")
        rationale = conclusion.get("rationale")

        if verdict not in {"PASS", "PASS_WITH_CHANGES", "FAIL"}:
            raise RuntimeError("deepseek_review_verdict_invalid")
        for name, value in (
            ("blockers", blockers),
            ("required_changes", required_changes),
            ("weaknesses", weaknesses),
        ):
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise RuntimeError("deepseek_review_" + name + "_invalid")
        if not isinstance(rationale, str) or not rationale.strip():
            raise RuntimeError("deepseek_review_rationale_invalid")

        if reviewed.get("status") != "SUCCESS":
            out["status"] = "FAILED_CLOSED"
            out["blockers"] = [
                "deepseek_specialist_status:" + str(reviewed.get("status"))
            ] + [
                str(item)
                for item in reviewed.get("unresolved_items", [])
                if isinstance(item, str)
            ]
        else:
            out["status"] = verdict
            out["blockers"] = blockers
            out["required_changes"] = required_changes
            out["weaknesses"] = weaknesses

        out["verdict"] = verdict
        out["rationale"] = rationale
        out["findings"] = [
            str(item)
            for item in reviewed.get("findings", [])
            if isinstance(item, str)
        ]
        out["evidence"] = [
            str(item)
            for item in reviewed.get("evidence", [])
            if isinstance(item, str)
        ]
        out["confidence"] = reviewed.get("confidence")
        out["bridge_diagnostics"] = dict(
            reviewed.get("bridge_diagnostics") or {}
        )
    except Exception as exc:
        out["blockers"] = [
            "deepseek_review_probe_error:" + type(exc).__name__
        ]

    print(
        "JAYTEC_DEEPSEEK_SECURITY_REVIEW_RESULT="
        + json.dumps(out, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _run_deepseek_transport_matrix_probe() -> None:
    """STAGING-ONLY transport compatibility probe for the exact DeepSeek reviewer model.

    Logs only case names, success/failure status, HTTP/provider error class and
    returned model identity. It never logs prompts, responses, API keys, or secrets.
    """
    enabled = (
        os.environ.get("JAYTEC_DEEPSEEK_TRANSPORT_MATRIX_ENABLED", "").strip()
        == "1"
    )
    if not enabled:
        return
    transport_digest = sha256_text(
        "deepseek_transport_matrix:v1:" + DEEPSEEK_REVIEWER_MODEL
    )
    if not _startup_probe_authorized(
        "deepseek_transport_matrix",
        transport_digest,
    ):
        return

    cases = [
        {
            "name": "json_schema_require_params_reasoning",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "jaytec_transport_probe",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                },
            },
            "provider": {"allow_fallbacks": False, "require_parameters": True},
            "reasoning": {"effort": "low", "exclude": True},
        },
        {
            "name": "json_schema_no_require_params_reasoning",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "jaytec_transport_probe",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                },
            },
            "provider": {"allow_fallbacks": False},
            "reasoning": {"effort": "low", "exclude": True},
        },
        {
            "name": "json_schema_no_require_params_no_reasoning",
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "jaytec_transport_probe",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                },
            },
            "provider": {"allow_fallbacks": False},
            "reasoning": None,
        },
        {
            "name": "json_object_no_require_params_no_reasoning",
            "response_format": {"type": "json_object"},
            "provider": {"allow_fallbacks": False},
            "reasoning": None,
        },
    ]

    results = []
    if OPENROUTER_CLIENT is None:
        results.append(
            {
                "case": "setup",
                "status": "FAILED_CLOSED",
                "error_class": "OPENROUTER_NOT_CONFIGURED",
                "model": DEEPSEEK_REVIEWER_MODEL,
            }
        )
    else:
        for case in cases:
            item = {
                "case": case["name"],
                "status": "FAILED_CLOSED",
                "error_class": None,
                "model": DEEPSEEK_REVIEWER_MODEL,
                "returned_model": None,
            }
            try:
                extra_body = {"provider": case["provider"]}
                if case["reasoning"] is not None:
                    extra_body["reasoning"] = case["reasoning"]
                response = OPENROUTER_CLIENT.chat.completions.create(
                    model=DEEPSEEK_REVIEWER_MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "Return exactly one JSON object matching the requested schema. "
                                "Set ok to true."
                            ),
                        }
                    ],
                    temperature=0,
                    max_tokens=128,
                    timeout=30,
                    stream=False,
                    response_format=case["response_format"],
                    extra_body=extra_body,
                )
                returned_model = getattr(response, "model", None)
                item["returned_model"] = returned_model
                if returned_model not in (None, DEEPSEEK_REVIEWER_MODEL):
                    item["error_class"] = "MODEL_MISMATCH"
                elif not getattr(response, "choices", None):
                    item["error_class"] = "NO_CHOICES"
                else:
                    content = response.choices[0].message.content or ""
                    parsed = json.loads(content)
                    if parsed == {"ok": True}:
                        item["status"] = "SUCCESS"
                    else:
                        item["error_class"] = "UNEXPECTED_PAYLOAD"
            except Exception as exc:
                item["error_class"] = type(exc).__name__
            results.append(item)

    print(
        "JAYTEC_DEEPSEEK_TRANSPORT_MATRIX_RESULT="
        + json.dumps(
            {
                "model": DEEPSEEK_REVIEWER_MODEL,
                "results": results,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def _run_deepseek_route_visibility_probe() -> None:
    """STAGING-ONLY metadata probe for reviewer model visibility.

    Logs only the configured OpenRouter host, exact model identity, visibility,
    model count, and error class. It never logs API keys, prompts, responses,
    provider secrets, or account metadata.
    """
    enabled = (
        os.environ.get("JAYTEC_DEEPSEEK_ROUTE_VISIBILITY_ENABLED", "").strip()
        == "1"
    )
    if not enabled:
        return
    visibility_digest = sha256_text(
        "deepseek_route_visibility:v1:"
        + DEEPSEEK_REVIEWER_MODEL
        + ":"
        + OPENROUTER_BASE_URL
    )
    if not _startup_probe_authorized(
        "deepseek_route_visibility",
        visibility_digest,
    ):
        return

    parsed = urlparse(OPENROUTER_BASE_URL)
    out = {
        "status": "FAILED_CLOSED",
        "model": DEEPSEEK_REVIEWER_MODEL,
        "base_host": parsed.netloc or None,
        "model_visible": False,
        "models_count": None,
        "visible_deepseek_models": [],
        "error_class": None,
    }

    if OPENROUTER_CLIENT is None:
        out["error_class"] = "OPENROUTER_NOT_CONFIGURED"
    else:
        try:
            page = OPENROUTER_CLIENT.models.list()
            data = list(getattr(page, "data", []) or [])
            model_ids = [
                getattr(item, "id", None)
                for item in data
                if isinstance(getattr(item, "id", None), str)
            ]
            out["models_count"] = len(model_ids)
            out["visible_deepseek_models"] = sorted(
                model_id
                for model_id in model_ids
                if model_id.startswith("deepseek/")
            )[:50]
            out["model_visible"] = DEEPSEEK_REVIEWER_MODEL in model_ids
            out["status"] = "SUCCESS" if out["model_visible"] else "MODEL_NOT_VISIBLE"
        except Exception as exc:
            out["error_class"] = type(exc).__name__

    print(
        "JAYTEC_DEEPSEEK_ROUTE_VISIBILITY_RESULT="
        + json.dumps(out, ensure_ascii=False, sort_keys=True),
        flush=True,
    )


def _run_independent_g1_review_server_oneshot() -> None:
    """Run the zero-paid independent G1 reviewer after the HTTP server starts.

    The normal sequential startup reviewer stays disabled so a slow free-model
    response cannot prevent Render from detecting the service port. This
    one-shot launches the exact same hash-pinned reviewer in a child process
    while the staging server remains available. The child inherits the packet,
    model allowlist and hash pin, but receives an explicit local enable flag.
    """

    if (
        os.environ.get(
            "JAYTEC_G1_INDEPENDENT_REVIEW_SERVER_ONESHOT",
            "0",
        ).strip()
        != "1"
    ):
        return

    expected_sha = os.environ.get(
        "JAYTEC_G1_REVIEW_PACKET_SHA256", ""
    ).strip()
    if not _startup_probe_authorized(
        "independent_g1_review",
        expected_sha,
    ):
        return

    child_env = dict(os.environ)
    child_env["JAYTEC_G1_INDEPENDENT_REVIEW_ENABLED"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "staging_independent_g1_review.py"],
            env=child_env,
            check=False,
            timeout=240,
        )
        print(
            "JAYTEC_G1_INDEPENDENT_REVIEW_SERVER_ONESHOT="
            + json.dumps(
                {
                    "status": (
                        "COMPLETE"
                        if completed.returncode == 0
                        else "FAILED_CLOSED"
                    ),
                    "returncode": completed.returncode,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    except subprocess.TimeoutExpired:
        print(
            "JAYTEC_G1_INDEPENDENT_REVIEW_SERVER_ONESHOT="
            + json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "reason": "WRAPPER_TIMEOUT",
                },
                sort_keys=True,
            ),
            flush=True,
        )
    except Exception as exc:
        print(
            "JAYTEC_G1_INDEPENDENT_REVIEW_SERVER_ONESHOT="
            + json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "reason": type(exc).__name__,
                },
                sort_keys=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    threading.Thread(
        target=_run_jaytec_read_bootstrap_probe,
        name="jaytec-read-bootstrap-oneshot",
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_independent_g1_review_server_oneshot,
        name="jaytec-independent-g1-review-oneshot",
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_god_project_review_probe,
        name="jaytec-god-project-review",
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_deepseek_security_review_probe,
        name="jaytec-deepseek-security-review",
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_deepseek_transport_matrix_probe,
        name="jaytec-deepseek-transport-matrix",
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_deepseek_route_visibility_probe,
        name="jaytec-deepseek-route-visibility",
        daemon=True,
    ).start()
    http_policy = load_host_origin_policy(os.environ, require_hosts=True)
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=list(http_policy.allowed_hosts),
        allowed_origins=list(http_policy.allowed_origins),
    )
