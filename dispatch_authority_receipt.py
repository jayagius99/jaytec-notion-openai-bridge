"""Strict packet-bound JAYTEC dispatch authority receipt verifier.

This module is intentionally transport-agnostic. It does not grant production
dispatch by itself. A caller must supply a trusted, atomic claim callback owned
by the JAYTEC control plane. That callback is the authenticity/replay boundary;
receipt_sha256 alone is only an integrity check.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from orchestration import packet_hash

SCHEMA_VERSION = "JAYTEC_DISPATCH_AUTHORITY_RECEIPT_V1"
AUTHORITY_SOURCE = "JAYTEC_CONTROL_PLANE"
APPROVAL_STATE = "APPROVED"
JAY_APPROVER = "JAY"
MAX_LIST_ITEMS = 32
MAX_ID_LEN = 200
MAX_CLOCK_SKEW_SECONDS = 60

REQUIRED_FIELDS = {
    "schema_version",
    "authority_id",
    "authority_source",
    "task_id",
    "subtask_id",
    "workflow_id",
    "idempotency_key",
    "packet_subject_sha256",
    "protected_action",
    "approval_state",
    "approval_actor",
    "estimated_cost_units",
    "budget_limit_units",
    "allowed_operations",
    "allowed_specialists",
    "issued_at",
    "expires_at",
    "single_use",
    "receipt_sha256",
}
OPTIONAL_FIELDS = {
    "provider_constraints",
    "model_constraints",
}
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS


class DispatchAuthorityReceiptError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DispatchAuthorityReceiptError("INVALID_" + field.upper())
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DispatchAuthorityReceiptError("INVALID_" + field.upper()) from exc
    if parsed.tzinfo is None:
        raise DispatchAuthorityReceiptError("INVALID_" + field.upper())
    return parsed.astimezone(timezone.utc)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_ID_LEN


def _valid_str_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_LIST_ITEMS
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(set(value)) == len(value)
    )


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _sha(unsigned)


def build_receipt(
    *,
    authority_id: str,
    packet: Mapping[str, Any],
    protected_action: bool,
    approval_actor: str,
    estimated_cost_units: int,
    budget_limit_units: int,
    allowed_operations: list[str],
    allowed_specialists: list[str],
    issued_at: str,
    expires_at: str,
    provider_constraints: Mapping[str, Any] | None = None,
    model_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "authority_id": authority_id,
        "authority_source": AUTHORITY_SOURCE,
        "task_id": packet.get("task_id"),
        "subtask_id": packet.get("subtask_id"),
        "workflow_id": packet.get("workflow_id"),
        "idempotency_key": packet.get("idempotency_key"),
        "packet_subject_sha256": packet_hash(packet),
        "protected_action": protected_action,
        "approval_state": APPROVAL_STATE,
        "approval_actor": approval_actor,
        "estimated_cost_units": estimated_cost_units,
        "budget_limit_units": budget_limit_units,
        "allowed_operations": list(allowed_operations),
        "allowed_specialists": list(allowed_specialists),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "single_use": True,
    }
    if provider_constraints is not None:
        receipt["provider_constraints"] = dict(provider_constraints)
    if model_constraints is not None:
        receipt["model_constraints"] = dict(model_constraints)
    receipt["receipt_sha256"] = receipt_digest(receipt)
    return receipt


TrustedClaim = Callable[[str, str, str], bool]


def verify_and_claim_dispatch_authority_receipt(
    *,
    packet: Mapping[str, Any],
    receipt: Mapping[str, Any],
    trusted_claim: TrustedClaim,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify one exact receipt and atomically claim it from trusted JAYTEC state.

    trusted_claim(authority_id, receipt_sha256, packet_subject_sha256) MUST be
    atomic and MUST return True only for an unconsumed receipt previously issued
    by the trusted JAYTEC control plane. False means unknown, forged, stale, or
    replayed authority and therefore fails closed.
    """
    if not isinstance(packet, Mapping) or not isinstance(receipt, Mapping):
        raise DispatchAuthorityReceiptError("INVALID_ROOT")
    if not callable(trusted_claim):
        raise DispatchAuthorityReceiptError("TRUSTED_CLAIM_REQUIRED")

    keys = set(receipt.keys())
    missing = sorted(REQUIRED_FIELDS - keys)
    if missing:
        raise DispatchAuthorityReceiptError("MISSING_FIELDS:" + ",".join(missing))
    unknown = sorted(keys - ALLOWED_FIELDS)
    if unknown:
        raise DispatchAuthorityReceiptError("UNKNOWN_FIELDS:" + ",".join(unknown))

    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise DispatchAuthorityReceiptError("SCHEMA_MISMATCH")
    if receipt.get("authority_source") != AUTHORITY_SOURCE:
        raise DispatchAuthorityReceiptError("AUTHORITY_SOURCE_MISMATCH")

    for field in ("authority_id", "task_id", "subtask_id", "workflow_id", "idempotency_key"):
        if not _valid_id(receipt.get(field)):
            raise DispatchAuthorityReceiptError("INVALID_" + field.upper())

    for field in ("task_id", "subtask_id", "workflow_id", "idempotency_key"):
        if receipt.get(field) != packet.get(field):
            raise DispatchAuthorityReceiptError("PACKET_BINDING_MISMATCH:" + field)

    expected_packet_hash = packet_hash(packet)
    observed_packet_hash = str(receipt.get("packet_subject_sha256") or "").lower()
    if observed_packet_hash != expected_packet_hash:
        raise DispatchAuthorityReceiptError("PACKET_DIGEST_MISMATCH")

    observed_receipt_hash = str(receipt.get("receipt_sha256") or "").lower()
    if len(observed_receipt_hash) != 64 or any(c not in "0123456789abcdef" for c in observed_receipt_hash):
        raise DispatchAuthorityReceiptError("RECEIPT_DIGEST_INVALID")
    if observed_receipt_hash != receipt_digest(receipt):
        raise DispatchAuthorityReceiptError("RECEIPT_DIGEST_MISMATCH")

    if receipt.get("approval_state") != APPROVAL_STATE:
        raise DispatchAuthorityReceiptError("APPROVAL_STATE_INVALID")
    protected = receipt.get("protected_action")
    if type(protected) is not bool:
        raise DispatchAuthorityReceiptError("PROTECTED_ACTION_INVALID")
    actor = receipt.get("approval_actor")
    if not _valid_id(actor):
        raise DispatchAuthorityReceiptError("APPROVAL_ACTOR_INVALID")
    if protected and actor != JAY_APPROVER:
        raise DispatchAuthorityReceiptError("PROTECTED_ACTION_REQUIRES_JAY")

    estimated = receipt.get("estimated_cost_units")
    budget = receipt.get("budget_limit_units")
    if type(estimated) is not int or estimated < 0:
        raise DispatchAuthorityReceiptError("ESTIMATED_COST_INVALID")
    if type(budget) is not int or budget < 0:
        raise DispatchAuthorityReceiptError("BUDGET_LIMIT_INVALID")
    if estimated > budget:
        raise DispatchAuthorityReceiptError("COST_EXCEEDS_BUDGET")

    allowed_operations = receipt.get("allowed_operations")
    allowed_specialists = receipt.get("allowed_specialists")
    if not _valid_str_list(allowed_operations):
        raise DispatchAuthorityReceiptError("ALLOWED_OPERATIONS_INVALID")
    if not _valid_str_list(allowed_specialists):
        raise DispatchAuthorityReceiptError("ALLOWED_SPECIALISTS_INVALID")

    packet_operations = packet.get("allowed_operations")
    packet_specialists = packet.get("specialist_plan")
    if not isinstance(packet_operations, list):
        raise DispatchAuthorityReceiptError("PACKET_OPERATIONS_INVALID")
    if not isinstance(packet_specialists, list):
        raise DispatchAuthorityReceiptError("PACKET_SPECIALISTS_INVALID")
    if not set(packet_operations).issubset(set(allowed_operations)):
        raise DispatchAuthorityReceiptError("OPERATION_SCOPE_EXCEEDED")
    if not set(packet_specialists).issubset(set(allowed_specialists)):
        raise DispatchAuthorityReceiptError("SPECIALIST_SCOPE_EXCEEDED")

    if receipt.get("single_use") is not True:
        raise DispatchAuthorityReceiptError("SINGLE_USE_REQUIRED")

    issued = _parse_time(receipt.get("issued_at"), "issued_at")
    expires = _parse_time(receipt.get("expires_at"), "expires_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires <= issued:
        raise DispatchAuthorityReceiptError("INVALID_AUTHORITY_WINDOW")
    if issued.timestamp() - current.timestamp() > MAX_CLOCK_SKEW_SECONDS:
        raise DispatchAuthorityReceiptError("AUTHORITY_NOT_YET_VALID")
    if current >= expires:
        raise DispatchAuthorityReceiptError("AUTHORITY_EXPIRED")

    for optional_field in ("provider_constraints", "model_constraints"):
        value = receipt.get(optional_field)
        if value is not None and not isinstance(value, Mapping):
            raise DispatchAuthorityReceiptError(optional_field.upper() + "_INVALID")

    authority_id = str(receipt["authority_id"])
    claimed = trusted_claim(authority_id, observed_receipt_hash, expected_packet_hash)
    if claimed is not True:
        raise DispatchAuthorityReceiptError("TRUSTED_AUTHORITY_CLAIM_REJECTED")

    return dict(receipt)
