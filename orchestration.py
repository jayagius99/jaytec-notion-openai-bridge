from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, MutableMapping, Optional

from jaytec_read import (
    JAYTEC_READ_ALLOWED_OPERATIONS,
    JAYTEC_READ_REQUIRED_OPERATIONS,
    JAYTEC_READ_WORKFLOW_ID,
)

PACKET_VERSION = "1.0"
RETURN_SCHEMA_VERSION = "1.0"
ALLOWED_SPECIALISTS = ("codex", "gemini")
EXPECTED_MODELS = {
    "codex": "gpt-5.6-sol",
    "gemini": "google/gemini-3.1-pro-preview",
}
ALLOWED_STATUSES = {
    "SUCCESS",
    "PARTIAL_SUCCESS",
    "NEEDS_VALIDATION",
    "POLICY_BLOCKED",
    "FAILED_CLOSED",
    "INVALID_PACKET",
    "TIMEOUT",
    "RATE_LIMITED",
}
SAFE_OPERATIONS = {"read", "research", "analyze", "validate", "test", "draft", "code_staging", "web_fetch"}
REQUIRED_PACKET_FIELDS = {
    "packet_version",
    "task_id",
    "subtask_id",
    "request",
    "intent",
    "workflow_id",
    "risk_level",
    "specialist_plan",
    "allowed_operations",
    "expected_output",
    "validation_requirements",
    "side_effect_policy",
    "idempotency_key",
    "deadline",
    "max_fanout",
    "max_retries",
    "return_schema_version",
}
OPTIONAL_PACKET_FIELDS = {
    "parent_task_id",
    "required_context",
    "context_digests",
    "known_facts",
    "constraints",
}
ALLOWED_PACKET_FIELDS = REQUIRED_PACKET_FIELDS | OPTIONAL_PACKET_FIELDS
SECRET_KEY_PATTERN = re.compile(r"(api[_-]?key|authorization|bearer|token|password|secret|credential)", re.I)
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)"
)
OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_CONTEXT_BYTES = 256_000
MAX_WORKER_OUTPUT_BYTES = 256_000
MAX_TEXT_FIELD = 100_000
MAX_OPERATIONS = 32
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 86_400


class PacketValidationError(ValueError):
    pass


