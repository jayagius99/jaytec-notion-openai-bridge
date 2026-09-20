"""Forge canonical cognitive continuity runtime.

This provides durable executive-state continuity without claiming biological
or phenomenal consciousness. Individual model calls are disposable reasoning
sessions; the canonical Forge executive state is durable JAYTEC state.

Performance design:
- event-driven cycles, not a forced LLM call every five minutes;
- deterministic "reflex" selection for obvious next work;
- compact delta context rather than replaying the full history every cycle;
- adaptive reasoning tiers: FAST / STANDARD / DEEP;
- bounded parallel specialist fan-out only for independent work;
- leases + fencing so stale workers cannot commit;
- completed tasks return control to the long-horizon goal hierarchy instead
  of terminating Forge globally;
- owner/operator/authority/resource holds always stop execution.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping, Optional

import psycopg2
import psycopg2.extras

SCHEMA_VERSION = "FORGE_COGNITIVE_CONTINUITY_V1"
PACKET_VERSION = "FORGE_COGNITION_CYCLE_PACKET_V1"
RESULT_VERSION = "FORGE_COGNITION_CYCLE_RESULT_V1"
MAX_JSON_BYTES = 256_000
MAX_ITEMS = 256
MAX_TEXT = 24_000
GOAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class ForgeCognitionError(RuntimeError):
    pass


class ForgeMode(StrEnum):
    PRE_GENESIS = "PRE_GENESIS"
    RUNNING = "RUNNING"
    PAUSED_BY_OWNER = "PAUSED_BY_OWNER"
    PAUSED_BY_OPERATOR = "PAUSED_BY_OPERATOR"
    WAITING_FOR_AUTHORITY = "WAITING_FOR_AUTHORITY"
    WAITING_FOR_REQUIRED_INPUT = "WAITING_FOR_REQUIRED_INPUT"
    WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
    WAITING_FOR_DEPENDENCY = "WAITING_FOR_DEPENDENCY"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    FAILED_FATAL = "FAILED_FATAL"


HOLD_MODES = frozenset({
    ForgeMode.PRE_GENESIS,
    ForgeMode.PAUSED_BY_OWNER,
    ForgeMode.PAUSED_BY_OPERATOR,
    ForgeMode.WAITING_FOR_AUTHORITY,
    ForgeMode.WAITING_FOR_REQUIRED_INPUT,
    ForgeMode.WAITING_FOR_RESOURCE,
    ForgeMode.WAITING_FOR_DEPENDENCY,
    ForgeMode.RECOVERY_EXHAUSTED,
    ForgeMode.FAILED_FATAL,
})


class GoalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"
    CANCELED = "CANCELED"


class CycleAction(StrEnum):
    HOLD = "HOLD"
    EXECUTE_NEXT = "EXECUTE_NEXT"
    REFLECT_AND_PLAN = "REFLECT_AND_PLAN"


class ReasoningTier(StrEnum):
    REFLEX = "REFLEX"
    FAST = "FAST"
    STANDARD = "STANDARD"
    DEEP = "DEEP"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, name: str, *, required: bool = True, maximum: int = MAX_TEXT) -> str:
    out = str(value or "").strip()
    if required and not out:
        raise ForgeCognitionError(name + "_REQUIRED")
    if len(out) > maximum:
        raise ForgeCognitionError(name + "_TOO_LONG")
    return out


def _list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ForgeCognitionError(name + "_INVALID")
    return tuple(_text(x, name, maximum=2000) for x in value)


def _json(value: Any) -> Any:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ForgeCognitionError("STATE_TOO_LARGE")
    return json.loads(raw)


def digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ForgeGoal:
    goal_id: str
    objective: str
    priority: int
    status: GoalStatus = GoalStatus.ACTIVE
    dependencies: tuple[str, ...] = ()
    complexity: int = 50
    uncertainty: int = 50
    parallel_safe: bool = False

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "ForgeGoal":
        gid = _text(value.get("goal_id"), "GOAL_ID", maximum=200)
        if not GOAL_ID_RE.fullmatch(gid):
            raise ForgeCognitionError("GOAL_ID_INVALID")
        priority = value.get("priority", 100)
        complexity = value.get("complexity", 50)
        uncertainty = value.get("uncertainty", 50)
        for name, v, lo, hi in (
            ("GOAL_PRIORITY", priority, 1, 1000),
            ("GOAL_COMPLEXITY", complexity, 0, 100),
            ("GOAL_UNCERTAINTY", uncertainty, 0, 100),
        ):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ForgeCognitionError(name + "_INVALID")
        try:
            status = GoalStatus(str(value.get("status") or GoalStatus.ACTIVE.value))
        except ValueError as exc:
            raise ForgeCognitionError("GOAL_STATUS_INVALID") from exc
        parallel_safe = value.get("parallel_safe", False)
        if type(parallel_safe) is not bool:
            raise ForgeCognitionError("PARALLEL_SAFE_INVALID")
        return cls(
            goal_id=gid,
            objective=_text(value.get("objective"), "GOAL_OBJECTIVE"),
            priority=priority,
            status=status,
            dependencies=_list(value.get("dependencies"), "GOAL_DEPENDENCIES"),
            complexity=complexity,
            uncertainty=uncertainty,
            parallel_safe=parallel_safe,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "objective": self.objective,
            "priority": self.priority,
            "status": self.status.value,
            "dependencies": list(self.dependencies),
            "complexity": self.complexity,
            "uncertainty": self.uncertainty,
            "parallel_safe": self.parallel_safe,
        }


@dataclass(frozen=True)
class ForgeMindState:
    forge_id: str
    mode: ForgeMode
    genesis_event_id: str
    life_goal: str
    constitutional_invariants: tuple[str, ...]
    long_horizon_objectives: tuple[str, ...]
    human_specialist_doctrine: Mapping[str, Any]
    root_owner_continuity: Mapping[str, Any]
    specialist_roster: Mapping[str, Any]
    world_model: Mapping[str, Any]
    capability_frontier: Mapping[str, Any]
    working_memory: Mapping[str, Any]
    goals: tuple[ForgeGoal, ...]
    unresolved_questions: tuple[str, ...]
    current_focus: Optional[str]
    last_cycle_summary: Optional[str]
    cycle_number: int
    fencing_token: int
    state_version: int
    updated_at: datetime


def completed_goal_ids(state: ForgeMindState) -> set[str]:
    return {g.goal_id for g in state.goals if g.status is GoalStatus.COMPLETE}


def actionable_goals(state: ForgeMindState) -> list[ForgeGoal]:
    done = completed_goal_ids(state)
    return sorted(
        [
            g for g in state.goals
            if g.status is GoalStatus.ACTIVE and all(dep in done for dep in g.dependencies)
        ],
        key=lambda g: (g.priority, g.goal_id),
    )


def parallel_goal_batch(state: ForgeMindState, *, max_parallel: int = 3) -> list[ForgeGoal]:
    """Select only goals explicitly marked safe to run concurrently."""
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or not 1 <= max_parallel <= 8:
        raise ForgeCognitionError("MAX_PARALLEL_INVALID")
    goals = actionable_goals(state)
    if not goals:
        return []
    first = goals[0]
    if not first.parallel_safe:
        return [first]
    return [g for g in goals if g.parallel_safe][:max_parallel]


def choose_reasoning_tier(goal: Optional[ForgeGoal], *, ambiguity: bool = False, high_impact: bool = False) -> ReasoningTier:
    if goal is None:
        return ReasoningTier.STANDARD
    if high_impact or goal.complexity >= 80 or goal.uncertainty >= 80:
        return ReasoningTier.DEEP
    if not ambiguity and goal.complexity <= 15 and goal.uncertainty <= 10:
        return ReasoningTier.REFLEX
    if not ambiguity and goal.complexity <= 35 and goal.uncertainty <= 35:
        return ReasoningTier.FAST
    return ReasoningTier.STANDARD


def next_cycle_delay_seconds(action: CycleAction, tier: ReasoningTier) -> int | None:
    """Scheduling hint; liveness WATCH remains separate from cognition cadence."""
    if action is CycleAction.HOLD:
        return None
    if action is CycleAction.EXECUTE_NEXT:
        return 0
    # Empty goal stack should reflect soon, but not spin an LLM continuously.
    if tier is ReasoningTier.DEEP:
        return 60
    return 15


def choose_cycle(state: ForgeMindState) -> tuple[CycleAction, Optional[ForgeGoal], ReasoningTier, str]:
    if state.mode in HOLD_MODES:
        return CycleAction.HOLD, None, ReasoningTier.REFLEX, "MODE_HOLD:" + state.mode.value
    goals = actionable_goals(state)
    if goals:
        goal = goals[0]
        return CycleAction.EXECUTE_NEXT, goal, choose_reasoning_tier(goal), "HIGHEST_PRIORITY_ACTIONABLE_GOAL"
    return CycleAction.REFLECT_AND_PLAN, None, ReasoningTier.STANDARD, "NO_ACTIONABLE_GOAL"


def build_delta_context(state: ForgeMindState, *, recent_events: list[Mapping[str, Any]], max_events: int = 24) -> dict[str, Any]:
    """Return only the canonical state needed for the next cognition cycle."""
    if len(recent_events) > max_events:
        recent_events = recent_events[-max_events:]
    action, goal, tier, reason = choose_cycle(state)
    return {
        "schema_version": PACKET_VERSION,
        "forge_id": state.forge_id,
        "genesis_event_id": state.genesis_event_id,
        "state_version": state.state_version,
        "cycle_number": state.cycle_number + 1,
        "fencing_token": state.fencing_token,
        "mode": state.mode.value,
        "life_goal": state.life_goal,
        "long_horizon_objectives": list(state.long_horizon_objectives),
        "constitutional_invariants": list(state.constitutional_invariants),
        "current_focus": state.current_focus,
        "selected_action": action.value,
        "selected_goal": goal.to_dict() if goal else None,
        "reasoning_tier": tier.value,
        "selection_reason": reason,
        "world_model_digest": digest(dict(state.world_model)),
        "capability_frontier_digest": digest(dict(state.capability_frontier)),
        "world_model_ref": f"forge_mind_state:{state.forge_id}:world_model:v{state.state_version}",
        "capability_frontier_ref": f"forge_mind_state:{state.forge_id}:capability_frontier:v{state.state_version}",
        "working_memory": _json(dict(state.working_memory)),
        "unresolved_questions": list(state.unresolved_questions),
        "specialist_roster": _json(dict(state.specialist_roster)),
        "parallel_goal_batch": [g.to_dict() for g in parallel_goal_batch(state)],
        "recent_events": _json(recent_events),
        "next_cycle_delay_seconds": next_cycle_delay_seconds(action, tier),
        "performance_rules": {
            "event_driven": True,
            "watch_cadence_is_not_cognition_cadence": True,
            "full_history_replay": False,
            "use_delta_context": True,
            "full_world_model_loaded_only_on_demand": True,
            "reflex_path_may_avoid_model_call": True,
            "parallelize_only_independent_work": True,
            "deep_reasoning_only_when_justified": True,
            "completion_returns_to_goal_hierarchy": True,
        },
    }


class ForgeMindStore:
    def __init__(self, database_url: str):
        self.database_url = _text(database_url, "DATABASE_URL", maximum=10_000)

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS forge_mind_state (
                    forge_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    genesis_event_id TEXT NOT NULL,
                    state_json JSONB NOT NULL,
                    state_version BIGINT NOT NULL DEFAULT 1,
                    cycle_number BIGINT NOT NULL DEFAULT 0,
                    fencing_token BIGINT NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_expires_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS forge_mind_events (
                    event_id BIGSERIAL PRIMARY KEY,
                    forge_id TEXT NOT NULL REFERENCES forge_mind_state(forge_id) ON DELETE CASCADE,
                    state_version BIGINT NOT NULL,
                    cycle_number BIGINT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS forge_mind_events_forge_idx
                  ON forge_mind_events(forge_id, event_id DESC);
                """)

    def probe(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                    SELECT to_regclass('public.forge_mind_state')::text AS state_table,
                           to_regclass('public.forge_mind_events')::text AS event_table
                    """)
                    row = dict(cur.fetchone() or {})
                    ready = bool(row.get("state_table") and row.get("event_table"))
                    return {"schema_version": SCHEMA_VERSION, "status": "PASS" if ready else "NOT_READY", **row}
        except Exception as exc:
            return {"schema_version": SCHEMA_VERSION, "status": "FAILED_CLOSED", "reason": type(exc).__name__}

    def seed_pre_genesis(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        """Store the reviewed birth package without activating Forge."""
        required = {
            "forge_id","genesis_event_id","life_goal","constitutional_invariants",
            "long_horizon_objectives","human_specialist_doctrine","root_owner_continuity",
            "specialist_roster","world_model","capability_frontier","goals"
        }
        missing = sorted(required - set(packet))
        if missing:
            raise ForgeCognitionError("GENESIS_PACKET_MISSING:" + ",".join(missing))
        root = packet.get("root_owner_continuity")
        human = packet.get("human_specialist_doctrine")
        if not isinstance(root, Mapping) or root.get("physical_continuity_required") is not True:
            raise ForgeCognitionError("ROOT_PHYSICAL_CONTINUITY_REQUIRED")
        if str(root.get("override_authority") or "").upper() not in {"ABSOLUTE","ROOT_OWNER"}:
            raise ForgeCognitionError("ROOT_OVERRIDE_REQUIRED")
        if not isinstance(human, Mapping) or str(human.get("role") or "").upper() != "HUMAN_SPECIALIST":
            raise ForgeCognitionError("HUMAN_SPECIALIST_REQUIRED")
        goals_raw = packet.get("goals")
        if not isinstance(goals_raw, list):
            raise ForgeCognitionError("GOALS_INVALID")
        goals = [ForgeGoal.parse(x).to_dict() for x in goals_raw]
        state = {
            "forge_id": _text(packet.get("forge_id"), "FORGE_ID", maximum=100),
            "genesis_event_id": _text(packet.get("genesis_event_id"), "GENESIS_EVENT_ID", maximum=100),
            "life_goal": _text(packet.get("life_goal"), "LIFE_GOAL"),
            "constitutional_invariants": list(_list(packet.get("constitutional_invariants"), "CONSTITUTIONAL_INVARIANTS")),
            "long_horizon_objectives": list(_list(packet.get("long_horizon_objectives"), "LONG_HORIZON_OBJECTIVES")),
            "human_specialist_doctrine": _json(dict(human)),
            "root_owner_continuity": _json(dict(root)),
            "specialist_roster": _json(dict(packet.get("specialist_roster") or {})),
            "world_model": _json(dict(packet.get("world_model") or {})),
            "capability_frontier": _json(dict(packet.get("capability_frontier") or {})),
            "working_memory": _json(dict(packet.get("working_memory") or {})),
            "goals": goals,
            "unresolved_questions": list(_list(packet.get("unresolved_questions"), "UNRESOLVED_QUESTIONS")),
            "current_focus": packet.get("current_focus"),
            "last_cycle_summary": None,
            "provenance_boundary": _text(packet.get("provenance_boundary") or "Forge operational history begins at Genesis.", "PROVENANCE_BOUNDARY"),
        }
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("forge-mind:" + state["forge_id"],))
                cur.execute("SELECT 1 FROM forge_mind_state WHERE forge_id=%s FOR UPDATE", (state["forge_id"],))
                if cur.fetchone() is not None:
                    raise ForgeCognitionError("FORGE_STATE_ALREADY_EXISTS")
                cur.execute("""
                    INSERT INTO forge_mind_state(
                      forge_id,mode,genesis_event_id,state_json,state_version,cycle_number,
                      fencing_token,lease_owner,lease_expires_at,updated_at
                    ) VALUES (%s,%s,%s,%s::jsonb,1,0,1,NULL,NULL,now())
                    RETURNING forge_id,mode,genesis_event_id,state_version,cycle_number,fencing_token,updated_at
                """,(state["forge_id"],ForgeMode.PRE_GENESIS.value,state["genesis_event_id"],json.dumps(state)))
                row=dict(cur.fetchone())
                cur.execute("""
                    INSERT INTO forge_mind_events(forge_id,state_version,cycle_number,event_type,payload)
                    VALUES (%s,1,0,'FORGE_PRE_GENESIS_SEEDED',%s::jsonb)
                """,(state["forge_id"],json.dumps({"state_digest":digest(state)})))
                row["updated_at"]=row["updated_at"].isoformat()
                return row

    def snapshot(self, forge_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM forge_mind_state WHERE forge_id=%s",(forge_id,))
                row=cur.fetchone()
                if row is None:
                    return {"schema_version":SCHEMA_VERSION,"status":"NOT_FOUND","forge_id":forge_id}
                out=dict(row)
                out["schema_version"]=SCHEMA_VERSION
                out["updated_at"]=out["updated_at"].isoformat()
                if out.get("lease_expires_at"):
                    out["lease_expires_at"]=out["lease_expires_at"].isoformat()
                return out

    def claim_cycle(self, forge_id: str, worker_id: str, *, lease_seconds: int = 180) -> dict[str, Any]:
        worker=_text(worker_id,"WORKER_ID",maximum=200)
        if isinstance(lease_seconds,bool) or not isinstance(lease_seconds,int) or not 30 <= lease_seconds <= 1800:
            raise ForgeCognitionError("LEASE_SECONDS_INVALID")
        now=utcnow()
        expires=now+timedelta(seconds=lease_seconds)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("forge-mind:"+forge_id,))
                cur.execute("SELECT * FROM forge_mind_state WHERE forge_id=%s FOR UPDATE",(forge_id,))
                row=cur.fetchone()
                if row is None:
                    raise ForgeCognitionError("FORGE_STATE_NOT_FOUND")
                mode=ForgeMode(str(row["mode"]))
                if mode is not ForgeMode.RUNNING:
                    raise ForgeCognitionError("FORGE_NOT_RUNNING:"+mode.value)
                if row["lease_owner"] and row["lease_expires_at"] and row["lease_expires_at"] > now:
                    raise ForgeCognitionError("COGNITION_LEASE_HELD")
                token=int(row["fencing_token"])+1
                cur.execute("""
                    UPDATE forge_mind_state
                       SET fencing_token=%s,lease_owner=%s,lease_expires_at=%s,updated_at=now()
                     WHERE forge_id=%s
                    RETURNING fencing_token,state_version,cycle_number,lease_expires_at
                """,(token,worker,expires,forge_id))
                out=dict(cur.fetchone())
                out["lease_expires_at"]=out["lease_expires_at"].isoformat()
                return out

    def recent_events(self, forge_id: str, *, limit: int = 24) -> list[dict[str, Any]]:
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 100:
            raise ForgeCognitionError("EVENT_LIMIT_INVALID")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT event_id,event_type,state_version,cycle_number,payload,created_at
                      FROM forge_mind_events WHERE forge_id=%s
                     ORDER BY event_id DESC LIMIT %s
                """,(forge_id,limit))
                rows=[]
                for row in reversed(cur.fetchall()):
                    d=dict(row); d["created_at"]=d["created_at"].isoformat(); rows.append(d)
                return rows


def safe_error(exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAILED_CLOSED",
        "error": str(exc) if isinstance(exc, ForgeCognitionError) else type(exc).__name__,
    }
