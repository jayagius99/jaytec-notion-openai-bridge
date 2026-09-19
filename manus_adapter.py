"""STAGING ONLY: bounded Manus API v2 adapter for JAYTEC.

Every Manus execution must pass the composed JAYTEC dispatch contract:
- exact MANUS project identity;
- explicit task-scoped connector binding from the GitHub/Neon/Render allowlist;
- explicit Lite profile selection;
- current-task authority;
- post-dispatch observed-profile verification.

No implicit project/user connector inheritance is allowed.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from manus_dispatch_contract import (
    ManusDispatchAuthorization,
    ManusDispatchContractError,
    authorize_manus_dispatch,
    verify_manus_dispatch_result,
)
from manus_governance import AuthoritySource, ManusScope, render_directive
from manus_policy import ManusProfilePolicyError, verify_manus_profile
from participant_contracts import render_actor_contract
from relationship_policy import Actor, MUTATING, Purpose, authorize_relationship

MANUS_BASE_URL = os.environ.get("MANUS_BASE_URL", "https://api.manus.ai/v2").rstrip("/")
MANUS_API_KEY = os.environ.get("MANUS_API_KEY", "").strip()
MANUS_TIMEOUT_S = float(os.environ.get("MANUS_TIMEOUT_S", "20"))
MANUS_MAX_RETRIES = min(max(int(os.environ.get("MANUS_MAX_RETRIES", "1")), 0), 3)
MANUS_MAX_RESPONSE_BYTES = min(max(int(os.environ.get("MANUS_MAX_RESPONSE_BYTES", "1048576")), 4096), 4 * 1024 * 1024)
MANUS_MAX_MESSAGE_CHARS = min(max(int(os.environ.get("MANUS_MAX_MESSAGE_CHARS", "6000")), 1), 10000)
JAYTEC_MANUS_PROJECT_ID = os.environ.get("JAYTEC_MANUS_PROJECT_ID", "").strip()

APPROVED_CONNECTOR_KEYS = ("github", "neon", "render")
_CONNECTOR_ALIASES = {
    "github": "github",
    "github connect": "github",
    "neon": "neon",
    "neon connect": "neon",
    "render": "render",
    "render connect": "render",
}


class ManusError(RuntimeError):
    pass


class ManusInsufficientCredits(ManusError):
    """Vendor quota/usage signal.

    Manus Lite is treated by JAYTEC as a free profile. Callers must not convert
    this API signal into a monetary top-up instruction for Lite.
    """


@dataclass(frozen=True)
class ManusResponse:
    endpoint: str
    status_code: int
    body: Mapping[str, Any]
    request_id: str | None = None
    credit_usage: float | int | None = None


@dataclass(frozen=True)
class BoundManusRoute:
    authorization: ManusDispatchAuthorization
    connector_ids: tuple[str, ...]
    connector_permissions: tuple[tuple[str, str], ...] = ()


def _error_code(parsed: Mapping[str, Any]) -> str | None:
    error = parsed.get("error")
    if isinstance(error, Mapping) and error.get("code") is not None:
        return str(error.get("code"))
    if parsed.get("code") is not None:
        return str(parsed.get("code"))
    return None


def _credit_usage(parsed: Mapping[str, Any]) -> float | int | None:
    for candidate in (parsed, parsed.get("data"), parsed.get("task")):
        if isinstance(candidate, Mapping):
            value = candidate.get("credit_usage")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def _is_credit_failure(parsed: Mapping[str, Any]) -> bool:
    if _error_code(parsed) == "10091":
        return True
    text = json.dumps(parsed, ensure_ascii=False).lower()
    return "not enough credits" in text or "insufficient credits" in text


def _rows(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = body.get("data")
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _task(body: Mapping[str, Any]) -> Mapping[str, Any]:
    value = body.get("task")
    if isinstance(value, Mapping):
        return value
    value = body.get("data")
    return value if isinstance(value, Mapping) else {}


def _normalize_connector_name(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


class ManusClient:
    def __init__(self, api_key: str = MANUS_API_KEY, base_url: str = MANUS_BASE_URL) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        if not self._api_key:
            raise ManusError("MANUS_API_KEY_NOT_CONFIGURED")

    @staticmethod
    def _parse_json(raw: bytes) -> Mapping[str, Any]:
        if len(raw) > MANUS_MAX_RESPONSE_BYTES:
            raise ManusError("MANUS_RESPONSE_TOO_LARGE")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManusError("MANUS_RESPONSE_INVALID_JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ManusError("MANUS_RESPONSE_NOT_OBJECT")
        return parsed

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(10.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(5.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.25)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ManusResponse:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}/{endpoint}" + (f"?{query}" if query else "")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "x-manus-api-key": self._api_key,
            "User-Agent": "JAYTEC-Manus-Door/2",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        attempts = MANUS_MAX_RETRIES + 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=MANUS_TIMEOUT_S) as resp:
                    raw = resp.read(MANUS_MAX_RESPONSE_BYTES + 1)
                    parsed = self._parse_json(raw)
                    if _is_credit_failure(parsed):
                        raise ManusInsufficientCredits("MANUS_INSUFFICIENT_CREDITS")
                    if parsed.get("ok") is False:
                        raise ManusError(f"MANUS_API_ERROR:{_error_code(parsed) or 'unknown'}")
                    return ManusResponse(
                        endpoint=endpoint,
                        status_code=int(resp.status),
                        body=parsed,
                        request_id=resp.headers.get("x-request-id") or resp.headers.get("request-id"),
                        credit_usage=_credit_usage(parsed),
                    )
            except urllib.error.HTTPError as exc:
                last = exc
                try:
                    parsed = self._parse_json(exc.read(MANUS_MAX_RESPONSE_BYTES + 1))
                except ManusError:
                    parsed = {}
                if _is_credit_failure(parsed):
                    raise ManusInsufficientCredits("MANUS_INSUFFICIENT_CREDITS") from exc
                if exc.code != 429 or attempt + 1 >= attempts:
                    raise ManusError(f"MANUS_HTTP_{exc.code}:{_error_code(parsed) or 'unknown'}") from exc
                time.sleep(self._retry_delay(attempt, exc.headers.get("Retry-After")))
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt + 1 >= attempts:
                    raise ManusError("MANUS_TRANSPORT_ERROR") from exc
                time.sleep(self._retry_delay(attempt, None))
        raise ManusError("MANUS_REQUEST_FAILED") from last

    def user_me(self) -> Mapping[str, Any]:
        return self._request("GET", "user.me").body

    def list_projects(self) -> Mapping[str, Any]:
        return self._request("GET", "project.list").body

    def list_connectors(self) -> Mapping[str, Any]:
        return self._request("GET", "connector.list").body

    def list_tasks(self, *, project_id: str | None = None, limit: int = 100) -> Mapping[str, Any]:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 100), "order": "desc"}
        if project_id:
            params.update({"scope": "project", "project_id": project_id})
        return self._request("GET", "task.list", params=params).body

    def task_detail(self, task_id: str) -> Mapping[str, Any]:
        return self._request("GET", "task.detail", params={"task_id": task_id}).body

    def list_messages(self, task_id: str, *, limit: int = 50) -> Mapping[str, Any]:
        return self._request(
            "GET",
            "task.listMessages",
            params={"task_id": task_id, "order": "desc", "limit": min(max(limit, 1), 200)},
        ).body

    def stop_task(self, task_id: str) -> Mapping[str, Any]:
        return self._request("POST", "task.stop", payload={"task_id": task_id}).body

    def resolve_manus_project(self) -> tuple[str, str]:
        matches = [
            row for row in _rows(self.list_projects())
            if str(row.get("name") or "").strip().casefold() == "manus"
        ]
        if len(matches) != 1:
            raise ManusError(
                "MANUS_PROJECT_NOT_FOUND" if not matches else "MANUS_PROJECT_AMBIGUOUS"
            )
        project_id = str(matches[0].get("id") or "").strip()
        if not project_id:
            raise ManusError("MANUS_PROJECT_ID_MISSING")
        if JAYTEC_MANUS_PROJECT_ID and project_id != JAYTEC_MANUS_PROJECT_ID:
            raise ManusError("MANUS_PROJECT_ID_MISMATCH")
        return project_id, str(matches[0].get("name") or "")

    def resolve_approved_connector_ids(
        self,
        requested_keys: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        canonical: list[str] = []
        for raw in requested_keys:
            key = _CONNECTOR_ALIASES.get(_normalize_connector_name(raw))
            if key is None or key not in APPROVED_CONNECTOR_KEYS:
                raise ManusError("MANUS_CONNECTOR_NOT_ALLOWLISTED")
            if key not in canonical:
                canonical.append(key)

        if not canonical:
            return (), ()

        by_key: dict[str, list[str]] = {key: [] for key in canonical}
        for row in _rows(self.list_connectors()):
            normalized = _normalize_connector_name(row.get("name"))
            key = _CONNECTOR_ALIASES.get(normalized)
            if key not in by_key:
                continue
            connector_id = str(row.get("id") or "").strip()
            if connector_id:
                by_key[key].append(connector_id)

        missing = [key for key, values in by_key.items() if not values]
        ambiguous = [key for key, values in by_key.items() if len(values) > 1]
        if missing:
            raise ManusError("MANUS_APPROVED_CONNECTOR_MISSING:" + ",".join(missing))
        if ambiguous:
            raise ManusError("MANUS_APPROVED_CONNECTOR_AMBIGUOUS:" + ",".join(ambiguous))

        names = tuple(canonical)
        ids = tuple(by_key[key][0] for key in names)
        return names, ids

    def prepare_route(
        self,
        *,
        scope: str | ManusScope,
        authority_source: str | AuthoritySource,
        current_task_authorized: bool,
        requested_profile: str = "lite",
        requested_connector_purposes: Mapping[str, str | Purpose] | None = None,
        connector_mutation_authorized: bool = False,
        notion_authorized_by_jay_via_chatgpt: bool = False,
    ) -> BoundManusRoute:
        if type(connector_mutation_authorized) is not bool:
            raise ManusError("MANUS_CONNECTOR_MUTATION_AUTH_FLAG_INVALID")
        project_id, project_name = self.resolve_manus_project()
        requested = dict(requested_connector_purposes or {})
        canonical_purposes: list[tuple[str, str]] = []
        actor_for = {
            "github": Actor.GITHUB,
            "neon": Actor.NEON,
            "render": Actor.RENDER,
        }
        for raw_name, raw_purpose in requested.items():
            key = _CONNECTOR_ALIASES.get(_normalize_connector_name(raw_name))
            if key is None or key not in actor_for:
                raise ManusError("MANUS_CONNECTOR_NOT_ALLOWLISTED")
            try:
                purpose = raw_purpose if isinstance(raw_purpose, Purpose) else Purpose(str(raw_purpose).strip().casefold())
            except ValueError as exc:
                raise ManusError("MANUS_CONNECTOR_PURPOSE_INVALID") from exc
            if purpose in MUTATING:
                source_value = (
                    authority_source.value
                    if isinstance(authority_source, AuthoritySource)
                    else str(authority_source).strip().casefold()
                )
                if (
                    not connector_mutation_authorized
                    or source_value not in {
                        AuthoritySource.JAY.value,
                        AuthoritySource.CHATGPT.value,
                    }
                ):
                    raise ManusError("MANUS_CONNECTOR_MUTATION_AUTH_REQUIRED")
            authorize_relationship(
                source=Actor.MANUS,
                destination=actor_for[key],
                purpose=purpose,
                current_task_authorized=(
                    current_task_authorized
                    and (purpose not in MUTATING or connector_mutation_authorized)
                ),
            )
            canonical_purposes.append((key, purpose.value))

        connector_names, connector_ids = self.resolve_approved_connector_ids(
            [name for name, _ in canonical_purposes]
        )
        expected_project_id = JAYTEC_MANUS_PROJECT_ID or project_id

        authorization = authorize_manus_dispatch(
            project_id=project_id,
            expected_project_id=expected_project_id,
            project_name=project_name,
            connectors=list(connector_names),
            connectors_explicit=True,
            scope=scope,
            authority_source=authority_source,
            current_task_authorized=current_task_authorized,
            requested_profile=requested_profile,
            route_supports_profile_selector=True,
            notion_authorized_by_jay_via_chatgpt=notion_authorized_by_jay_via_chatgpt,
        )
        return BoundManusRoute(
            authorization=authorization,
            connector_ids=connector_ids,
            connector_permissions=tuple(canonical_purposes),
        )

    @staticmethod
    def _governed_message(route: BoundManusRoute, content: str) -> str:
        raw = content.strip()
        if not raw:
            raise ManusError("EMPTY_MESSAGE")
        if len(raw) > MANUS_MAX_MESSAGE_CHARS:
            raise ManusError("MESSAGE_TOO_LARGE")
        connector_scope = (
            ", ".join(f"{name}:{purpose}" for name, purpose in route.connector_permissions)
            if route.connector_permissions
            else "NONE — no connector use is authorized for this task"
        )
        return (
            render_actor_contract(Actor.MANUS)
            + "\n\nCANONICAL MANUS DIRECTIVE\n"
            + render_directive()
            + "\n\nCURRENT TASK CONNECTOR SCOPE\n"
            + connector_scope
            + "\nUse no connector or connector operation outside that exact scope. "
              "Connector availability never expands authority.\n"
            + "\nCURRENT DELEGATED TASK\n"
            + raw
        )

    def create_task(
        self,
        route: BoundManusRoute,
        content: str,
        *,
        title: str | None = None,
        structured_output_schema: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        message = self._governed_message(route, content)

        payload: dict[str, Any] = {
            "message": {
                "content": message,
                "connectors": list(route.connector_ids),
            },
            "project_id": route.authorization.project_id,
            "agent_profile": route.authorization.profile.requested_profile.value,
            "interactive_mode": False,
            "share_visibility": "private",
        }
        if title:
            payload["title"] = str(title)[:200]
        if structured_output_schema is not None:
            payload["structured_output_schema"] = dict(structured_output_schema)

        created = self._request("POST", "task.create", payload=payload).body
        task_id = str(created.get("task_id") or "").strip()
        if not task_id:
            raise ManusError("MANUS_TASK_ID_MISSING")

        # Verify what Manus actually used, not merely what JAYTEC requested.
        try:
            self.verify_task_profile(route, task_id)
        except (ManusProfilePolicyError, ManusError):
            try:
                self.stop_task(task_id)
            finally:
                raise
        return created

    def send_message(
        self,
        route: BoundManusRoute,
        task_id: str,
        content: str,
        *,
        structured_output_schema: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        message = self._governed_message(route, content)

        # Old tasks that are not observably Lite are not continued.
        self.verify_task_profile(route, task_id)

        payload: dict[str, Any] = {
            "task_id": task_id,
            "message": {"content": message},
            # sendMessage explicitly supports a per-turn profile override.
            "agent_profile": route.authorization.profile.requested_profile.value,
        }
        if route.connector_ids:
            payload["message"]["connectors"] = list(route.connector_ids)
        else:
            # Empty sendMessage connectors would reuse old connectors.
            payload["clear_connectors"] = True
        if structured_output_schema is not None:
            payload["structured_output_schema"] = dict(structured_output_schema)

        result = self._request("POST", "task.sendMessage", payload=payload).body
        self.verify_task_profile(route, task_id)
        return result

    def verify_completed_result(
        self,
        route: BoundManusRoute,
        task_id: str,
        result: Mapping[str, object],
    ) -> Mapping[str, Any]:
        """Run the composed post-dispatch gate on evidence-backed completion."""
        detail = self.verify_task_profile(route, task_id)
        task = _task(detail)
        observed = task.get("agent_profile")
        verify_manus_dispatch_result(
            route.authorization,
            observed_profile=str(observed) if observed is not None else None,
            result=result,
        )
        return detail

    def verify_task_profile(
        self,
        route: BoundManusRoute,
        task_id: str,
        *,
        attempts: int = 3,
        delay_seconds: float = 0.5,
    ) -> Mapping[str, Any]:
        """Verify observed Manus profile with a short bounded propagation window.

        A concrete mismatch fails immediately. Missing profile identity is
        retried only as a read-only propagation allowance, then fails closed.
        """
        bounded_attempts = min(max(int(attempts), 1), 5)
        last_detail: Mapping[str, Any] = {}
        for attempt in range(bounded_attempts):
            detail = self.task_detail(task_id)
            last_detail = detail
            task = _task(detail)
            project_id = task.get("project_id")
            if (
                project_id not in (None, "")
                and str(project_id) != route.authorization.project_id
            ):
                raise ManusError("MANUS_TASK_PROJECT_MISMATCH")

            observed = task.get("agent_profile")
            if observed not in (None, ""):
                verify_manus_profile(
                    route.authorization.profile,
                    observed_profile=str(observed),
                )
                return detail

            if attempt + 1 < bounded_attempts:
                time.sleep(max(0.0, min(float(delay_seconds), 2.0)))

        # Reuse canonical policy error for unobservable identity.
        verify_manus_profile(
            route.authorization.profile,
            observed_profile=None,
        )
        return last_detail


def safe_identity_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    data = body.get("data") if isinstance(body.get("data"), Mapping) else body.get("user")
    if not isinstance(data, Mapping):
        data = {}
    return {
        "authenticated": bool(body.get("ok", True)),
        "user_id": data.get("id"),
        "display_name": data.get("name") or data.get("display_name"),
    }


def safe_task_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    task = _task(body)
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "title": task.get("title"),
        "project_id": task.get("project_id"),
        "agent_profile": task.get("agent_profile"),
        "credit_usage": task.get("credit_usage"),
        "task_url": task.get("task_url"),
    }
