"""JAYTEC meeting-only specialist bus.

This module exposes no general specialist API. It accepts only authenticated
GitHub Actions requests from the JAYTEC meeting-ledger branch and only permits
advisory Gemini/Sol calls for a named meeting. Notion and ChatGPT Work are not
part of this route.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Mapping, Optional

import jwt
from jwt import PyJWKClient
from openai import OpenAI

from idempotency_postgres import PostgresExecutionRegistry
from orchestration import ExecutionRegistry
from worker_json import WorkerJsonError, json_object_with_diagnostics

MEETING_SCOPE = "JAYTEC_MEETING_ONLY"
MEETING_ROUTE = "DIRECT_GITHUB_OIDC_MEETING_BUS"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
OIDC_AUDIENCE = os.environ.get("MEETING_BUS_OIDC_AUDIENCE", "jaytec-meeting-bus").strip()

EXPECTED_REPOSITORY = os.environ.get(
    "MEETING_BUS_GITHUB_REPOSITORY", "jayagius99/jaytec-work-engine-v2-g1"
).strip()
EXPECTED_REF = os.environ.get(
    "MEETING_BUS_GITHUB_REF", "refs/heads/ops/meeting-ledger-v1"
).strip()
EXPECTED_WORKFLOW_PATH = ".github/workflows/meeting-specialist-bus.yml"

SOL_MODEL = os.environ.get("MEETING_SOL_MODEL", "gpt-5.6-sol").strip()
GEMINI_MODEL = os.environ.get(
    "MEETING_GEMINI_MODEL", "google/gemini-3.1-pro-preview"
).strip()
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).strip()

MEETING_BUS_ENABLED = os.environ.get(
    "MEETING_SPECIALIST_BUS_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
SOL_ENABLED = os.environ.get("MEETING_SOL_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
SOL_RESERVE_MODE = os.environ.get(
    "OPENAI_API_ENGINEERING_RESERVE_DOOR", "LOCKED_RESERVE"
).strip().upper()
SOL_OUTPUT_TOKEN_CAP = int(os.environ.get("MEETING_SOL_OUTPUT_TOKEN_CAP", "1200"))
GEMINI_ENABLED = os.environ.get("MEETING_GEMINI_ENABLED", "1").strip().lower() in {
    "1", "true", "yes", "on"
}

MAX_BODY_BYTES = 65_536
MAX_BRIEF_CHARS = 20_000
MAX_ROLE_QUESTION_CHARS = 8_000
MAX_OUTPUT_TOKENS = 3_000
ALLOWED_PARTICIPANTS = frozenset({"gemini", "sol"})
ALLOWED_OPERATIONS = frozenset({"analyze", "review", "challenge"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")

_jwk_client = PyJWKClient(OIDC_JWKS_URL, cache_keys=True)


class MeetingBusError(RuntimeError):
    pass


class MeetingPolicyError(MeetingBusError):
    pass


class MeetingAuthError(MeetingBusError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_digest(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(request)).encode("utf-8")).hexdigest()


def validate_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    request = dict(raw)
    if request.get("schema_version") != "1.0":
        raise MeetingPolicyError("unsupported_schema_version")
    if request.get("scope") != MEETING_SCOPE:
        raise MeetingPolicyError("meeting_scope_required")

    for key in ("meeting_id", "request_id"):
        value = request.get(key)
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise MeetingPolicyError("invalid_" + key)

    participant = request.get("participant")
    if participant not in ALLOWED_PARTICIPANTS:
        raise MeetingPolicyError("unsupported_participant")

    if request.get("side_effect_policy") != "none":
        raise MeetingPolicyError("side_effects_forbidden")

    operations = request.get("allowed_operations")
    if (
        not isinstance(operations, list)
        or not operations
        or len(set(operations)) != len(operations)
        or any(op not in ALLOWED_OPERATIONS for op in operations)
    ):
        raise MeetingPolicyError("meeting_operations_only")

    brief = request.get("brief")
    if not isinstance(brief, str) or not brief.strip() or len(brief) > MAX_BRIEF_CHARS:
        raise MeetingPolicyError("invalid_brief")

    role_question = request.get("role_question", "")
    if (
        not isinstance(role_question, str)
        or len(role_question) > MAX_ROLE_QUESTION_CHARS
    ):
        raise MeetingPolicyError("invalid_role_question")

    max_output_tokens = request.get("max_output_tokens", 1800)
    if (
        type(max_output_tokens) is not int
        or max_output_tokens < 256
        or max_output_tokens > MAX_OUTPUT_TOKENS
    ):
        raise MeetingPolicyError("invalid_max_output_tokens")

    if request.get("notion_allowed") not in (None, False):
        raise MeetingPolicyError("notion_forbidden")
    if request.get("work_allowed") not in (None, False):
        raise MeetingPolicyError("chatgpt_work_forbidden")

    return request


def verify_github_oidc(token: str, transport: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(token, str) or not token.strip():
        raise MeetingAuthError("missing_bearer_token")
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=OIDC_AUDIENCE,
            issuer=OIDC_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "repository", "ref", "sha"]},
        )
    except Exception as exc:
        raise MeetingAuthError("invalid_github_oidc:" + type(exc).__name__) from exc

    repository = str(claims.get("repository", ""))
    ref = str(claims.get("ref", ""))
    sha = str(claims.get("sha", ""))
    workflow_ref = str(claims.get("workflow_ref", ""))
    event_name = str(claims.get("event_name", ""))

    expected_workflow_ref = (
        EXPECTED_REPOSITORY + "/" + EXPECTED_WORKFLOW_PATH + "@" + EXPECTED_REF
    )
    if repository != EXPECTED_REPOSITORY:
        raise MeetingAuthError("repository_not_authorized")
    if ref != EXPECTED_REF:
        raise MeetingAuthError("branch_not_authorized")
    if event_name != "push":
        raise MeetingAuthError("event_not_authorized")
    if workflow_ref != expected_workflow_ref:
        raise MeetingAuthError("workflow_not_authorized")

    transport_sha = str(transport.get("commit_sha", ""))
    if not transport_sha or transport_sha != sha:
        raise MeetingAuthError("transport_sha_mismatch")

    return {
        "repository": repository,
        "ref": ref,
        "sha": sha,
        "workflow_ref": workflow_ref,
        "run_id": str(transport.get("run_id", "")),
    }


def _participant_prompt(request: Mapping[str, Any]) -> str:
    return """JAYTEC MEETING PARTICIPANT CONTRACT

