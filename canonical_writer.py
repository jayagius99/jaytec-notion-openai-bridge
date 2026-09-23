from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import psycopg2
import psycopg2.extras

CANONICAL_WRITER_ID = "CHATGPT-CANONICAL-WRITER"
CANONICAL_APPROVERS = frozenset({"CHATGPT", "ROOT_OWNER"})
TERMINAL_STATES = frozenset(
    {"VERIFIED_COMPLETE", "FAILED_SAFE", "QUARANTINED", "SUPERSEDED", "CANCELLED"}
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_QUEUE_LOCK_KEY = "jaytec-canonical-writer:queue"
_LEDGER_LOCK_KEY = "jaytec-canonical-writer:ledger"


class CanonicalWriterError(RuntimeError):
    pass


class CanonicalWriterConflict(CanonicalWriterError):
    pass


class CanonicalWriterStale(CanonicalWriterError):
    pass


class CanonicalWriterAuthorityError(CanonicalWriterError):
    pass


@dataclass(frozen=True)
class CanonicalWriterToken:
    owner: str
    writer_epoch: int
    fence_token: int
    lease_expires_at: datetime
    write_id: str


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(dict(value)).encode("utf-8")).hexdigest()


def _ledger_hash(prev_hash: str, event: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        (prev_hash + "|" + _json(dict(event))).encode("utf-8")
    ).hexdigest()


def _clean(value: Any, name: str, limit: int = 500) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ValueError(name + "_invalid")
    return text


