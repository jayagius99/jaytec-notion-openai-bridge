from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras

from concurrency import SCHEDULER_LOCK_KEY, UNRESOLVED_OPERATION_STATUSES
from five_seat_runtime import FiveSeatLeaseToken, FiveSeatStaleLease, WORKER_SEAT_IDS

WATCH_LOCK_KEY = "jaytec:five-seat-watch-leader:v1"
WATCH_DECISIONS = frozenset({"ACCEPT", "REWORK", "BLOCK", "ESCALATE"})

class WatchRuntimeError(RuntimeError): pass
class WatchStaleLeader(WatchRuntimeError): pass
class WatchReviewBlocked(WatchRuntimeError): pass

@dataclass(frozen=True)
class WatchLeaderToken:
    leader_id: str
    leader_epoch: int
    fence_token: int
    lease_expires_at: datetime


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(dict(evidence)).encode("utf-8")).hexdigest()


class PostgresFiveSeatWatch:
    """Out-of-band singleton WATCH leader and independent review barrier."""
    def __init__(self, database_url: str):
        if not database_url: raise ValueError("database_url is required")
        self.database_url = database_url

    def _connect(self): return psycopg2.connect(self.database_url)

    def claim_leader(self, *, leader_id: str, lease_seconds: int = 60) -> Optional[WatchLeaderToken]:
        if not leader_id: raise ValueError("leader_id is required")
        if lease_seconds < 10 or lease_seconds > 3600: raise ValueError("lease_seconds must be between 10 and 3600")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (WATCH_LOCK_KEY,))
                cur.execute("SELECT * FROM jaytec_watch_leader WHERE singleton_id=1 FOR UPDATE")
                row = cur.fetchone()
                if row is None: raise WatchRuntimeError("watch_singleton_missing")
                now = datetime.now(timezone.utc)
                if row["state"] == "LEADER" and row["lease_expires_at"] is not None and row["lease_expires_at"] > now and row["leader_id"] != leader_id:
                    return None
                cur.execute("""
                    UPDATE jaytec_watch_leader
                    SET state='LEADER',leader_id=%s,
                        lease_expires_at=now()+(%s*interval '1 second'),
                        leader_epoch=leader_epoch+1,fence_token=fence_token+1,
                        version=version+1,updated_at=now()
                    WHERE singleton_id=1
                    RETURNING leader_epoch,fence_token,lease_expires_at
                """, (leader_id, lease_seconds))
                claimed=cur.fetchone()
                return WatchLeaderToken(leader_id,int(claimed["leader_epoch"]),int(claimed["fence_token"]),claimed["lease_expires_at"])

    def heartbeat(self, token: WatchLeaderToken, *, lease_seconds: int = 60) -> WatchLeaderToken:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    UPDATE jaytec_watch_leader SET lease_expires_at=now()+(%s*interval '1 second'),version=version+1,updated_at=now()
                    WHERE singleton_id=1 AND state='LEADER' AND leader_id=%s AND leader_epoch=%s AND fence_token=%s AND lease_expires_at>now()
                    RETURNING lease_expires_at
                """, (lease_seconds,token.leader_id,token.leader_epoch,token.fence_token))
                row=cur.fetchone()
                if row is None: raise WatchStaleLeader(token.leader_id)
                return WatchLeaderToken(token.leader_id,token.leader_epoch,token.fence_token,row["lease_expires_at"])

    def write_handoff_and_retire(self, worker: FiveSeatLeaseToken, *, handoff_id: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
        """Worker may submit evidence and retire; it cannot approve its own work."""
        if not handoff_id: raise ValueError("handoff_id is required")
        if worker.seat_id not in WORKER_SEAT_IDS: raise ValueError("invalid seat")
        digest=evidence_hash(evidence)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEDULER_LOCK_KEY,))
                cur.execute("SELECT * FROM jaytec_worker_seats WHERE seat_id=%s FOR UPDATE",(worker.seat_id,))
                seat=cur.fetchone()
                cur.execute("SELECT * FROM jaytec_jobs WHERE job_id=%s FOR UPDATE",(worker.job_id,))
                job=cur.fetchone()
                now=datetime.now(timezone.utc)
                if seat is None or job is None or seat["state"]!="RUNNING" or seat["current_job_id"]!=worker.job_id or seat["worker_id"]!=worker.owner or seat["lease_owner"]!=worker.owner or int(seat["seat_epoch"])!=worker.seat_epoch or int(seat["fence_token"])!=worker.seat_fence_token or seat["lease_expires_at"] is None or seat["lease_expires_at"]<=now:
                    raise FiveSeatStaleLease(worker.job_id)
                if job["status"]!="RUNNING" or job["fabric_state"]!="RUNNING" or job["seat_id"]!=worker.seat_id or job["lease_owner"]!=worker.owner or int(job["ownership_epoch"])!=worker.ownership_epoch or int(job["fence_token"])!=worker.job_fence_token or job["lease_expires_at"] is None or job["lease_expires_at"]<=now:
                    raise FiveSeatStaleLease(worker.job_id)
                cur.execute("SELECT operation_id FROM jaytec_operations WHERE job_id=%s AND status=ANY(%s)",(worker.job_id,list(UNRESOLVED_OPERATION_STATUSES)))
                unresolved=[str(r["operation_id"]) for r in cur.fetchall()]
                if unresolved: raise WatchReviewBlocked("unresolved_operations:"+",".join(unresolved))
                cur.execute("""
                    INSERT INTO jaytec_worker_handoffs(handoff_id,job_id,seat_id,worker_id,job_ownership_epoch,job_fence_token,seat_epoch,seat_fence_token,evidence_hash,evidence)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (handoff_id) DO NOTHING RETURNING handoff_id
                """,(handoff_id,worker.job_id,worker.seat_id,worker.owner,worker.ownership_epoch,worker.job_fence_token,worker.seat_epoch,worker.seat_fence_token,digest,_json(dict(evidence))))
                inserted=cur.fetchone()
                if inserted is None:
                    cur.execute("SELECT evidence_hash,job_id,worker_id FROM jaytec_worker_handoffs WHERE handoff_id=%s",(handoff_id,))
                    existing=cur.fetchone()
                    if existing is None or existing["evidence_hash"]!=digest or existing["job_id"]!=worker.job_id or existing["worker_id"]!=worker.owner:
                        raise WatchRuntimeError("conflicting_handoff_id")
                cur.execute("""UPDATE jaytec_jobs SET status='PAUSED',fabric_state='HANDOFF_PENDING_REVIEW',seat_id=NULL,lease_owner=NULL,lease_expires_at=NULL,checkpoint_ref=%s,version=version+1,updated_at=now() WHERE job_id=%s AND seat_id=%s AND lease_owner=%s AND ownership_epoch=%s AND fence_token=%s RETURNING job_id""",(handoff_id,worker.job_id,worker.seat_id,worker.owner,worker.ownership_epoch,worker.job_fence_token))
                if cur.fetchone() is None: raise FiveSeatStaleLease(worker.job_id)
                cur.execute("""UPDATE jaytec_worker_seats SET state='FREE',current_job_id=NULL,worker_id=NULL,lease_owner=NULL,lease_expires_at=NULL,last_handoff_ref=%s,version=version+1,updated_at=now() WHERE seat_id=%s AND current_job_id=%s AND worker_id=%s AND seat_epoch=%s AND fence_token=%s RETURNING seat_id""",(handoff_id,worker.seat_id,worker.job_id,worker.owner,worker.seat_epoch,worker.seat_fence_token))
                if cur.fetchone() is None: raise FiveSeatStaleLease(worker.job_id)
                return {"handoff_id":handoff_id,"evidence_hash":digest,"job_id":worker.job_id,"seat_id":worker.seat_id}

    def review(self, leader: WatchLeaderToken, *, handoff_id: str, decision: str, reason: str, evidence: Optional[Mapping[str,Any]]=None) -> Dict[str,Any]:
        decision=str(decision).upper()
        if decision not in WATCH_DECISIONS: raise ValueError("invalid decision")
        if not reason: raise ValueError("reason is required")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (WATCH_LOCK_KEY,))
                cur.execute("SELECT * FROM jaytec_watch_leader WHERE singleton_id=1 FOR UPDATE")
                w=cur.fetchone(); now=datetime.now(timezone.utc)
                if w is None or w["state"]!="LEADER" or w["leader_id"]!=leader.leader_id or int(w["leader_epoch"])!=leader.leader_epoch or int(w["fence_token"])!=leader.fence_token or w["lease_expires_at"] is None or w["lease_expires_at"]<=now:
                    raise WatchStaleLeader(leader.leader_id)
                cur.execute("SELECT * FROM jaytec_worker_handoffs WHERE handoff_id=%s FOR UPDATE",(handoff_id,)); h=cur.fetchone()
                if h is None: raise WatchReviewBlocked("handoff_missing")
                if h["worker_id"]==leader.leader_id: raise WatchReviewBlocked("self_approval_forbidden")
                cur.execute("SELECT * FROM jaytec_watch_reviews WHERE handoff_id=%s",(handoff_id,)); existing=cur.fetchone()
                if existing is not None:
                    if existing["decision"]==decision and existing["watch_leader_id"]==leader.leader_id: return dict(existing)
                    raise WatchReviewBlocked("handoff_already_reviewed")
                job_id=str(h["job_id"])
                target={"ACCEPT":("SUCCEEDED","SUCCEEDED"),"REWORK":("PAUSED","REWORK_QUEUED"),"BLOCK":("PAUSED","FAILED_SAFE"),"ESCALATE":("PAUSED","ESCALATED")}[decision]
                cur.execute("""UPDATE jaytec_jobs SET status=%s,fabric_state=%s,version=version+1,updated_at=now() WHERE job_id=%s AND status='PAUSED' AND fabric_state='HANDOFF_PENDING_REVIEW' AND checkpoint_ref=%s RETURNING job_id""",(target[0],target[1],job_id,handoff_id))
                if cur.fetchone() is None: raise WatchReviewBlocked("job_not_pending_current_handoff")
                cur.execute("""INSERT INTO jaytec_watch_reviews(handoff_id,job_id,watch_leader_id,watch_leader_epoch,watch_fence_token,decision,reason,evidence) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",(handoff_id,job_id,leader.leader_id,leader.leader_epoch,leader.fence_token,decision,reason,_json(dict(evidence or {}))))
                return dict(cur.fetchone())