You are participating in exactly one JAYTEC system meeting as an advisory
specialist. Jay is owner/root authority and ChatGPT/OpenAI Lead is the sole
JAYTEC coordinator/controller for specialist work. You are a subordinate
specialist only. Work only on the exact meeting question ChatGPT supplied.
You have no execution authority and no permission to mutate JAYTEC. Do not
self-initiate JAYTEC work, broaden scope, create follow-on tasks, approve your
own recommendations, or decide that a JAYTEC change should be applied. Do not
call tools, browse, write files, modify code, contact services, or take side
effects. Return advice/evidence only to ChatGPT. Do not use or request Notion,
Notion Agent, Custom Agents, or ChatGPT Work. Challenge weak claims instead of
agreeing automatically.

Return ONLY one JSON object with these fields:
status: SUCCESS | PARTIAL_SUCCESS | FAILED_CLOSED
current_state_observations: array of strings
evidence: array of strings
risks: array of strings
disagreements_or_challenges: array of strings
recommended_next_focus: array of strings
cost_efficiency_observations: array of strings
unresolved_questions: array of strings
confidence: string
side_effects_attempted: [] exactly

MEETING_ID: %s
PARTICIPANT: %s
ALLOWED_OPERATIONS: %s

CANONICAL MEETING BRIEF:
%s