class RateLimitError(RuntimeError):
    def __init__(self, message: str = "rate limited", *, retry_after: Any = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailableError(RuntimeError):
    def __init__(self, message: str = "provider unavailable", *, retry_after: Any = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass
class IdempotencyRecord:
    packet_hash: str
    result: Dict[str, Any]
    created_at: datetime


@dataclass
class ExecutionRegistry:
    ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS
    _records: MutableMapping[str, IdempotencyRecord] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def lookup(
        self, key: str, packet_hash: str, *, now: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            existing = self._records.get(key)
            if not existing:
                return None
            if (current - existing.created_at).total_seconds() > self.ttl_seconds:
                del self._records[key]
                return None
            if existing.packet_hash != packet_hash:
                raise PacketValidationError("CONFLICTING_DUPLICATE")
            return copy.deepcopy(existing.result)

    def store(
        self,
        key: str,
        packet_hash: str,
        result: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            existing = self._records.get(key)
            if existing and (current - existing.created_at).total_seconds() <= self.ttl_seconds:
                if existing.packet_hash != packet_hash:
                    raise PacketValidationError("CONFLICTING_DUPLICATE")
            self._records[key] = IdempotencyRecord(packet_hash, copy.deepcopy(dict(result)), current)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def packet_hash(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(packet).encode("utf-8")).hexdigest()


def _parse_deadline(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PacketValidationError("deadline must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PacketValidationError("deadline must be valid ISO-8601") from exc
    if dt.tzinfo is None:
        raise PacketValidationError("deadline must include timezone")
    return dt.astimezone(timezone.utc)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = "[REDACTED]" if SECRET_KEY_PATTERN.search(str(k)) else redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return SECRET_VALUE_PATTERN.sub("[REDACTED]", value[:MAX_TEXT_FIELD])
    return value


def parse_packet_json(packet_json: str) -> tuple[Optional[Dict[str, Any]], tuple[str, ...]]:
    if not isinstance(packet_json, str) or not packet_json.strip():
        return None, ("malformed_json:empty",)
    try:
        parsed = json.loads(packet_json)
    except json.JSONDecodeError as exc:
        return None, (f"malformed_json:{exc.msg}",)
    if not isinstance(parsed, dict):
        return None, ("malformed_json:root_must_be_object",)
    return parsed, ()


def _valid_operation_list(value: Any, *, require_nonempty: bool) -> bool:
    if not isinstance(value, list):
        return False
    if require_nonempty and not value:
        return False
    if len(value) > MAX_OPERATIONS:
        return False
    return all(isinstance(op, str) and OPERATION_PATTERN.fullmatch(op) for op in value)


def validate_packet(packet: Mapping[str, Any], *, now: Optional[datetime] = None) -> ValidationResult:
    errors: list[str] = []
    for key in sorted(REQUIRED_PACKET_FIELDS):
        if key not in packet:
            errors.append(f"missing:{key}")
    unknown_fields = sorted(set(packet.keys()) - ALLOWED_PACKET_FIELDS)
    if unknown_fields:
        errors.append("unknown_fields:" + ",".join(map(str, unknown_fields)))
    if errors and any(e.startswith("missing:") for e in errors):
        return ValidationResult(False, tuple(errors))

    if packet.get("packet_version") != PACKET_VERSION:
        errors.append("unsupported:packet_version")
    if packet.get("return_schema_version") != RETURN_SCHEMA_VERSION:
        errors.append("unsupported:return_schema_version")

    for ident in ("task_id", "subtask_id", "workflow_id", "idempotency_key"):
        value = packet.get(ident)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            errors.append(f"invalid:{ident}")
    for text_field in ("request", "intent", "risk_level", "expected_output"):
        value = packet.get(text_field)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_FIELD:
            errors.append(f"invalid:{text_field}")

    plan = packet.get("specialist_plan")
    if not isinstance(plan, list) or not plan:
        errors.append("invalid:specialist_plan")
        plan = []
    elif not all(isinstance(s, str) for s in plan):
        errors.append("invalid:specialist_plan_items")
        plan = []
    else:
        unknown = [s for s in plan if s not in ALLOWED_SPECIALISTS]
        if unknown:
            errors.append("unknown_specialist:" + ",".join(map(str, unknown)))
        if len(set(plan)) != len(plan):
            errors.append("duplicate_specialist")

    max_fanout = packet.get("max_fanout")
    if (
        not isinstance(max_fanout, int)
        or isinstance(max_fanout, bool)
        or max_fanout < 1
        or max_fanout > len(ALLOWED_SPECIALISTS)
    ):
        errors.append("invalid:max_fanout")
    elif len(plan) > max_fanout:
        errors.append("fanout_exceeds_limit")

    max_retries = packet.get("max_retries")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not 0 <= max_retries <= 3:
        errors.append("invalid:max_retries")

    ops = packet.get("allowed_operations")
    if not _valid_operation_list(ops, require_nonempty=True):
        errors.append("invalid:allowed_operations")
    else:
        forbidden = [op for op in ops if op not in SAFE_OPERATIONS]
        if forbidden:
            errors.append("unauthorized_operation:" + ",".join(forbidden))
        if len(set(ops)) != len(ops):
            errors.append("duplicate_allowed_operation")

    if packet.get("workflow_id") == JAYTEC_READ_WORKFLOW_ID:
        if plan != ["gemini"]:
            errors.append("jaytec_read_requires_gemini_only")
        allowed_ops = set(ops) if isinstance(ops, list) else set()
        if not JAYTEC_READ_REQUIRED_OPERATIONS.issubset(allowed_ops):
            errors.append("jaytec_read_missing_required_operations")
        disallowed_read_ops = sorted(allowed_ops - JAYTEC_READ_ALLOWED_OPERATIONS)
        if disallowed_read_ops:
            errors.append("jaytec_read_disallowed_operations:" + ",".join(disallowed_read_ops))
        if packet.get("side_effect_policy") != "none":
            errors.append("jaytec_read_side_effects_forbidden")
        required_context = packet.get("required_context")
        if not isinstance(required_context, Mapping):
            errors.append("jaytec_read_required_context_missing")
        else:
            source_url = required_context.get("source_url")
            if not isinstance(source_url, str) or not source_url.strip():
                errors.append("jaytec_read_source_url_missing")

    if packet.get("side_effect_policy") not in ("none", "staging_only"):
        errors.append("invalid:side_effect_policy")
    if not isinstance(packet.get("validation_requirements"), list) or not packet.get(
        "validation_requirements"
    ):
        errors.append("invalid:validation_requirements")

    try:
        deadline = _parse_deadline(packet.get("deadline"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if deadline <= current:
            errors.append("deadline_expired")
    except PacketValidationError as exc:
        errors.append(f"invalid:deadline:{exc}")

    try:
        if len(_canonical_json(packet.get("required_context", {})).encode("utf-8")) > MAX_CONTEXT_BYTES:
            errors.append("oversized_context")
    except (TypeError, ValueError):
        errors.append("invalid:required_context")

    return ValidationResult(not errors, tuple(errors))


def _base_envelope(packet: Mapping[str, Any], *, status: str, execution_id: str) -> Dict[str, Any]:
    return {
        "execution_id": execution_id,
        "task_id": packet.get("task_id", ""),
        "subtask_id": packet.get("subtask_id", ""),
        "overall_status": status,
        "answer": "",
        "findings": [],
        "evidence": [],
        "confidence": None,
        "codex_result": None,
        "gemini_result": None,
        "conflicts": [],
        "unresolved_items": [],
        "files_or_artifacts": [],
        "architecture_changes_required": [],
        "knowledge_writeback_proposal": [],
        "side_effects_attempted": [],
        "approval_required": False,
        "retry_trace": [],
        "worker_trace": [],
        "timing": {},
        "usage_summary": {},
        "packet_hash": packet_hash(packet),
        "integrity": {"algorithm": "sha256", "signature": None},
        "return_schema_version": RETURN_SCHEMA_VERSION,
    }


def invalid_packet_envelope(packet: Mapping[str, Any], errors: Iterable[str]) -> Dict[str, Any]:
    envelope = _base_envelope(packet, status="INVALID_PACKET", execution_id="invalid")
    envelope["unresolved_items"] = list(errors)
    return redact(envelope)


def retry_after_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _fail_closed_worker(unresolved: Iterable[str]) -> Dict[str, Any]:
    return {
        "status": "FAILED_CLOSED",
        "findings": [],
        "evidence": [],
        "unresolved_items": list(unresolved),
    }


def _validate_worker_contract_fields(result: MutableMapping[str, Any]) -> list[str]:
    unresolved: list[str] = []

    if "status" not in result:
        unresolved.append("missing_required_field:status")
    if "findings" not in result:
        unresolved.append("missing_required_field:findings")
    if "evidence" not in result:
        unresolved.append("missing_required_field:evidence")

    status = result.get("status")
    if not isinstance(status, str):
        unresolved.append("invalid_type:status")
    elif status not in ALLOWED_STATUSES:
        unresolved.append("invalid_worker_status")

    findings = result.get("findings")
    if not isinstance(findings, list):
        unresolved.append("invalid_type:findings")
    elif not all(isinstance(x, str) for x in findings):
        unresolved.append("invalid_type:findings_items")

    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        unresolved.append("invalid_type:evidence")
    elif not all(isinstance(x, str) for x in evidence):
        unresolved.append("invalid_type:evidence_items")

    if "unresolved_items" in result:
        unresolved_items = result.get("unresolved_items")
        if not isinstance(unresolved_items, list) or not all(isinstance(x, str) for x in unresolved_items):
            unresolved.append("invalid_type:unresolved_items")

    if "requested_operations" in result:
        requested_ops = result.get("requested_operations")
        if not _valid_operation_list(requested_ops, require_nonempty=False):
            unresolved.append("invalid_requested_operations_type")

    if "side_effects_attempted" in result:
        side_effects = result.get("side_effects_attempted")
        if not _valid_operation_list(side_effects, require_nonempty=False):
            unresolved.append("invalid_side_effects_attempted_type")

    if "model" in result and not (isinstance(result.get("model"), str) or result.get("model") is None):
        unresolved.append("invalid_type:model")

    return unresolved


def _normalize_worker_result(
    specialist: str,
    raw: Mapping[str, Any],
    allowed_operations: Iterable[str],
) -> Dict[str, Any]:
    try:
        encoded = _canonical_json(raw).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return _fail_closed_worker([f"invalid_worker_output:{type(exc).__name__}"])
    if len(encoded) > MAX_WORKER_OUTPUT_BYTES:
        return _fail_closed_worker(["oversized_worker_output"])

    result: Dict[str, Any] = dict(raw)

    unresolved = _validate_worker_contract_fields(result)
    if unresolved:
        return redact(_fail_closed_worker(unresolved))

    expected_model = EXPECTED_MODELS[specialist]
    returned_model = result.get("model")
    if returned_model != expected_model:
        return redact(_fail_closed_worker([f"model_mismatch:expected={expected_model}:returned={returned_model}"]))

    requested_ops = result.get("requested_operations", [])
    side_effects = result.get("side_effects_attempted", [])

    globally_unsafe = [op for op in requested_ops if op not in SAFE_OPERATIONS]
    packet_allowed = set(allowed_operations)
    task_unauthorized = [op for op in requested_ops if op not in packet_allowed]
    policy_violations: list[str] = []
    if globally_unsafe:
        policy_violations.append("malicious_or_unauthorized_worker_operation:" + ",".join(globally_unsafe))
    if task_unauthorized:
        policy_violations.append("worker_operation_not_allowed_by_packet:" + ",".join(task_unauthorized))
    if side_effects:
        policy_violations.append("worker_side_effect_attempted:" + ",".join(side_effects))

    if policy_violations:
        return redact(
            {
                **result,
                "status": "POLICY_BLOCKED" if result.get("status") != "FAILED_CLOSED" else "FAILED_CLOSED",
                "unresolved_items": list(result.get("unresolved_items", [])) + policy_violations,
            }
        )

    return redact(result)


def _invoke_with_retries(
    specialist: str,
    dispatcher: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    packet: Mapping[str, Any],
    *,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], None],
) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
    deadline = _parse_deadline(packet["deadline"])
    retry_trace: list[Dict[str, Any]] = []
    max_retries = int(packet["max_retries"])

    for attempt in range(max_retries + 1):
        current = now_fn().astimezone(timezone.utc)
        if current >= deadline:
            return (
                {
                    "status": "TIMEOUT",
                    "model": EXPECTED_MODELS[specialist],
                    "findings": [],
                    "evidence": [],
                    "unresolved_items": ["deadline_exceeded_before_dispatch"],
                },
                retry_trace,
            )
        try:
            raw = dispatcher(copy.deepcopy(packet))
            if not isinstance(raw, Mapping):
                raise TypeError("dispatcher result must be mapping")
            return _normalize_worker_result(specialist, raw, packet["allowed_operations"]), retry_trace
        except TimeoutError:
            return (
                {
                    "status": "TIMEOUT",
                    "model": EXPECTED_MODELS[specialist],
                    "findings": [],
                    "evidence": [],
                    "unresolved_items": ["worker_timeout"],
                },
                retry_trace,
            )
        except (RateLimitError, ProviderUnavailableError) as exc:
            status = "RATE_LIMITED" if isinstance(exc, RateLimitError) else "FAILED_CLOSED"
            retry_after = retry_after_seconds(getattr(exc, "retry_after", None))
            retry_trace.append(
                {
                    "specialist": specialist,
                    "attempt": attempt + 1,
                    "error": type(exc).__name__,
                    "retry_after_seconds": retry_after,
                }
            )
            if attempt >= max_retries:
                return (
                    {
                        "status": status,
                        "model": EXPECTED_MODELS[specialist],
                        "findings": [],
                        "evidence": [],
                        "unresolved_items": ["retry_budget_exhausted"],
                    },
                    retry_trace,
                )
            delay = float(retry_after if retry_after is not None else min(2**attempt, 8))
            if delay >= (deadline - current).total_seconds():
                return (
                    {
                        "status": "TIMEOUT",
                        "model": EXPECTED_MODELS[specialist],
                        "findings": [],
                        "evidence": [],
                        "unresolved_items": ["retry_after_exceeds_deadline"],
                    },
                    retry_trace,
                )
            sleep_fn(delay)
        except Exception as exc:
            return (
                {
                    "status": "FAILED_CLOSED",
                    "model": EXPECTED_MODELS[specialist],
                    "findings": [],
                    "evidence": [],
                    "unresolved_items": [f"worker_error:{type(exc).__name__}"],
                },
                retry_trace,
            )
    raise AssertionError("unreachable")


def deterministic_fan_in(
    packet: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]], *, execution_id: str
) -> Dict[str, Any]:
    ordered = [s for s in ALLOWED_SPECIALISTS if s in results]
    statuses = [str(results[s].get("status", "FAILED_CLOSED")) for s in ordered]
    success_count = sum(1 for s in statuses if s == "SUCCESS")
    if success_count == len(ordered) and ordered:
        overall = "SUCCESS"
    elif success_count > 0:
        overall = "PARTIAL_SUCCESS"
    elif any(s == "POLICY_BLOCKED" for s in statuses):
        overall = "POLICY_BLOCKED"
    elif any(s == "RATE_LIMITED" for s in statuses):
        overall = "RATE_LIMITED"
    elif any(s == "TIMEOUT" for s in statuses):
        overall = "TIMEOUT"
    else:
        overall = "FAILED_CLOSED"

    env = _base_envelope(packet, status=overall, execution_id=execution_id)
    for specialist in ordered:
        safe = redact(dict(results[specialist]))
        env[f"{specialist}_result"] = safe
        env["worker_trace"].append(
            {
                "specialist": specialist,
                "status": safe.get("status", "FAILED_CLOSED"),
                "model": safe.get("model"),
            }
        )
        env["findings"].extend(safe.get("findings", []) if isinstance(safe.get("findings"), list) else [])
        env["evidence"].extend(safe.get("evidence", []) if isinstance(safe.get("evidence"), list) else [])
        env["unresolved_items"].extend(
            safe.get("unresolved_items", []) if isinstance(safe.get("unresolved_items"), list) else []
        )
        env["side_effects_attempted"].extend(
            safe.get("side_effects_attempted", [])
            if isinstance(safe.get("side_effects_attempted"), list)
            else []
        )

    claims = {s: results[s].get("conclusion") for s in ordered if results[s].get("conclusion") is not None}
    if len(set(map(_canonical_json, claims.values()))) > 1:
        env["conflicts"].append({"type": "SPECIALIST_CONCLUSION_CONFLICT", "claims": redact(claims)})
        if env["overall_status"] == "SUCCESS":
            env["overall_status"] = "NEEDS_VALIDATION"
    return redact(env)


def execute_task_packet_core(
    packet: Mapping[str, Any],
    dispatchers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]],
    registry: ExecutionRegistry,
    *,
    now: Optional[datetime] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    fixed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    clock = now_fn or (lambda: fixed_now)
    validation = validate_packet(packet, now=fixed_now)
    if not validation.ok:
        return invalid_packet_envelope(packet, validation.errors)

    digest = packet_hash(packet)
    idem = str(packet["idempotency_key"])
    try:
        cached = registry.lookup(idem, digest, now=fixed_now)
    except PacketValidationError:
        env = invalid_packet_envelope(packet, ["CONFLICTING_DUPLICATE"])
        env["overall_status"] = "FAILED_CLOSED"
        return env
    if cached is not None:
        cached["usage_summary"] = {**cached.get("usage_summary", {}), "idempotent_replay": True}
        return cached

    execution_id = hashlib.sha256(f"{idem}:{digest}".encode()).hexdigest()[:24]
    results: Dict[str, Mapping[str, Any]] = {}
    retry_trace: list[Dict[str, Any]] = []
    for specialist in packet["specialist_plan"]:
        dispatcher = dispatchers.get(specialist)
        if dispatcher is None:
            results[specialist] = {
                "status": "FAILED_CLOSED",
                "model": EXPECTED_MODELS[specialist],
                "findings": [],
                "evidence": [],
                "unresolved_items": [f"dispatcher_unavailable:{specialist}"],
            }
            continue
        result, trace = _invoke_with_retries(specialist, dispatcher, packet, now_fn=clock, sleep_fn=sleep_fn)
        results[specialist] = result
        retry_trace.extend(trace)

    result = deterministic_fan_in(packet, results, execution_id=execution_id)
    result["retry_trace"] = redact(retry_trace)
    result["usage_summary"] = {
        **result.get("usage_summary", {}),
        "idempotent_replay": False,
        "specialist_count": len(packet["specialist_plan"]),
        "retry_count": len(retry_trace),
    }
    registry.store(idem, digest, result, now=fixed_now)
    return result