def _sha(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(text):
        raise ValueError(name + "_invalid")
    return text


class PostgresCanonicalWriterQueue:
    """Durable handoff from WATCH to the one operational canonical editor.

    WATCH may queue only an ACCEPTed candidate. CHATGPT/ROOT_OWNER approval is
    a separate durable gate. Only CHATGPT-CANONICAL-WRITER can claim work.
    This module intentionally has no GitHub, Render, shell, merge or deploy
    client. Canonical mutation stays outside WATCH/worker execution surfaces.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self):
        return psycopg2.connect(self.database_url)

    @staticmethod
    def _append_ledger(
        cur,
        *,
        write_id: str,
        event_type: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LEDGER_LOCK_KEY,))
        cur.execute(
            """
            SELECT event_hash
            FROM jaytec_canonical_write_ledger
            ORDER BY event_id DESC
            LIMIT 1
            FOR UPDATE
            """
        )
        prior = cur.fetchone()
        prev_hash = str(prior["event_hash"]) if prior else "GENESIS"
        body = {
            "write_id": write_id,
            "event_type": event_type,
            "actor": actor,
            "payload": dict(payload or {}),
        }
        event_hash = _ledger_hash(prev_hash, body)
        cur.execute(
            """
            INSERT INTO jaytec_canonical_write_ledger(
              write_id,event_type,actor,payload,prev_hash,event_hash
            ) VALUES (%s,%s,%s,%s::jsonb,%s,%s)
            RETURNING *
            """,
            (
                write_id,
                event_type,
                actor,
                _json(dict(payload or {})),
                prev_hash,
                event_hash,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise CanonicalWriterError("ledger_append_failed")
        return dict(row)

    def enqueue_from_watch(
        self,
        *,
        job_id: str,
        handoff_id: str,
        review_id: str,
        repository: str,
        pull_request_number: int,
        candidate_head_sha: str,
        expected_base_sha: str,
        candidate_digest: str,
        source_shared_state_version: int,
        idempotency_key: str,
        requested_by: str,
        priority: int = 100,
        deployment_target: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        job_id = _clean(job_id, "job_id", 200)
        handoff_id = _clean(handoff_id, "handoff_id", 240)
        review_id = _clean(review_id, "review_id", 240)
        repository = _clean(repository, "repository", 200)
        idempotency_key = _clean(idempotency_key, "idempotency_key", 300)
        requested_by = _clean(requested_by, "requested_by", 240)
        candidate_head_sha = _sha(candidate_head_sha, "candidate_head_sha")
        expected_base_sha = _sha(expected_base_sha, "expected_base_sha")
        candidate_digest = str(candidate_digest or "").strip().lower()
        if not _DIGEST_RE.fullmatch(candidate_digest):
            raise ValueError("candidate_digest_invalid")
        if int(pull_request_number) <= 0:
            raise ValueError("pull_request_number_invalid")
        source_version = int(source_shared_state_version)
        if source_version <= 0:
            raise ValueError("source_shared_state_version_invalid")
        priority = max(0, min(int(priority), 1000))
        envelope = {
            "job_id": job_id,
            "handoff_id": handoff_id,
            "review_id": review_id,
            "repository": repository,
            "pull_request_number": int(pull_request_number),
            "candidate_head_sha": candidate_head_sha,
            "expected_base_sha": expected_base_sha,
            "candidate_digest": candidate_digest,
            "source_shared_state_version": source_version,
            "idempotency_key": idempotency_key,
        }
        write_id = "canonical-" + _digest(envelope)[:24]

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_QUEUE_LOCK_KEY,))
                cur.execute(
                    """
                    SELECT r.decision,j.source_shared_state_version,
                           a.current_shared_state_version
                    FROM jaytec_watch_reviews r
                    JOIN jaytec_worker_handoffs h ON h.handoff_id=r.handoff_id
                    JOIN jaytec_jobs j ON j.job_id=r.job_id
                    JOIN jaytec_fabric_authority_state a ON a.authority_id='FABRIC'
                    WHERE r.review_id=%s AND r.job_id=%s AND r.handoff_id=%s
                    FOR UPDATE OF r,j,a
                    """,
                    (review_id, job_id, handoff_id),
                )
                review = cur.fetchone()
                if review is None:
                    raise CanonicalWriterAuthorityError("watch_review_not_found")
                if str(review["decision"]).upper() != "ACCEPT":
                    raise CanonicalWriterAuthorityError("watch_accept_required")
                if int(review["source_shared_state_version"]) != source_version:
                    raise CanonicalWriterStale("job_source_shared_state_version_mismatch")
                if int(review["current_shared_state_version"]) != source_version:
                    raise CanonicalWriterStale("current_shared_state_version_mismatch")

                cur.execute(
                    """
                    SELECT * FROM jaytec_canonical_write_queue
                    WHERE idempotency_key=%s FOR UPDATE
                    """,
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    exact = (
                        existing["write_id"] == write_id
                        and existing["review_id"] == review_id
                        and existing["candidate_head_sha"] == candidate_head_sha
                        and existing["expected_base_sha"] == expected_base_sha
                        and existing["candidate_digest"] == candidate_digest
                    )
                    if exact:
                        return dict(existing)
                    raise CanonicalWriterConflict("idempotency_key_conflict")

                cur.execute(
                    """
                    INSERT INTO jaytec_canonical_write_queue(
                      write_id,job_id,handoff_id,review_id,idempotency_key,
                      repository,pull_request_number,candidate_head_sha,
                      expected_base_sha,candidate_digest,
                      source_shared_state_version,deployment_target,
                      requested_by,priority,state
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,
                      'PENDING_CHATGPT_APPROVAL'
                    )
                    RETURNING *
                    """,
                    (
                        write_id,
                        job_id,
                        handoff_id,
                        review_id,
                        idempotency_key,
                        repository,
                        int(pull_request_number),
                        candidate_head_sha,
                        expected_base_sha,
                        candidate_digest,
                        source_version,
                        _json(dict(deployment_target or {})),
                        requested_by,
                        priority,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise CanonicalWriterError("canonical_queue_insert_failed")
                self._append_ledger(
                    cur,
                    write_id=write_id,
                    event_type="WATCH_ENQUEUED",
                    actor=requested_by,
                    payload=envelope,
                )
                return dict(row)

    def approve(
        self,
        write_id: str,
        *,
        approved_by: str,
        approval_ref: str,
        expected_candidate_digest: str,
        expected_base_sha: str,
        expected_source_shared_state_version: int,
    ) -> dict[str, Any]:
        write_id = _clean(write_id, "write_id", 240)
        approved_by = _clean(approved_by, "approved_by", 100).upper()
        approval_ref = _clean(approval_ref, "approval_ref", 500)
        if approved_by not in CANONICAL_APPROVERS:
            raise CanonicalWriterAuthorityError("canonical_approver_not_allowed")
        digest = str(expected_candidate_digest or "").strip().lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("expected_candidate_digest_invalid")
        base_sha = _sha(expected_base_sha, "expected_base_sha")
        source_version = int(expected_source_shared_state_version)

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_QUEUE_LOCK_KEY,))
                cur.execute(
                    """
                    SELECT q.*,a.current_shared_state_version
                    FROM jaytec_canonical_write_queue q
                    JOIN jaytec_fabric_authority_state a ON a.authority_id='FABRIC'
                    WHERE q.write_id=%s
                    FOR UPDATE OF q,a
                    """,
                    (write_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise CanonicalWriterError("write_not_found:" + write_id)
                if row["state"] == "APPROVED":
                    same = (
                        row["approved_by"] == approved_by
                        and row["approval_ref"] == approval_ref
                        and row["candidate_digest"] == digest
                        and row["expected_base_sha"] == base_sha
                        and int(row["source_shared_state_version"]) == source_version
                    )
                    if same:
                        return dict(row)
                    raise CanonicalWriterConflict("different_active_canonical_approval")
                if row["state"] != "PENDING_CHATGPT_APPROVAL":
                    raise CanonicalWriterAuthorityError("write_not_pending_approval")
                if row["candidate_digest"] != digest:
                    raise CanonicalWriterStale("candidate_digest_mismatch")
                if row["expected_base_sha"] != base_sha:
                    raise CanonicalWriterStale("base_sha_mismatch")
                if int(row["source_shared_state_version"]) != source_version:
                    raise CanonicalWriterStale("source_shared_state_version_mismatch")
                if int(row["current_shared_state_version"]) != source_version:
                    raise CanonicalWriterStale("current_shared_state_version_moved")
                cur.execute(
                    """
                    UPDATE jaytec_canonical_write_queue
                    SET state='APPROVED',approved_by=%s,approval_ref=%s,
                        approved_at=now(),updated_at=now()
                    WHERE write_id=%s
                    RETURNING *
                    """,
                    (approved_by, approval_ref, write_id),
                )
                approved = cur.fetchone()
                self._append_ledger(
                    cur,
                    write_id=write_id,
                    event_type="CANONICAL_APPROVED",
                    actor=approved_by,
                    payload={
                        "approval_ref": approval_ref,
                        "candidate_digest": digest,
                        "expected_base_sha": base_sha,
                        "source_shared_state_version": source_version,
                    },
                )
                return dict(approved)

    def claim_next(
        self,
        *,
        owner: str = CANONICAL_WRITER_ID,
        lease_seconds: int = 300,
    ) -> Optional[CanonicalWriterToken]:
        owner = _clean(owner, "owner", 120)
        if owner != CANONICAL_WRITER_ID:
            raise CanonicalWriterAuthorityError(
                "only_chatgpt_canonical_writer_may_claim"
            )
        lease_seconds = max(30, min(int(lease_seconds), 1800))
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_QUEUE_LOCK_KEY,))
                cur.execute(
                    """
                    SELECT * FROM jaytec_canonical_writer_state
                    WHERE writer_id='CANONICAL' FOR UPDATE
                    """
                )
                writer = cur.fetchone()
                if writer is None:
                    raise CanonicalWriterError("canonical_writer_state_missing")
                now = datetime.now(timezone.utc)
                if (
                    writer["lease_owner"]
                    and writer["lease_expires_at"]
                    and writer["lease_expires_at"] > now
                    and writer["lease_owner"] != owner
                ):
                    raise CanonicalWriterAuthorityError("canonical_writer_lease_held")
                cur.execute(
                    """
                    SELECT write_id FROM jaytec_canonical_write_queue
                    WHERE state='IN_FLIGHT' FOR UPDATE
                    """
                )
                if cur.fetchall():
                    raise CanonicalWriterConflict("canonical_write_already_in_flight")
                cur.execute(
                    """
                    SELECT q.*,a.current_shared_state_version
                    FROM jaytec_canonical_write_queue q
                    JOIN jaytec_fabric_authority_state a ON a.authority_id='FABRIC'
                    WHERE q.state='APPROVED'
                    ORDER BY q.priority DESC,q.created_at ASC,q.write_id ASC
                    LIMIT 1 FOR UPDATE OF q,a
                    """
                )
                item = cur.fetchone()
                if item is None:
                    return None
                if int(item["source_shared_state_version"]) != int(
                    item["current_shared_state_version"]
                ):
                    cur.execute(
                        """
                        UPDATE jaytec_canonical_write_queue
                        SET state='SUPERSEDED',
                            last_error='shared_state_version_moved',
                            updated_at=now()
                        WHERE write_id=%s
                        """,
                        (item["write_id"],),
                    )
                    self._append_ledger(
                        cur,
                        write_id=item["write_id"],
                        event_type="SUPERSEDED",
                        actor=owner,
                        payload={"reason": "shared_state_version_moved"},
                    )
                    raise CanonicalWriterStale("shared_state_version_moved")
                cur.execute(
                    """
                    UPDATE jaytec_canonical_writer_state
                    SET lease_owner=%s,
                        lease_expires_at=now()+(%s*interval '1 second'),
                        writer_epoch=writer_epoch+1,
                        fence_token=fence_token+1,
                        active_write_id=%s,
                        version=version+1,updated_at=now()
                    WHERE writer_id='CANONICAL'
                    RETURNING *
                    """,
                    (owner, lease_seconds, item["write_id"]),
                )
                state = cur.fetchone()
                cur.execute(
                    """
                    UPDATE jaytec_canonical_write_queue
                    SET state='IN_FLIGHT',writer_owner=%s,writer_epoch=%s,
                        writer_fence_token=%s,writer_lease_expires_at=%s,
                        attempt_count=attempt_count+1,
                        started_at=COALESCE(started_at,now()),updated_at=now()
                    WHERE write_id=%s AND state='APPROVED'
                    RETURNING write_id
                    """,
                    (
                        owner,
                        int(state["writer_epoch"]),
                        int(state["fence_token"]),
                        state["lease_expires_at"],
                        item["write_id"],
                    ),
                )
                if cur.fetchone() is None:
                    raise CanonicalWriterConflict("canonical_claim_race")
                self._append_ledger(
                    cur,
                    write_id=item["write_id"],
                    event_type="WRITER_CLAIMED",
                    actor=owner,
                    payload={
                        "writer_epoch": int(state["writer_epoch"]),
                        "fence_token": int(state["fence_token"]),
                    },
                )
                return CanonicalWriterToken(
                    owner=owner,
                    writer_epoch=int(state["writer_epoch"]),
                    fence_token=int(state["fence_token"]),
                    lease_expires_at=state["lease_expires_at"],
                    write_id=item["write_id"],
                )

    def complete(
        self,
        token: CanonicalWriterToken,
        *,
        outcome: str,
        canonical_commit_sha: Optional[str] = None,
        verification_evidence: Optional[Mapping[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        outcome = str(outcome or "").strip().upper()
        if outcome not in {"VERIFIED_COMPLETE", "FAILED_SAFE", "QUARANTINED"}:
            raise ValueError("invalid_canonical_outcome")
        evidence = dict(verification_evidence or {})
        if outcome == "VERIFIED_COMPLETE":
            canonical_commit_sha = _sha(
                canonical_commit_sha, "canonical_commit_sha"
            )
            if not evidence or evidence.get("healthy") is not True:
                raise ValueError("healthy_verification_evidence_required")
        elif canonical_commit_sha:
            canonical_commit_sha = _sha(
                canonical_commit_sha, "canonical_commit_sha"
            )

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_QUEUE_LOCK_KEY,))
                cur.execute(
                    """
                    SELECT s.*,q.state AS queue_state
                    FROM jaytec_canonical_writer_state s
                    JOIN jaytec_canonical_write_queue q
                      ON q.write_id=s.active_write_id
                    WHERE s.writer_id='CANONICAL'
                    FOR UPDATE OF s,q
                    """
                )
                row = cur.fetchone()
                if row is None:
                    raise CanonicalWriterStale("no_active_canonical_write")
                if (
                    row["lease_owner"] != token.owner
                    or int(row["writer_epoch"]) != token.writer_epoch
                    or int(row["fence_token"]) != token.fence_token
                    or row["active_write_id"] != token.write_id
                    or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= datetime.now(timezone.utc)
                    or row["queue_state"] != "IN_FLIGHT"
                ):
                    raise CanonicalWriterStale("canonical_writer_fence_stale")
                cur.execute(
                    """
                    UPDATE jaytec_canonical_write_queue
                    SET state=%s,canonical_commit_sha=%s,
                        verification_evidence=%s::jsonb,last_error=%s,
                        completed_at=now(),updated_at=now()
                    WHERE write_id=%s
                    RETURNING *
                    """,
                    (
                        outcome,
                        canonical_commit_sha,
                        _json(evidence),
                        str(error or "")[:2000] or None,
                        token.write_id,
                    ),
                )
                item = cur.fetchone()
                cur.execute(
                    """
                    UPDATE jaytec_canonical_writer_state
                    SET lease_owner=NULL,lease_expires_at=NULL,active_write_id=NULL,
                        version=version+1,updated_at=now()
                    WHERE writer_id='CANONICAL'
                    """
                )
                self._append_ledger(
                    cur,
                    write_id=token.write_id,
                    event_type=outcome,
                    actor=token.owner,
                    payload={
                        "canonical_commit_sha": canonical_commit_sha,
                        "verification_evidence": evidence,
                        "error": str(error or "")[:500],
                    },
                )
                return dict(item)

    def reconcile_watch_accepts(self, *, limit: int = 100) -> dict[str, Any]:
        """Restart-safe WATCH->writer delivery with duplicate collapse."""
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT r.review_id,r.job_id,r.handoff_id,r.controller_owner,
                           h.payload,j.source_shared_state_version,j.priority
                    FROM jaytec_watch_reviews r
                    JOIN jaytec_worker_handoffs h ON h.handoff_id=r.handoff_id
                    JOIN jaytec_jobs j ON j.job_id=r.job_id
                    JOIN jaytec_fabric_envelopes e ON e.job_id=j.job_id
                    LEFT JOIN jaytec_canonical_write_queue q ON q.review_id=r.review_id
                    WHERE r.decision='ACCEPT'
                      AND e.worker_kind='GITHUB_BRANCH_PR'
                      AND q.write_id IS NULL
                    ORDER BY r.created_at ASC,r.review_id ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(row) for row in cur.fetchall()]
        enqueued = []
        errors = []
        for row in rows:
            try:
                payload = row.get("payload") or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)
                result = payload.get("result") if isinstance(payload, Mapping) else None
                if not isinstance(result, Mapping):
                    raise CanonicalWriterError("handoff_result_missing")
                verification = result.get("watch_verification")
                if not isinstance(verification, Mapping):
                    raise CanonicalWriterError("watch_verification_missing")
                head_sha = _sha(
                    verification.get("head_sha"), "candidate_head_sha"
                )
                item = self.enqueue_from_watch(
                    job_id=str(row["job_id"]),
                    handoff_id=str(row["handoff_id"]),
                    review_id=str(row["review_id"]),
                    repository=_clean(
                        verification.get("repository"), "repository", 200
                    ),
                    pull_request_number=int(
                        verification.get("pr_number") or 0
                    ),
                    candidate_head_sha=head_sha,
                    expected_base_sha=_sha(
                        verification.get("base_sha"), "expected_base_sha"
                    ),
                    candidate_digest=hashlib.sha256(
                        _json(dict(result)).encode("utf-8")
                    ).hexdigest(),
                    source_shared_state_version=int(
                        row["source_shared_state_version"]
                    ),
                    idempotency_key=(
                        "canonical:"
                        + str(row["job_id"])
                        + ":"
                        + str(row["handoff_id"])
                        + ":"
                        + head_sha
                    ),
                    requested_by=str(
                        row.get("controller_owner") or "WATCH"
                    ),
                    priority=int(row.get("priority") or 100),
                )
                enqueued.append(str(item["write_id"]))
            except Exception as exc:
                errors.append(
                    {
                        "review_id": str(row.get("review_id") or ""),
                        "error_class": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
        return {"enqueued": enqueued, "errors": errors}

    def contain_expired_inflight(
        self, *, actor: str = "CANONICAL-WRITER-GUARDIAN"
    ) -> list[str]:
        """Expired writer leases quarantine; they are never blindly retried."""
        actor = _clean(actor, "actor", 160)
        contained = []
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_QUEUE_LOCK_KEY,))
                cur.execute(
                    """
                    SELECT s.*,q.state AS queue_state
                    FROM jaytec_canonical_writer_state s
                    LEFT JOIN jaytec_canonical_write_queue q
                      ON q.write_id=s.active_write_id
                    WHERE s.writer_id='CANONICAL'
                    FOR UPDATE OF s,q
                    """
                )
                state = cur.fetchone()
                if (
                    state
                    and state.get("active_write_id")
                    and state.get("queue_state") == "IN_FLIGHT"
                    and state.get("lease_expires_at") is not None
                    and state["lease_expires_at"] <= datetime.now(timezone.utc)
                ):
                    write_id = str(state["active_write_id"])
                    cur.execute(
                        """
                        UPDATE jaytec_canonical_write_queue
                        SET state='QUARANTINED',
                            last_error='canonical_writer_lease_expired_uncertain_effect',
                            completed_at=now(),updated_at=now()
                        WHERE write_id=%s AND state='IN_FLIGHT'
                        """,
                        (write_id,),
                    )
                    cur.execute(
                        """
                        UPDATE jaytec_canonical_writer_state
                        SET lease_owner=NULL,lease_expires_at=NULL,
                            active_write_id=NULL,version=version+1,updated_at=now()
                        WHERE writer_id='CANONICAL'
                        """
                    )
                    self._append_ledger(
                        cur,
                        write_id=write_id,
                        event_type="QUARANTINED",
                        actor=actor,
                        payload={
                            "reason":
                            "canonical_writer_lease_expired_uncertain_effect"
                        },
                    )
                    contained.append(write_id)
        return contained

    def writer_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT writer_id,lease_owner,lease_expires_at,writer_epoch,
                           fence_token,active_write_id,version,updated_at
                    FROM jaytec_canonical_writer_state
                    WHERE writer_id='CANONICAL'
                    """
                )
                row = cur.fetchone()
                if row is None:
                    raise CanonicalWriterError("canonical_writer_state_missing")
                return dict(row)

    def list_queue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT write_id,job_id,handoff_id,review_id,state,priority,
                           repository,pull_request_number,candidate_head_sha,
                           expected_base_sha,candidate_digest,
                           source_shared_state_version,requested_by,approved_by,
                           approval_ref,writer_owner,writer_epoch,
                           writer_fence_token,canonical_commit_sha,last_error,
                           created_at,approved_at,started_at,completed_at,updated_at
                    FROM jaytec_canonical_write_queue
                    ORDER BY
                      CASE state
                        WHEN 'IN_FLIGHT' THEN 0
                        WHEN 'APPROVED' THEN 1
                        WHEN 'PENDING_CHATGPT_APPROVAL' THEN 2
                        ELSE 3
                      END,
                      priority DESC,created_at ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def ledger(
        self, *, write_id: Optional[str] = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if write_id:
                    cur.execute(
                        """
                        SELECT * FROM jaytec_canonical_write_ledger
                        WHERE write_id=%s ORDER BY event_id ASC LIMIT %s
                        """,
                        (str(write_id), limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT * FROM jaytec_canonical_write_ledger
                        ORDER BY event_id DESC LIMIT %s
                        """,
                        (limit,),
                    )
                return [dict(row) for row in cur.fetchall()]