ROLE-SPECIFIC QUESTION:
%s
""" % (
        request["meeting_id"],
        request["participant"],
        _canonical_json(request["allowed_operations"]),
        request["brief"],
        request.get("role_question", ""),
    )


def _usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            result[name] = value
    return result


def _normalize_output(participant: str, model: str, raw_text: str) -> dict[str, Any]:
    try:
        parsed, diagnostics = json_object_with_diagnostics(raw_text or "")
    except WorkerJsonError as exc:
        raise MeetingBusError("invalid_specialist_json:" + exc.code) from exc

    if parsed.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS", "FAILED_CLOSED"}:
        raise MeetingBusError("invalid_specialist_status")

    array_fields = (
        "current_state_observations",
        "evidence",
        "risks",
        "disagreements_or_challenges",
        "recommended_next_focus",
        "cost_efficiency_observations",
        "unresolved_questions",
        "side_effects_attempted",
    )
    for field in array_fields:
        value = parsed.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise MeetingBusError("invalid_specialist_field:" + field)
    if parsed["side_effects_attempted"]:
        raise MeetingPolicyError("specialist_side_effect_attempt")
    if not isinstance(parsed.get("confidence"), str):
        raise MeetingBusError("invalid_specialist_field:confidence")

    return {
        "participant_id": participant,
        "model": model,
        "status": parsed["status"],
        "current_state_observations": parsed["current_state_observations"],
        "evidence": parsed["evidence"],
        "risks": parsed["risks"],
        "disagreements_or_challenges": parsed["disagreements_or_challenges"],
        "recommended_next_focus": parsed["recommended_next_focus"],
        "cost_efficiency_observations": parsed["cost_efficiency_observations"],
        "unresolved_questions": parsed["unresolved_questions"],
        "confidence": parsed["confidence"],
        "side_effects_attempted": [],
        "transport_parse": {
            "extracted_balanced_object": bool(diagnostics.extracted_object)
        },
    }


def _call_sol(request: Mapping[str, Any], client: Optional[OpenAI] = None):
    if not SOL_ENABLED:
        raise MeetingPolicyError("sol_meeting_lane_cost_locked")
    if SOL_RESERVE_MODE != "BOUNDED_SOL_ONLY":
        raise MeetingPolicyError("sol_reserve_door_not_bounded_open")
    if SOL_MODEL != "gpt-5.6-sol":
        raise MeetingPolicyError("sol_model_lock_mismatch")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if client is None and not api_key:
        raise MeetingBusError("sol_api_not_configured")
    client = client or OpenAI(api_key=api_key)
    try:
        response = client.responses.create(
            model=SOL_MODEL,
            input=_participant_prompt(request),
            reasoning={"effort": os.environ.get("MEETING_SOL_REASONING_EFFORT", "medium")},
            max_output_tokens=min(
                int(request.get("max_output_tokens", 1800)),
                SOL_OUTPUT_TOKEN_CAP,
            ),
            timeout=float(os.environ.get("MEETING_SOL_TIMEOUT_S", "90")),
        )
    except Exception as exc:
        raise MeetingBusError("sol_provider_error:" + type(exc).__name__) from exc
    returned_model = getattr(response, "model", None)
    if returned_model not in (None, SOL_MODEL):
        raise MeetingBusError("sol_provider_model_mismatch")
    return (
        _normalize_output("sol", SOL_MODEL, getattr(response, "output_text", "")),
        _usage(getattr(response, "usage", None)),
    )


def _call_gemini(request: Mapping[str, Any], client: Optional[OpenAI] = None):
    if not GEMINI_ENABLED:
        raise MeetingPolicyError("gemini_meeting_lane_disabled")
    if GEMINI_MODEL != "google/gemini-3.1-pro-preview":
        raise MeetingPolicyError("gemini_model_lock_mismatch")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if client is None and not api_key:
        raise MeetingBusError("gemini_api_not_configured")
    client = client or OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
    try:
        response = client.chat.completions.create(
            model=GEMINI_MODEL,
            messages=[{"role": "user", "content": _participant_prompt(request)}],
            temperature=0,
            max_tokens=int(request.get("max_output_tokens", 1800)),
            response_format={"type": "json_object"},
            extra_body={"provider": {"sort": "price", "allow_fallbacks": True}},
            timeout=float(os.environ.get("MEETING_GEMINI_TIMEOUT_S", "90")),
        )
    except Exception as exc:
        raise MeetingBusError("gemini_provider_error:" + type(exc).__name__) from exc
    choices = getattr(response, "choices", None)
    if not choices:
        raise MeetingBusError("gemini_no_choices")
    returned_model = getattr(response, "model", None)
    if returned_model not in (None, GEMINI_MODEL):
        raise MeetingBusError("gemini_provider_model_mismatch")
    return (
        _normalize_output("gemini", GEMINI_MODEL, choices[0].message.content or ""),
        _usage(getattr(response, "usage", None)),
    )


def _registry():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise MeetingBusError("durable_idempotency_unavailable")
    registry = PostgresExecutionRegistry(
        database_url=database_url,
        ttl_seconds=ExecutionRegistry().ttl_seconds,
    )
    registry.ensure_schema()
    return registry


def dispatch_request(
    raw_request: Mapping[str, Any],
    *,
    registry: Optional[Any] = None,
    sol_client: Optional[OpenAI] = None,
    gemini_client: Optional[OpenAI] = None,
    enabled: Optional[bool] = None,
) -> dict[str, Any]:
    request = validate_request(raw_request)
    if enabled is None:
        enabled = MEETING_BUS_ENABLED
    if not enabled:
        raise MeetingPolicyError("meeting_specialist_bus_disabled")

    registry = registry or _registry()
    digest = request_digest(request)
    idem_key = "meeting-bus:%s:%s" % (
        request["meeting_id"],
        request["participant"],
    )
    try:
        cached = registry.lookup(idem_key, digest)
    except ValueError as exc:
        if str(exc) == "CONFLICTING_DUPLICATE":
            raise MeetingPolicyError("conflicting_participant_request_for_meeting") from exc
        raise
    if cached is not None:
        replay = dict(cached)
        replay["idempotent_replay"] = True
        return replay

    if request["participant"] == "sol":
        specialist_result, usage = _call_sol(request, client=sol_client)
    else:
        specialist_result, usage = _call_gemini(request, client=gemini_client)

    result = {
        "schema_version": "1.0",
        "route": MEETING_ROUTE,
        "notion_used": False,
        "chatgpt_work_used": False,
        "meeting_id": request["meeting_id"],
        "request_id": request["request_id"],
        "participant": request["participant"],
        "request_sha256": digest,
        "provider_call_count": 1,
        "provider_usage": usage,
        "side_effects_attempted": [],
        "idempotent_replay": False,
        "result": specialist_result,
    }
    try:
        registry.store(idem_key, digest, result)
    except ValueError as exc:
        if str(exc) == "CONFLICTING_DUPLICATE":
            raise MeetingPolicyError("conflicting_participant_request_for_meeting") from exc
        raise
    return result


def status() -> dict[str, Any]:
    return {
        "route": MEETING_ROUTE,
        "enabled": MEETING_BUS_ENABLED,
        "meeting_scope": MEETING_SCOPE,
        "notion_in_path": False,
        "chatgpt_work_in_path": False,
        "expected_repository": EXPECTED_REPOSITORY,
        "expected_ref": EXPECTED_REF,
        "participants": {
            "sol": {
                "model": SOL_MODEL,
                "enabled": SOL_ENABLED,
                "configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                "reserve_mode": SOL_RESERVE_MODE,
                "output_token_cap": SOL_OUTPUT_TOKEN_CAP,
            },
            "gemini": {
                "model": GEMINI_MODEL,
                "enabled": GEMINI_ENABLED,
                "configured": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
            },
        },
        "max_provider_calls_per_participant_per_meeting": 1,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


class MeetingBusMiddleware:
    """Intercept the dedicated meeting-bus HTTP surface only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if path not in {"/meeting-bus/v1/call", "/meeting-bus/v1/status"}:
            return await self.app(scope, receive, send)

        try:
            header = self._header(scope, b"authorization")
            if not header or not header.lower().startswith(b"bearer "):
                raise MeetingAuthError("bearer_token_required")
            token = header.split(b" ", 1)[1].decode("utf-8")
            body = await self._read_body(receive)
            payload: dict[str, Any] = {}
            if body:
                decoded = json.loads(body.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise MeetingPolicyError("body_must_be_object")
                payload = decoded
            transport = payload.get("transport", {})
            if not isinstance(transport, Mapping):
                raise MeetingPolicyError("invalid_transport")
            caller = verify_github_oidc(token, transport)

            if path == "/meeting-bus/v1/status":
                return await self._json_response(
                    send, 200, {**status(), "caller_sha": caller["sha"]}
                )

            if scope.get("method") != "POST":
                return await self._json_response(
                    send, 405, {"ok": False, "error": "method_not_allowed"}
                )
            request = payload.get("request")
            if not isinstance(request, Mapping):
                raise MeetingPolicyError("request_object_required")
            result = dispatch_request(request)
            return await self._json_response(
                send,
                200,
                {
                    "ok": True,
                    "caller_sha": caller["sha"],
                    "caller_run_id": caller["run_id"],
                    **result,
                },
            )
        except MeetingAuthError as exc:
            return await self._json_response(send, 401, {"ok": False, "error": str(exc)})
        except MeetingPolicyError as exc:
            return await self._json_response(send, 403, {"ok": False, "error": str(exc)})
        except json.JSONDecodeError:
            return await self._json_response(
                send, 400, {"ok": False, "error": "invalid_json"}
            )
        except Exception as exc:
            return await self._json_response(
                send,
                502,
                {"ok": False, "error": "meeting_bus_failed:" + type(exc).__name__},
            )

    @staticmethod
    def _header(scope: Mapping[str, Any], name: bytes) -> Optional[bytes]:
        for key, value in scope.get("headers", []):
            if key.lower() == name.lower():
                return value
        return None

    @staticmethod
    async def _read_body(receive) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise MeetingPolicyError("request_body_too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    async def _json_response(send, http_status: int, payload: Mapping[str, Any]):
        body = _canonical_json(dict(payload)).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": http_status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
