"""Bounded Manus -> JAYTEC model-specialist request broker.

WATCH keeps one canonical Manus worker/fence. Manus may request help, but it
never receives provider credentials and never dispatches models directly.
JAYTEC validates the request, invokes an allow-listed subordinate specialist,
then returns correlated evidence to the same Manus task.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Mapping, Sequence

from manus_governance import validate_specialist_request

SCHEMA_VERSION = "JAYTEC_MANUS_SPECIALIST_BROKER_V1"
RESULT_SCHEMA_VERSION = "JAYTEC_MANUS_SPECIALIST_RESULTS_V1"
ALLOWED_MANUS_MODEL_SPECIALISTS = frozenset({"sol", "deepseek", "nemo"})
MAX_MODEL_REQUESTS = 3
MAX_RESULT_BYTES = 64_000

_SECRET_KEY = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|credential|private[_-]?key|access[_-]?token)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+)"
)

# Sealed identity/origin provenance is not specialist context. These are
# detection markers only; they contain no sealed provenance themselves.
_SEALED_MARKERS = (
    "uren origin",
    "uren birth",
    "how uren was born",
    "how uren will be born",
    "uren construction",
    "uren activation sequence",
    "genesis_event_0001",
    "/jaytec/uren/pre-genesis",
    "uren_identity_genesis",
)
_SEALED_COMPACT_MARKERS = tuple(
    "".join(ch for ch in marker if ch.isalnum()) for marker in _SEALED_MARKERS
)


class SpecialistBrokerError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                return True
            if _contains_sensitive(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive(item) for item in value)
    return isinstance(value, str) and bool(_SECRET_VALUE.search(value))


def _contains_sealed_provenance(value: Any) -> bool:
    text = unicodedata.normalize(
        "NFKC",
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str),
    ).lower()
    compact = "".join(ch for ch in text if ch.isalnum())
    if any(marker in text for marker in _SEALED_MARKERS):
        return True
    return any(marker in compact for marker in _SEALED_COMPACT_MARKERS)


def normalize_manus_model_request(
    value: Mapping[str, Any],
    *,
    parent_task_prefix: str,
) -> dict[str, Any]:
    """Validate one Manus SPECIALIST_REQUEST without granting dispatch authority."""
    validate_specialist_request(value)
    specialist = str(value.get("specialist") or "").strip().casefold()
    if specialist not in ALLOWED_MANUS_MODEL_SPECIALISTS:
        raise SpecialistBrokerError("SPECIALIST_NOT_ALLOWLISTED:" + specialist)

    parent_task_id = str(value.get("parent_task_id") or "").strip()
    if not parent_task_id.startswith(parent_task_prefix):
        raise SpecialistBrokerError("SPECIALIST_PARENT_TASK_MISMATCH")
    if value.get("authority") != "REQUEST_ONLY_NO_SELF_DISPATCH":
        raise SpecialistBrokerError("SPECIALIST_AUTHORITY_INVALID")
    if _contains_sensitive(value):
        raise SpecialistBrokerError("SPECIALIST_REQUEST_SECRET_MATERIAL_FORBIDDEN")
    if _contains_sealed_provenance(value):
        raise SpecialistBrokerError("SPECIALIST_REQUEST_SEALED_PROVENANCE_BLOCKED")

    return {
        "request_id": str(value["request_id"]),
        "parent_task_id": parent_task_id,
        "specialist": specialist,
        "objective": str(value["objective"]).strip(),
        "reason": str(value["reason"]).strip(),
        "required_context": dict(value.get("required_context") or {}),
        "packet_sha256": str(value["packet_sha256"]).lower(),
    }


def build_dispatch_packet(request: Mapping[str, Any]) -> dict[str, Any]:
    specialist = str(request["specialist"])
    request_id = str(request["request_id"])
    return {
        "task_id": str(request["parent_task_id"]),
        "subtask_id": request_id,
        "workflow_id": "JAYTEC_WATCH_SPECIALIST_" + specialist.upper() + "_V1",
        "request": str(request["objective"]),
        "objective": str(request["objective"]),
        "reason": str(request["reason"]),
        "specialist_plan": [specialist],
        "allowed_operations": [],
        "max_retries": 0,
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE",
            "source": "JAYTEC_WATCH_SPECIALIST_BROKER",
            "return_to": "SAME_MANUS_WORKER",
            "manus_request_context": dict(request.get("required_context") or {}),
        },
    }


def dispatch_manus_model_requests(
    requests: Sequence[Mapping[str, Any]],
    *,
    dispatchers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]],
) -> dict[str, Any]:
    """Invoke bounded model specialists and return one correlated result package."""
    if len(requests) > MAX_MODEL_REQUESTS:
        raise SpecialistBrokerError("SPECIALIST_REQUEST_COUNT_EXCEEDED")

    results: list[dict[str, Any]] = []
    for request in requests:
        specialist = str(request.get("specialist") or "").casefold()
        if specialist not in ALLOWED_MANUS_MODEL_SPECIALISTS:
            raise SpecialistBrokerError("SPECIALIST_NOT_ALLOWLISTED:" + specialist)
        dispatcher = dispatchers.get(specialist)
        if dispatcher is None:
            raise SpecialistBrokerError("SPECIALIST_DISPATCHER_UNAVAILABLE:" + specialist)

        packet = build_dispatch_packet(request)
        if _contains_sealed_provenance(packet):
            raise SpecialistBrokerError("SPECIALIST_PACKET_SEALED_PROVENANCE_BLOCKED")

        try:
            raw = dispatcher(packet)
        except Exception as exc:
            # Error class only; provider text can contain account or transport data.
            result = {
                "status": "FAILED_CLOSED",
                "model": None,
                "findings": [],
                "evidence": [],
                "unresolved_items": ["specialist_error:" + type(exc).__name__],
                "side_effects_attempted": [],
                "requested_operations": [],
            }
        else:
            if not isinstance(raw, Mapping):
                raise SpecialistBrokerError("SPECIALIST_RESULT_NOT_MAPPING:" + specialist)
            result = dict(raw)

        if result.get("side_effects_attempted") not in (None, []):
            raise SpecialistBrokerError("SPECIALIST_SIDE_EFFECT_ATTEMPT:" + specialist)
        if result.get("requested_operations") not in (None, []):
            raise SpecialistBrokerError("SPECIALIST_OPERATION_REQUEST_FORBIDDEN:" + specialist)
        if _contains_sensitive(result):
            raise SpecialistBrokerError("SPECIALIST_RESULT_SECRET_MATERIAL_FORBIDDEN")
        if _contains_sealed_provenance(result):
            raise SpecialistBrokerError("SPECIALIST_RESULT_SEALED_PROVENANCE_BLOCKED")

        encoded = _canonical(result)
        if len(encoded) > MAX_RESULT_BYTES:
            raise SpecialistBrokerError("SPECIALIST_RESULT_TOO_LARGE:" + specialist)
        results.append(
            {
                "request_id": str(request["request_id"]),
                "specialist": specialist,
                "status": str(result.get("status") or "FAILED_CLOSED"),
                "model": result.get("model"),
                "result": result,
                "result_sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )

    unsigned = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "authority": "RESULTS_ONLY_NO_DISPATCH_AUTHORITY",
        "request_results": results,
    }
    digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    return {**unsigned, "sha256": digest}


def safe_result_summary(package: Mapping[str, Any]) -> dict[str, Any]:
    rows = package.get("request_results")
    if not isinstance(rows, list):
        return {"status": "INVALID"}
    return {
        "schema_version": str(package.get("schema_version") or ""),
        "sha256": str(package.get("sha256") or ""),
        "results": [
            {
                "request_id": str(row.get("request_id") or ""),
                "specialist": str(row.get("specialist") or ""),
                "status": str(row.get("status") or ""),
                "model": row.get("model"),
                "result_sha256": str(row.get("result_sha256") or ""),
            }
            for row in rows
            if isinstance(row, Mapping)
        ],
    }
