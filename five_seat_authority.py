from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from five_seat_signals import PostgresFabricSignal, WORK_AVAILABLE_CHANNEL


HIGH_RISK_AUTHORITY_CLASSES = frozenset(
    {
        "CANONICAL_SHARED",
        "EXTERNAL_SIDE_EFFECT",
        "GLOBAL_EXCLUSIVE",
        "OWNER_GATED",
    }
)

COST_MODES = frozenset({"ZERO_SPEND", "OWNER_APPROVED"})


class FabricAuthorityError(RuntimeError):
    pass


class FabricAuthorityStale(FabricAuthorityError):
    pass


class FabricApprovalRequired(FabricAuthorityError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(dict(value)).encode("utf-8")).hexdigest()


def normalize_cost_policy(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    raw = dict(value or {})
    mode = str(raw.get("mode") or "ZERO_SPEND").strip().upper()
    if mode not in COST_MODES:
        raise ValueError("unsupported_cost_mode:" + mode)
    allow_paid = bool(raw.get("allow_paid", False))
    try:
        max_cost_usd = float(raw.get("max_cost_usd", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_max_cost_usd") from exc
    if max_cost_usd < 0:
        raise ValueError("max_cost_usd_must_be_nonnegative")
    provider_mode = str(raw.get("provider_mode") or "FREE_ONLY").strip().upper()
    if mode == "ZERO_SPEND":
        if allow_paid or max_cost_usd != 0:
            raise ValueError("zero_spend_policy_cannot_allow_paid_cost")
        provider_mode = "FREE_ONLY"
    elif mode == "OWNER_APPROVED":
        if not allow_paid and max_cost_usd > 0:
            raise ValueError("owner_approved_cost_requires_allow_paid")
    return {
        "mode": mode,
        "allow_paid": allow_paid,
        "max_cost_usd": round(max_cost_usd, 4),
        "provider_mode": provider_mode,
    }


def authority_requires_approval(
    authority_class: str,
    cost_policy: Optional[Mapping[str, Any]],
) -> bool:
    policy = normalize_cost_policy(cost_policy)
    klass = str(authority_class or "").strip().upper()
    return (
        klass in HIGH_RISK_AUTHORITY_CLASSES
        or policy["mode"] != "ZERO_SPEND"
        or policy["allow_paid"] is True
        or float(policy["max_cost_usd"]) > 0
    )


class PostgresFabricAuthority:
    """Durable owner/WATCH admission guard for the five-seat fabric.

    This is a projection/fence over the existing canonical shared-state version,
    not a replacement source of truth.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self.signal = PostgresFabricSignal(database_url)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def current_state(self) -> Dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_fabric_authority_state
                    WHERE authority_id='FABRIC'
                    """
                )
                row = cur.fetchone()
                if row is None:
                    raise FabricAuthorityError("fabric_authority_state_missing")
                return dict(row)

    def set_current_shared_state_version(
        self,
        version: int,
        *,
        updated_by: str,
        expected_authority_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        version = int(version)
        updated_by = str(updated_by or "").strip()
        if version <= 0:
            raise ValueError("version must be > 0")
        if not updated_by:
            raise ValueError("updated_by is required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_fabric_authority_state
                    WHERE authority_id='FABRIC'
                    FOR UPDATE
                    """
                )
                row = cur.fetchone()
                if row is None:
                    raise FabricAuthorityError("fabric_authority_state_missing")
                if (
                    expected_authority_epoch is not None
                    and int(row["authority_epoch"]) != int(expected_authority_epoch)
                ):
                    raise FabricAuthorityStale("authority_epoch_mismatch")
                if int(row["current_shared_state_version"]) > version:
                    raise FabricAuthorityStale("shared_state_version_regression")
                cur.execute(
                    """
                    UPDATE jaytec_fabric_authority_state
                    SET current_shared_state_version=%s,
                        authority_epoch=authority_epoch+1,
                        fence_token=fence_token+1,
                        updated_by=%s,
                        updated_at=now()
                    WHERE authority_id='FABRIC'
                    RETURNING *
                    """,
                    (version, updated_by),
                )
                updated = cur.fetchone()
                if updated is None:
                    raise FabricAuthorityError("authority_state_update_failed")
                return dict(updated)

    def approve_job(
        self,
        job_id: str,
        *,
        approved_by: str,
        approval_ref: str,
        max_cost_usd: float = 0.0,
        ttl_seconds: int = 3600,
    ) -> Dict[str, Any]:
        job_id = str(job_id or "").strip()
        approved_by = str(approved_by or "").strip()
        approval_ref = str(approval_ref or "").strip()
        if not job_id or not approved_by or not approval_ref:
            raise ValueError("job_id, approved_by and approval_ref are required")
        max_cost = float(max_cost_usd)
        if max_cost < 0:
            raise ValueError("max_cost_usd_must_be_nonnegative")
        ttl = max(30, min(int(ttl_seconds), 86400))

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_fabric_authority_state
                    WHERE authority_id='FABRIC'
                    FOR UPDATE
                    """
                )
                authority = cur.fetchone()
                if authority is None:
                    raise FabricAuthorityError("fabric_authority_state_missing")
                current_version = int(authority["current_shared_state_version"])
                if current_version <= 0:
                    raise FabricAuthorityStale("authority_state_uninitialized")

                cur.execute(
                    """
                    SELECT j.*,e.envelope_hash,e.authority_class,e.cost_policy,
                           e.approval_required
                    FROM jaytec_jobs j
                    JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    WHERE j.job_id=%s
                    FOR UPDATE OF j,e
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise FabricAuthorityError("job_or_envelope_not_found:" + job_id)
                if int(row["source_shared_state_version"]) != current_version:
                    raise FabricAuthorityStale("stale_source_shared_state_version")
                policy = normalize_cost_policy(row.get("cost_policy") or {})
                if float(policy["max_cost_usd"]) > max_cost:
                    raise FabricApprovalRequired("approval_cost_cap_too_low")
                if (
                    policy["mode"] == "ZERO_SPEND"
                    and max_cost != 0
                    and str(row["authority_class"]).upper() not in HIGH_RISK_AUTHORITY_CLASSES
                ):
                    raise FabricApprovalRequired("unnecessary_paid_cap_for_zero_spend_job")

                cur.execute(
                    """
                    SELECT *
                    FROM jaytec_fabric_approvals
                    WHERE job_id=%s
                      AND state='APPROVED'
                      AND (expires_at IS NULL OR expires_at > now())
                    FOR UPDATE
                    """,
                    (job_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    exact = (
                        existing["envelope_hash"] == row["envelope_hash"]
                        and int(existing["source_shared_state_version"]) == current_version
                        and existing["authority_class"] == row["authority_class"]
                        and existing["approval_ref"] == approval_ref
                        and float(existing["max_cost_usd"]) >= float(policy["max_cost_usd"])
                    )
                    if exact:
                        return dict(existing)
                    raise FabricApprovalRequired("different_active_approval_exists")

                approval_payload = {
                    "job_id": job_id,
                    "envelope_hash": row["envelope_hash"],
                    "source_shared_state_version": current_version,
                    "authority_class": row["authority_class"],
                    "approved_by": approved_by,
                    "approval_ref": approval_ref,
                    "max_cost_usd": round(max_cost, 4),
                    "authority_epoch": int(authority["authority_epoch"]),
                }
                approval_id = "approval-" + _hash(approval_payload)[:24]
                cur.execute(
                    """
                    INSERT INTO jaytec_fabric_approvals(
                      approval_id,job_id,envelope_hash,source_shared_state_version,
                      authority_class,approved_by,approval_ref,max_cost_usd,
                      state,expires_at
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,'APPROVED',
                      now()+(%s*interval '1 second')
                    )
                    RETURNING *
                    """,
                    (
                        approval_id,
                        job_id,
                        row["envelope_hash"],
                        current_version,
                        row["authority_class"],
                        approved_by,
                        approval_ref,
                        round(max_cost, 4),
                        ttl,
                    ),
                )
                approval = cur.fetchone()
                if approval is None:
                    raise FabricAuthorityError("approval_insert_failed")

                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status='QUEUED',
                        fabric_state='QUEUED',
                        blockers='[]'::jsonb,
                        next_attempt_at=now(),
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                      AND status='BLOCKED'
                      AND fabric_state='BLOCKED_OWNER'
                      AND cancel_requested_at IS NULL
                    RETURNING job_id
                    """,
                    (job_id,),
                )
                cur.execute(
                    """
                    INSERT INTO jaytec_job_events(
                      job_id,event_type,source,source_version,payload
                    ) VALUES (
                      %s,'FABRIC_JOB_APPROVED','FIVE_SEAT_AUTHORITY',%s,%s::jsonb
                    )
                    """,
                    (
                        job_id,
                        current_version,
                        _json(
                            {
                                "approval_id": approval_id,
                                "approval_ref": approval_ref,
                                "approved_by": approved_by,
                                "max_cost_usd": round(max_cost, 4),
                            }
                        ),
                    ),
                )
                cur.execute(
                    "SELECT pg_notify(%s,%s)",
                    (
                        WORK_AVAILABLE_CHANNEL,
                        _json({"reason":"fabric_job_approved","job_id":job_id}),
                    ),
                )
                return dict(approval)

    def revoke_job(
        self,
        job_id: str,
        *,
        revoked_by: str,
        reason: str,
    ) -> None:
        job_id = str(job_id or "").strip()
        revoked_by = str(revoked_by or "").strip()
        reason = str(reason or "").strip()
        if not job_id or not revoked_by or not reason:
            raise ValueError("job_id, revoked_by and reason are required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE jaytec_fabric_approvals
                    SET state='REVOKED',revoked_at=now(),updated_at=now()
                    WHERE job_id=%s AND state='APPROVED'
                    """,
                    (job_id,),
                )
                cur.execute(
                    """
                    UPDATE jaytec_jobs
                    SET status=CASE WHEN status='RUNNING' THEN status ELSE 'BLOCKED' END,
                        fabric_state=CASE
                          WHEN status='RUNNING' THEN 'CANCEL_REQUESTED'
                          ELSE 'BLOCKED_OWNER'
                        END,
                        cancel_requested_at=CASE
                          WHEN status='RUNNING' THEN COALESCE(cancel_requested_at,now())
                          ELSE cancel_requested_at
                        END,
                        blockers=%s::jsonb,
                        version=version+1,
                        updated_at=now()
                    WHERE job_id=%s
                    """,
                    (
                        _json(
                            [
                                {
                                    "source":"AUTHORITY_REVOKE",
                                    "revoked_by":revoked_by,
                                    "reason":reason,
                                }
                            ]
                        ),
                        job_id,
                    ),
                )
