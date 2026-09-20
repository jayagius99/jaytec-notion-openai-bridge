"""JAYTEC Protocol Portal: durable multi-chat task registration for WATCH.

The portal reuses the existing canonical JAYTEC job/event tables. It does not
create a second scheduler, queue, ledger, or canonical task store.

Normal ChatGPT UI chats remain non-callable workers. Registering a task keeps
its objective/checkpoints durable and WATCH-addressable; it does not fabricate
background execution authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import psycopg2
import psycopg2.extras

PORTAL_SCHEMA = "JAYTEC_PROTOCOL_PORTAL_V1"
MAX_JSON_BYTES = 96_000
MAX_TEXT = 12_000
MAX_LIST = 128
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
WORKER_KINDS = frozenset({"CHATGPT_UI", "JAYTEC_CALLABLE"})
REGISTER_STATUSES = frozenset({"RUNNING", "PAUSED", "BLOCKED"})
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED_SAFE", "CANCELED"})


class PortalError(RuntimeError):
    pass


def _bounded_text(value: Any, name: str, *, required: bool = True, maximum: int = MAX_TEXT) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PortalError(name + "_REQUIRED")
    if len(text) > maximum:
        raise PortalError(name + "_TOO_LONG")
    return text


def _json_object(raw: str | Mapping[str, Any], *, name: str) -> dict[str, Any]:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
            raise PortalError(name + "_TOO_LARGE")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PortalError(name + "_INVALID_JSON") from exc
    else:
        value = dict(raw)
    if not isinstance(value, Mapping):
        raise PortalError(name + "_NOT_OBJECT")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise PortalError(name + "_TOO_LARGE")
    return dict(value)


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise PortalError(name + "_INVALID")
    out: list[str] = []
    for item in value:
        text = _bounded_text(item, name, maximum=500)
        out.append(text)
    return tuple(out)


def portal_job_id(task_id: str) -> str:
    normalized = _bounded_text(task_id, "TASK_ID", maximum=200)
    if not TASK_ID_RE.fullmatch(normalized):
        raise PortalError("TASK_ID_INVALID")
    return "watch-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class PortalTaskRequest:
    task_id: str
    project_id: str
    objective: str
    source_chat_ref: str
    stage: str
    status: str
    priority: int
    watch_enabled: bool
    worker_kind: str
    worker_route: str
    notification_issue: Optional[int]
    dependencies: tuple[str, ...]
    checkpoint: Mapping[str, Any]

    @classmethod
    def parse(cls, raw: str | Mapping[str, Any]) -> "PortalTaskRequest":
        value = _json_object(raw, name="REGISTER_REQUEST")
        required = {"task_id", "objective", "source_chat_ref", "current_task_authorized"}
        missing = sorted(required - set(value))
        if missing:
            raise PortalError("REGISTER_MISSING:" + ",".join(missing))
        if value.get("current_task_authorized") is not True:
            raise PortalError("CURRENT_TASK_AUTH_REQUIRED")

        task_id = _bounded_text(value.get("task_id"), "TASK_ID", maximum=200)
        if not TASK_ID_RE.fullmatch(task_id):
            raise PortalError("TASK_ID_INVALID")
        project_id = _bounded_text(value.get("project_id") or "JAYTEC-PROTOCOL-PORTAL", "PROJECT_ID", maximum=200)
        objective = _bounded_text(value.get("objective"), "OBJECTIVE")
        source_chat_ref = _bounded_text(value.get("source_chat_ref"), "SOURCE_CHAT_REF", maximum=500)
        stage = _bounded_text(value.get("stage") or "REGISTERED", "STAGE", maximum=200)
        status = _bounded_text(value.get("status") or "RUNNING", "STATUS", maximum=32).upper()
        if status not in REGISTER_STATUSES:
            raise PortalError("REGISTER_STATUS_INVALID")
        priority_raw = value.get("priority", 100)
        if isinstance(priority_raw, bool) or not isinstance(priority_raw, int) or not 1 <= priority_raw <= 1000:
            raise PortalError("PRIORITY_INVALID")
        watch_enabled = value.get("watch_enabled", True)
        if type(watch_enabled) is not bool:
            raise PortalError("WATCH_ENABLED_INVALID")
        worker_kind = _bounded_text(value.get("worker_kind") or "CHATGPT_UI", "WORKER_KIND", maximum=40).upper()
        if worker_kind not in WORKER_KINDS:
            raise PortalError("WORKER_KIND_INVALID")
        worker_route = _bounded_text(
            value.get("worker_route") or ("chatgpt_ui" if worker_kind == "CHATGPT_UI" else ""),
            "WORKER_ROUTE",
            required=worker_kind == "JAYTEC_CALLABLE",
            maximum=200,
        )
        notification_issue = value.get("notification_issue")
        if notification_issue is not None:
            if isinstance(notification_issue, bool) or not isinstance(notification_issue, int) or notification_issue < 1:
                raise PortalError("NOTIFICATION_ISSUE_INVALID")
        checkpoint = value.get("checkpoint") or {}
        if not isinstance(checkpoint, Mapping):
            raise PortalError("CHECKPOINT_INVALID")
        return cls(
            task_id=task_id,
            project_id=project_id,
            objective=objective,
            source_chat_ref=source_chat_ref,
            stage=stage,
            status=status,
            priority=priority_raw,
            watch_enabled=watch_enabled,
            worker_kind=worker_kind,
            worker_route=worker_route,
            notification_issue=notification_issue,
            dependencies=_string_list(value.get("dependencies"), "DEPENDENCIES"),
            checkpoint=dict(checkpoint),
        )


class PortalStore:
    def __init__(self, database_url: str):
        self.database_url = _bounded_text(database_url, "PROTOCOL_DATABASE_URL", maximum=10_000)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def probe(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT
                          to_regclass('public.jaytec_jobs')::text AS jobs,
                          to_regclass('public.jaytec_job_events')::text AS events
                        """
                    )
                    row = dict(cur.fetchone() or {})
                    ready = bool(row.get("jobs") and row.get("events"))
                    return {
                        "schema_version": PORTAL_SCHEMA,
                        "status": "PASS" if ready else "NOT_READY",
                        "jobs_table": row.get("jobs"),
                        "events_table": row.get("events"),
                    }
        except Exception as exc:
            return {
                "schema_version": PORTAL_SCHEMA,
                "status": "FAILED_CLOSED",
                "reason": "PORTAL_PROBE_ERROR:" + type(exc).__name__,
            }

    @staticmethod
    def _resource(req: PortalTaskRequest) -> dict[str, Any]:
        stop_reason = "PAUSED_BY_OWNER" if req.status == "PAUSED" else (
            "WAITING_FOR_DEPENDENCY" if req.status == "BLOCKED" else "RUNNING"
        )
        return {
            "protocol_version": PORTAL_SCHEMA,
            "watch_enabled": req.watch_enabled,
            "worker_kind": req.worker_kind,
            "worker_route": req.worker_route,
            "source_chat_ref": req.source_chat_ref,
            "notification_issue": req.notification_issue,
            "stop_reason": stop_reason,
            "recovery_attempts": 0,
            "ui_chat_autoresume_supported": False,
            "continuation_instruction": "Resume — do not recreate completed work",
        }

    def register(self, raw: str | Mapping[str, Any]) -> dict[str, Any]:
        req = PortalTaskRequest.parse(raw)
        job_id = portal_job_id(req.task_id)
        resource = self._resource(req)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("portal:" + req.task_id,))
                cur.execute("SELECT * FROM public.jaytec_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
                existing = cur.fetchone()
                if existing is not None:
                    if str(existing["task_id"]) != req.task_id or str(existing["project_id"]) != req.project_id:
                        raise PortalError("TASK_ID_COLLISION")
                    if str(existing["objective"]).strip() != req.objective:
                        raise PortalError("CONFLICTING_DUPLICATE_OBJECTIVE")
                    if str(existing["status"]) in TERMINAL_STATUSES:
                        raise PortalError("TERMINAL_TASK_REQUIRES_NEW_TASK_ID")
                    cur.execute(
                        """
                        UPDATE public.jaytec_jobs
                           SET status=%s, stage=%s, priority=%s, dependencies=%s::jsonb,
                               resource_scope=%s::jsonb, health='HEALTHY',
                               blockers='[]'::jsonb, updated_at=now(), version=version+1
                         WHERE job_id=%s
                        """,
                        (
                            req.status,
                            req.stage,
                            req.priority,
                            json.dumps(list(req.dependencies)),
                            json.dumps(resource),
                            job_id,
                        ),
                    )
                else:
                    cur.execute("SELECT COALESCE(MAX(source_shared_state_version),1) AS v FROM public.jaytec_jobs")
                    source_version = int((cur.fetchone() or {}).get("v") or 1)
                    cur.execute(
                        """
                        INSERT INTO public.jaytec_jobs (
                          job_id, project_id, task_id, subtask_id, assignment_type,
                          objective, status, stage, priority, source_shared_state_version,
                          ownership_epoch, fence_token, execution_room_id, concurrency_class,
                          mutation_scope, read_scope, dependencies, resource_scope,
                          checkpoint_ref, latest_verified_result, blockers, health,
                          lease_owner, lease_expires_at, version, created_at, updated_at,
                          next_attempt_at
                        )
                        VALUES (
                          %s,%s,%s,NULL,'OWNER_CHAT_WATCH',%s,%s,%s,%s,%s,
                          1,1,NULL,NULL,'[]'::jsonb,'[]'::jsonb,%s::jsonb,%s::jsonb,
                          NULL,NULL,'[]'::jsonb,'HEALTHY',NULL,NULL,0,now(),now(),NULL
                        )
                        """,
                        (
                            job_id,
                            req.project_id,
                            req.task_id,
                            req.objective,
                            req.status,
                            req.stage,
                            req.priority,
                            source_version,
                            json.dumps(list(req.dependencies)),
                            json.dumps(resource),
                        ),
                    )

                event_payload = {
                    "schema_version": PORTAL_SCHEMA,
                    "task_id": req.task_id,
                    "instruction": "Resume — do not recreate completed work",
                    "source_chat_ref": req.source_chat_ref,
                    "checkpoint": dict(req.checkpoint),
                }
                cur.execute(
                    """
                    INSERT INTO public.jaytec_job_events(
                      job_id,event_type,source,source_version,payload
                    )
                    SELECT job_id,'JAYTEC_PROTOCOL_TASK_REGISTERED','JAYTEC_PROTOCOL_PORTAL',
                           source_shared_state_version,%s::jsonb
                      FROM public.jaytec_jobs WHERE job_id=%s
                    RETURNING event_id
                    """,
                    (json.dumps(event_payload), job_id),
                )
                event_id = int(cur.fetchone()["event_id"])
                cur.execute(
                    """
                    UPDATE public.jaytec_jobs
                       SET checkpoint_ref=%s, updated_at=now()
                     WHERE job_id=%s
                    RETURNING job_id,task_id,project_id,status,stage,priority,
                              fence_token,ownership_epoch,checkpoint_ref,resource_scope,
                              health,version,updated_at
                    """,
                    ("event:" + str(event_id), job_id),
                )
                row = dict(cur.fetchone())
                row["registered_event_id"] = event_id
                return row

    def _row_for_update(self, cur, task_id: str):
        job_id = portal_job_id(task_id)
        cur.execute("SELECT * FROM public.jaytec_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
        row = cur.fetchone()
        if row is None:
            raise PortalError("TASK_NOT_FOUND")
        return job_id, row

    def checkpoint(self, task_id: str, checkpoint: str | Mapping[str, Any], *, expected_fence_token: int) -> dict[str, Any]:
        task = _bounded_text(task_id, "TASK_ID", maximum=200)
        payload = _json_object(checkpoint, name="CHECKPOINT")
        if payload.get("task_id") not in (None, "", task):
            raise PortalError("CHECKPOINT_TASK_ID_MISMATCH")
        if isinstance(expected_fence_token, bool) or not isinstance(expected_fence_token, int) or expected_fence_token < 1:
            raise PortalError("FENCE_TOKEN_INVALID")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("portal:" + task,))
                job_id, row = self._row_for_update(cur, task)
                if int(row["fence_token"]) != expected_fence_token:
                    raise PortalError("STALE_FENCE_TOKEN")
                event_payload = {
                    "schema_version": PORTAL_SCHEMA,
                    "task_id": task,
                    "instruction": "Resume — do not recreate completed work",
                    "checkpoint": payload,
                }
                cur.execute(
                    """
                    INSERT INTO public.jaytec_job_events(job_id,event_type,source,source_version,payload)
                    VALUES (%s,'JAYTEC_WATCH_CHECKPOINT_V1','JAYTEC_PROTOCOL_PORTAL',%s,%s::jsonb)
                    RETURNING event_id,created_at
                    """,
                    (job_id, int(row["source_shared_state_version"]), json.dumps(event_payload)),
                )
                event = dict(cur.fetchone())
                stage = str(payload.get("current_phase") or row["stage"] or "RUNNING")[:200]
                cur.execute(
                    """
                    UPDATE public.jaytec_jobs
                       SET checkpoint_ref=%s, stage=%s, updated_at=now(), version=version+1
                     WHERE job_id=%s
                    RETURNING status,stage,fence_token,version,updated_at
                    """,
                    ("event:" + str(event["event_id"]), stage, job_id),
                )
                updated = dict(cur.fetchone())
                return {
                    "schema_version": PORTAL_SCHEMA,
                    "task_id": task,
                    "checkpoint_ref": "event:" + str(event["event_id"]),
                    "checkpoint_created_at": event["created_at"].isoformat(),
                    **updated,
                }

    def status(self, task_id: str) -> dict[str, Any]:
        task = _bounded_text(task_id, "TASK_ID", maximum=200)
        job_id = portal_job_id(task)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id,project_id,task_id,objective,status,stage,priority,
                           source_shared_state_version,ownership_epoch,fence_token,
                           dependencies,resource_scope,checkpoint_ref,blockers,health,
                           lease_owner,lease_expires_at,version,updated_at,next_attempt_at
                      FROM public.jaytec_jobs WHERE job_id=%s
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return {"schema_version": PORTAL_SCHEMA, "status": "NOT_FOUND", "task_id": task}
                result = dict(row)
                result["schema_version"] = PORTAL_SCHEMA
                result["portal_status"] = "FOUND"
                result["updated_at"] = result["updated_at"].isoformat()
                if result.get("lease_expires_at"):
                    result["lease_expires_at"] = result["lease_expires_at"].isoformat()
                ref = str(result.get("checkpoint_ref") or "")
                if ref.startswith("event:"):
                    try:
                        event_id = int(ref.split(":", 1)[1])
                    except ValueError:
                        event_id = 0
                    if event_id:
                        cur.execute(
                            "SELECT event_id,event_type,source,payload,created_at FROM public.jaytec_job_events WHERE event_id=%s AND job_id=%s",
                            (event_id, job_id),
                        )
                        event = cur.fetchone()
                        if event is not None:
                            event_dict = dict(event)
                            event_dict["created_at"] = event_dict["created_at"].isoformat()
                            result["checkpoint_event"] = event_dict
                return result

    def list_watch_tasks(self, *, include_terminal: bool = False, limit: int = 128) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
            raise PortalError("LIMIT_INVALID")
        statuses = ("QUEUED","RUNNING","PAUSED","BLOCKED") if not include_terminal else (
            "QUEUED","RUNNING","PAUSED","BLOCKED","SUCCEEDED","FAILED_SAFE","CANCELED"
        )
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id,project_id,task_id,objective,status,stage,priority,
                           fence_token,checkpoint_ref,resource_scope,health,updated_at
                      FROM public.jaytec_jobs
                     WHERE COALESCE(resource_scope->>'protocol_version','')=%s
                       AND COALESCE((resource_scope->>'watch_enabled')::boolean,false)=true
                       AND status = ANY(%s)
                     ORDER BY priority ASC, updated_at ASC
                     LIMIT %s
                    """,
                    (PORTAL_SCHEMA, list(statuses), limit),
                )
                out = []
                for row in cur.fetchall():
                    item = dict(row)
                    item["updated_at"] = item["updated_at"].isoformat()
                    out.append(item)
                return out

    def transition(self, task_id: str, *, action: str, expected_fence_token: int, reason: str = "") -> dict[str, Any]:
        task = _bounded_text(task_id, "TASK_ID", maximum=200)
        act = _bounded_text(action, "ACTION", maximum=32).upper()
        reason_text = _bounded_text(reason, "REASON", required=False, maximum=1000)
        mapping = {
            "PAUSE": ("PAUSED", "PAUSED_BY_OWNER", "JAYTEC_WATCH_PAUSED"),
            "RESUME": ("RUNNING", "RUNNING", "JAYTEC_WATCH_RESUMED"),
            "COMPLETE": ("SUCCEEDED", "COMPLETED", "JAYTEC_WATCH_COMPLETED"),
        }
        if act not in mapping:
            raise PortalError("TRANSITION_INVALID")
        if isinstance(expected_fence_token, bool) or not isinstance(expected_fence_token, int) or expected_fence_token < 1:
            raise PortalError("FENCE_TOKEN_INVALID")
        new_status, stop_reason, event_type = mapping[act]
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("portal:" + task,))
                job_id, row = self._row_for_update(cur, task)
                if int(row["fence_token"]) != expected_fence_token:
                    raise PortalError("STALE_FENCE_TOKEN")
                resource = dict(row["resource_scope"] or {})
                resource["stop_reason"] = stop_reason
                resource["recovery_attempts"] = 0
                new_fence = int(row["fence_token"]) + 1
                new_epoch = int(row["ownership_epoch"]) + 1
                cur.execute(
                    """
                    UPDATE public.jaytec_jobs
                       SET status=%s, resource_scope=%s::jsonb,
                           fence_token=%s, ownership_epoch=%s,
                           lease_owner=NULL, lease_expires_at=NULL,
                           health='HEALTHY', updated_at=now(), version=version+1
                     WHERE job_id=%s
                    RETURNING status,fence_token,ownership_epoch,version,updated_at
                    """,
                    (new_status, json.dumps(resource), new_fence, new_epoch, job_id),
                )
                updated = dict(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO public.jaytec_job_events(job_id,event_type,source,source_version,payload)
                    VALUES (%s,%s,'JAYTEC_PROTOCOL_PORTAL',%s,%s::jsonb)
                    RETURNING event_id
                    """,
                    (
                        job_id,
                        event_type,
                        int(row["source_shared_state_version"]),
                        json.dumps({
                            "schema_version": PORTAL_SCHEMA,
                            "task_id": task,
                            "action": act,
                            "reason": reason_text,
                            "new_fence_token": new_fence,
                            "instruction": "Resume — do not recreate completed work",
                        }),
                    ),
                )
                event_id = int(cur.fetchone()["event_id"])
                updated["event_id"] = event_id
                updated["task_id"] = task
                updated["schema_version"] = PORTAL_SCHEMA
                updated["updated_at"] = updated["updated_at"].isoformat()
                return updated


def safe_error(exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": PORTAL_SCHEMA,
        "status": "FAILED_CLOSED",
        "error": str(exc) if isinstance(exc, PortalError) else type(exc).__name__,
    }


__all__ = [
    "PORTAL_SCHEMA",
    "PortalError",
    "PortalStore",
    "PortalTaskRequest",
    "portal_job_id",
    "safe_error",
]
