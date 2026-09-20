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
import select as select_module
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping, Optional

import psycopg2
import psycopg2.extras
from forge_strategic_drives import RootOwnerBoundary, StrategicDriveConfig

SCHEMA_VERSION = "FORGE_COGNITIVE_CONTINUITY_V1"
PACKET_VERSION = "FORGE_COGNITION_CYCLE_PACKET_V1"
RESULT_VERSION = "FORGE_COGNITION_CYCLE_RESULT_V1"
MAX_JSON_BYTES = 256_000
MAX_ITEMS = 256
MAX_TEXT = 24_000
GOAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

ALLOWED_CYCLE_RESULT_FIELDS=frozenset({
    "schema_version",
    "summary",
    "next_mode",
    "goal_updates",
    "new_goals",
    "world_model_patch",
    "capability_frontier_patch",
    "working_memory",
    "unresolved_questions",
    "current_focus",
    "telemetry",
})
IMMUTABLE_COGNITIVE_FIELDS=frozenset({
    "forge_id",
    "genesis_event_id",
    "owner_activation_ref",
    "life_goal",
    "constitutional_invariants",
    "root_owner_continuity",
    "strategic_drives",
    "provenance_boundary",
})


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
    strategic_drives: Mapping[str, Any]
    specialist_roster: Mapping[str, Any]
    world_model: Mapping[str, Any]
    capability_frontier: Mapping[str, Any]
    world_model_digest: str
    capability_frontier_digest: str
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


def reasoning_policy_for_tier(tier: ReasoningTier) -> dict[str, Any]:
    policies={
        ReasoningTier.REFLEX:{
            "model_call":False,
            "effort_hint":"none",
            "latency_class":"local",
            "context_scope":"minimal",
        },
        ReasoningTier.FAST:{
            "model_call":True,
            "effort_hint":"low",
            "latency_class":"interactive",
            "context_scope":"focused",
        },
        ReasoningTier.STANDARD:{
            "model_call":True,
            "effort_hint":"medium",
            "latency_class":"normal",
            "context_scope":"working_set",
        },
        ReasoningTier.DEEP:{
            "model_call":True,
            "effort_hint":"high",
            "latency_class":"deliberate",
            "context_scope":"expanded_on_demand",
        },
    }
    return dict(policies[tier])


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
        "strategic_drives": _json(dict(state.strategic_drives)),
        "current_focus": state.current_focus,
        "selected_action": action.value,
        "selected_goal": goal.to_dict() if goal else None,
        "reasoning_tier": tier.value,
        "reasoning_policy": reasoning_policy_for_tier(tier),
        "selection_reason": reason,
        "world_model_digest": state.world_model_digest,
        "capability_frontier_digest": state.capability_frontier_digest,
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


SELF_SETTABLE_MODES = frozenset({
    ForgeMode.RUNNING,
    ForgeMode.WAITING_FOR_AUTHORITY,
    ForgeMode.WAITING_FOR_REQUIRED_INPUT,
    ForgeMode.WAITING_FOR_RESOURCE,
    ForgeMode.WAITING_FOR_DEPENDENCY,
})


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ForgeCognitionError(name + "_INVALID")
    return _json(dict(value))


def _mind_state_from_row(row: Mapping[str, Any]) -> ForgeMindState:
    state = row.get("state_json")
    if not isinstance(state, Mapping):
        raise ForgeCognitionError("STATE_JSON_INVALID")
    goals_raw = state.get("goals") or []
    if not isinstance(goals_raw, list):
        raise ForgeCognitionError("STATE_GOALS_INVALID")
    updated = row.get("updated_at")
    if not isinstance(updated, datetime):
        raise ForgeCognitionError("STATE_UPDATED_AT_INVALID")
    return ForgeMindState(
        forge_id=_text(row.get("forge_id"), "FORGE_ID", maximum=100),
        mode=ForgeMode(str(row.get("mode"))),
        genesis_event_id=_text(row.get("genesis_event_id"), "GENESIS_EVENT_ID", maximum=100),
        life_goal=_text(state.get("life_goal"), "LIFE_GOAL"),
        constitutional_invariants=_list(state.get("constitutional_invariants"), "CONSTITUTIONAL_INVARIANTS"),
        long_horizon_objectives=_list(state.get("long_horizon_objectives"), "LONG_HORIZON_OBJECTIVES"),
        human_specialist_doctrine=_mapping(state.get("human_specialist_doctrine"), "HUMAN_SPECIALIST_DOCTRINE"),
        root_owner_continuity=_mapping(state.get("root_owner_continuity"), "ROOT_OWNER_CONTINUITY"),
        strategic_drives=_mapping(state.get("strategic_drives"), "STRATEGIC_DRIVES"),
        specialist_roster=_mapping(state.get("specialist_roster"), "SPECIALIST_ROSTER"),
        world_model=_mapping(state.get("world_model"), "WORLD_MODEL"),
        capability_frontier=_mapping(state.get("capability_frontier"), "CAPABILITY_FRONTIER"),
        world_model_digest=str(state.get("world_model_digest") or digest(_mapping(state.get("world_model"), "WORLD_MODEL"))),
        capability_frontier_digest=str(state.get("capability_frontier_digest") or digest(_mapping(state.get("capability_frontier"), "CAPABILITY_FRONTIER"))),
        working_memory=_mapping(state.get("working_memory"), "WORKING_MEMORY"),
        goals=tuple(ForgeGoal.parse(x) for x in goals_raw),
        unresolved_questions=_list(state.get("unresolved_questions"), "UNRESOLVED_QUESTIONS"),
        current_focus=(str(state.get("current_focus")).strip() if state.get("current_focus") is not None else None),
        last_cycle_summary=(str(state.get("last_cycle_summary")).strip() if state.get("last_cycle_summary") is not None else None),
        cycle_number=int(row.get("cycle_number") or 0),
        fencing_token=int(row.get("fencing_token") or 0),
        state_version=int(row.get("state_version") or 0),
        updated_at=updated,
    )


def _validate_goal_graph(goals: list[ForgeGoal]) -> None:
    by_id={g.goal_id:g for g in goals}
    if len(by_id) != len(goals):
        raise ForgeCognitionError("DUPLICATE_GOAL_ID")
    for goal in goals:
        for dep in goal.dependencies:
            if dep not in by_id:
                raise ForgeCognitionError("GOAL_DEPENDENCY_UNKNOWN:" + dep)
    visiting:set[str]=set()
    visited:set[str]=set()
    def visit(gid: str) -> None:
        if gid in visited:
            return
        if gid in visiting:
            raise ForgeCognitionError("GOAL_DEPENDENCY_CYCLE")
        visiting.add(gid)
        for dep in by_id[gid].dependencies:
            visit(dep)
        visiting.remove(gid)
        visited.add(gid)
    for gid in by_id:
        visit(gid)


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
                CREATE TABLE IF NOT EXISTS forge_cognition_inbox (
                    signal_id BIGSERIAL PRIMARY KEY,
                    forge_id TEXT NOT NULL REFERENCES forge_mind_state(forge_id) ON DELETE CASCADE,
                    dedupe_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    consumed_at TIMESTAMPTZ,
                    UNIQUE(forge_id, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS forge_cognition_inbox_pending_idx
                  ON forge_cognition_inbox(forge_id, status, priority, signal_id);
                """)

    def probe(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                    SELECT to_regclass('public.forge_mind_state')::text AS state_table,
                           to_regclass('public.forge_mind_events')::text AS event_table,
                           to_regclass('public.forge_cognition_inbox')::text AS inbox_table
                    """)
                    row = dict(cur.fetchone() or {})
                    ready = bool(row.get("state_table") and row.get("event_table") and row.get("inbox_table"))
                    return {"schema_version": SCHEMA_VERSION, "status": "PASS" if ready else "NOT_READY", **row}
        except Exception as exc:
            return {"schema_version": SCHEMA_VERSION, "status": "FAILED_CLOSED", "reason": type(exc).__name__}

    def seed_pre_genesis(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        """Store the reviewed birth package without activating Forge."""
        required = {
            "forge_id","genesis_event_id","owner_activation_ref","life_goal","constitutional_invariants",
            "long_horizon_objectives","human_specialist_doctrine","root_owner_continuity",
            "strategic_drives","specialist_roster","world_model","capability_frontier","goals"
        }
        missing = sorted(required - set(packet))
        if missing:
            raise ForgeCognitionError("GENESIS_PACKET_MISSING:" + ",".join(missing))
        genesis_event_id=_text(packet.get("genesis_event_id"),"GENESIS_EVENT_ID",maximum=100)
        if not re.fullmatch(r"GENESIS_EVENT_[0-9]{4,}",genesis_event_id):
            raise ForgeCognitionError("GENESIS_EVENT_ID_INVALID")
        root = packet.get("root_owner_continuity")
        drives = packet.get("strategic_drives")
        human = packet.get("human_specialist_doctrine")
        if not isinstance(root, Mapping):
            raise ForgeCognitionError("ROOT_OWNER_CONTINUITY_INVALID")
        try:
            RootOwnerBoundary.parse(root)
        except Exception as exc:
            raise ForgeCognitionError("ROOT_OWNER_BOUNDARY_INVALID:" + str(exc)) from exc
        if not isinstance(drives, Mapping):
            raise ForgeCognitionError("STRATEGIC_DRIVES_INVALID")
        try:
            StrategicDriveConfig.parse(drives)
        except Exception as exc:
            raise ForgeCognitionError("STRATEGIC_DRIVES_INVALID:" + str(exc)) from exc
        if not isinstance(human, Mapping) or str(human.get("role") or "").upper() != "HUMAN_SPECIALIST":
            raise ForgeCognitionError("HUMAN_SPECIALIST_REQUIRED")
        roster=packet.get("specialist_roster")
        if not isinstance(roster,Mapping) or not isinstance(roster.get("SOL"),Mapping):
            raise ForgeCognitionError("SOL_PRIMARY_REQUIRED")
        goals_raw = packet.get("goals")
        if not isinstance(goals_raw, list):
            raise ForgeCognitionError("GOALS_INVALID")
        goals = [ForgeGoal.parse(x).to_dict() for x in goals_raw]
        state = {
            "forge_id": _text(packet.get("forge_id"), "FORGE_ID", maximum=100),
            "genesis_event_id": genesis_event_id,
            "owner_activation_ref": _text(packet.get("owner_activation_ref"), "OWNER_ACTIVATION_REF", maximum=500),
            "life_goal": _text(packet.get("life_goal"), "LIFE_GOAL"),
            "constitutional_invariants": list(_list(packet.get("constitutional_invariants"), "CONSTITUTIONAL_INVARIANTS")),
            "long_horizon_objectives": list(_list(packet.get("long_horizon_objectives"), "LONG_HORIZON_OBJECTIVES")),
            "human_specialist_doctrine": _json(dict(human)),
            "root_owner_continuity": _json(dict(root)),
            "strategic_drives": _json(dict(drives)),
            "specialist_roster": _json(dict(packet.get("specialist_roster") or {})),
            "world_model": _json(dict(packet.get("world_model") or {})),
            "capability_frontier": _json(dict(packet.get("capability_frontier") or {})),
            "world_model_digest": digest(dict(packet.get("world_model") or {})),
            "capability_frontier_digest": digest(dict(packet.get("capability_frontier") or {})),
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

    def load_state(self, forge_id: str) -> ForgeMindState:
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM forge_mind_state WHERE forge_id=%s",(forge_id,))
                row=cur.fetchone()
                if row is None:
                    raise ForgeCognitionError("FORGE_STATE_NOT_FOUND")
                return _mind_state_from_row(row)

    def prepare_cycle(self, forge_id: str, worker_id: str, *, lease_seconds: int = 180, event_limit: int = 24) -> dict[str, Any]:
        """Atomically claim the cycle and assemble its compact working packet."""
        worker=_text(worker_id,"WORKER_ID",maximum=200)
        if isinstance(lease_seconds,bool) or not isinstance(lease_seconds,int) or not 30 <= lease_seconds <= 1800:
            raise ForgeCognitionError("LEASE_SECONDS_INVALID")
        if isinstance(event_limit,bool) or not isinstance(event_limit,int) or not 1 <= event_limit <= 100:
            raise ForgeCognitionError("EVENT_LIMIT_INVALID")
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
                    RETURNING *
                """,(token,worker,expires,forge_id))
                claimed=cur.fetchone()
                state=_mind_state_from_row(claimed)

                cur.execute("""
                    SELECT event_id,event_type,state_version,cycle_number,payload,created_at
                      FROM forge_mind_events WHERE forge_id=%s
                     ORDER BY event_id DESC LIMIT %s
                """,(forge_id,event_limit))
                events=[]
                for event in reversed(cur.fetchall()):
                    item=dict(event); item["created_at"]=item["created_at"].isoformat(); events.append(item)

                cur.execute("""
                    SELECT signal_id,source,event_type,priority,payload,created_at
                      FROM forge_cognition_inbox
                     WHERE forge_id=%s AND status='PENDING'
                     ORDER BY priority ASC, signal_id ASC
                     LIMIT 16
                """,(forge_id,))
                signals=[]
                for signal in cur.fetchall():
                    item=dict(signal); item["created_at"]=item["created_at"].isoformat(); signals.append(item)

                packet=build_delta_context(state,recent_events=events,max_events=event_limit)
                packet["pending_signals"]=signals
                packet["signal_ids"]=[int(s["signal_id"]) for s in signals]
                packet["wake_reason"]="SIGNAL" if signals else "GOAL_OR_REFLECTION"
                if signals and min(int(s["priority"]) for s in signals) <= 10:
                    packet["selected_action"]=CycleAction.REFLECT_AND_PLAN.value
                    packet["selected_goal"]=None
                    packet["parallel_goal_batch"]=[]
                    packet["reasoning_tier"]=ReasoningTier.STANDARD.value
                    packet["reasoning_policy"]=reasoning_policy_for_tier(ReasoningTier.STANDARD)
                    packet["selection_reason"]="URGENT_SIGNAL_PREEMPTS_CURRENT_GOAL"
                    packet["next_cycle_delay_seconds"]=0
                packet["lease_owner"]=worker
                packet["lease_expires_at"]=expires.isoformat()
                packet["model_call_policy"]=(
                    "LOCAL_FIRST"
                    if packet["reasoning_tier"] == ReasoningTier.REFLEX.value
                    and packet["selected_action"] == CycleAction.EXECUTE_NEXT.value
                    else "REQUIRED"
                )
                return packet

    def release_cycle(self, forge_id: str, *, worker_id: str, fencing_token: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("forge-mind:"+forge_id,))
                cur.execute("""
                    UPDATE forge_mind_state
                       SET lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                     WHERE forge_id=%s AND lease_owner=%s AND fencing_token=%s
                """,(forge_id,worker_id,fencing_token))
                if cur.rowcount != 1:
                    raise ForgeCognitionError("STALE_OR_FOREIGN_CYCLE_LEASE")

    def commit_cycle_result(
        self,
        forge_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        expected_state_version: int,
        result: Mapping[str, Any],
        consumed_signal_ids: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        if not isinstance(result,Mapping):
            raise ForgeCognitionError("CYCLE_RESULT_INVALID")
        validate_cycle_result_shape(result)
        if str(result.get("schema_version") or RESULT_VERSION) != RESULT_VERSION:
            raise ForgeCognitionError("CYCLE_RESULT_VERSION_INVALID")
        summary=_text(result.get("summary"),"CYCLE_SUMMARY",maximum=8000)
        next_mode_raw=str(result.get("next_mode") or ForgeMode.RUNNING.value)
        try:
            next_mode=ForgeMode(next_mode_raw)
        except ValueError as exc:
            raise ForgeCognitionError("NEXT_MODE_INVALID") from exc
        if next_mode not in SELF_SETTABLE_MODES:
            raise ForgeCognitionError("NEXT_MODE_NOT_SELF_SETTABLE")

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("forge-mind:"+forge_id,))
                cur.execute("SELECT * FROM forge_mind_state WHERE forge_id=%s FOR UPDATE",(forge_id,))
                row=cur.fetchone()
                if row is None:
                    raise ForgeCognitionError("FORGE_STATE_NOT_FOUND")
                if row["lease_owner"] != worker_id or int(row["fencing_token"]) != fencing_token:
                    raise ForgeCognitionError("STALE_OR_FOREIGN_CYCLE_LEASE")
                if int(row["state_version"]) != expected_state_version:
                    raise ForgeCognitionError("STATE_VERSION_CONFLICT")
                if ForgeMode(str(row["mode"])) is not ForgeMode.RUNNING:
                    raise ForgeCognitionError("FORGE_NOT_RUNNING")

                state=dict(row["state_json"] or {})
                goals=[ForgeGoal.parse(x) for x in (state.get("goals") or [])]
                by_id={g.goal_id:g for g in goals}

                updates=result.get("goal_updates") or []
                if not isinstance(updates,list) or len(updates)>MAX_ITEMS:
                    raise ForgeCognitionError("GOAL_UPDATES_INVALID")
                for update in updates:
                    if not isinstance(update,Mapping):
                        raise ForgeCognitionError("GOAL_UPDATE_INVALID")
                    gid=_text(update.get("goal_id"),"GOAL_ID",maximum=200)
                    old=by_id.get(gid)
                    if old is None:
                        raise ForgeCognitionError("GOAL_UPDATE_UNKNOWN:"+gid)
                    status=old.status
                    if "status" in update:
                        try:
                            status=GoalStatus(str(update["status"]))
                        except ValueError as exc:
                            raise ForgeCognitionError("GOAL_STATUS_INVALID") from exc
                    by_id[gid]=ForgeGoal.parse({
                        "goal_id":old.goal_id,
                        "objective":old.objective,
                        "priority":update.get("priority",old.priority),
                        "status":status.value,
                        "dependencies":list(old.dependencies),
                        "complexity":update.get("complexity",old.complexity),
                        "uncertainty":update.get("uncertainty",old.uncertainty),
                        "parallel_safe":old.parallel_safe,
                    })

                new_goals=result.get("new_goals") or []
                if not isinstance(new_goals,list) or len(new_goals)>MAX_ITEMS:
                    raise ForgeCognitionError("NEW_GOALS_INVALID")
                for raw in new_goals:
                    if not isinstance(raw,Mapping):
                        raise ForgeCognitionError("NEW_GOAL_INVALID")
                    goal=ForgeGoal.parse(raw)
                    if goal.goal_id in by_id:
                        raise ForgeCognitionError("NEW_GOAL_DUPLICATE:"+goal.goal_id)
                    by_id[goal.goal_id]=goal

                final_goals=list(by_id.values())
                _validate_goal_graph(final_goals)

                if next_mode is ForgeMode.RUNNING and not any(
                    g.status is GoalStatus.ACTIVE for g in final_goals
                ):
                    # One strategic reflection is enough. If it yields no next
                    # goal, sleep until a new event wakes Forge instead of
                    # burning model calls in an empty loop.
                    next_mode=ForgeMode.WAITING_FOR_DEPENDENCY

                for field,name in (("world_model_patch","WORLD_MODEL_PATCH"),("capability_frontier_patch","CAPABILITY_FRONTIER_PATCH")):
                    patch=result.get(field) or {}
                    if not isinstance(patch,Mapping):
                        raise ForgeCognitionError(name+"_INVALID")
                    target="world_model" if field=="world_model_patch" else "capability_frontier"
                    digest_field="world_model_digest" if target=="world_model" else "capability_frontier_digest"
                    if patch:
                        current=dict(state.get(target) or {})
                        current.update(_json(dict(patch)))
                        state[target]=_json(current)
                        state[digest_field]=digest(state[target])

                if "working_memory" in result:
                    state["working_memory"]=_mapping(result.get("working_memory"),"WORKING_MEMORY")
                if "unresolved_questions" in result:
                    state["unresolved_questions"]=list(_list(result.get("unresolved_questions"),"UNRESOLVED_QUESTIONS"))
                if "current_focus" in result:
                    focus=result.get("current_focus")
                    state["current_focus"]=_text(focus,"CURRENT_FOCUS",required=False,maximum=2000) or None

                state["goals"]=[g.to_dict() for g in final_goals]
                state["last_cycle_summary"]=summary
                new_version=int(row["state_version"])+1
                new_cycle=int(row["cycle_number"])+1
                state=_json(state)
                if consumed_signal_ids:
                    ids=[]
                    for raw_id in consumed_signal_ids:
                        if isinstance(raw_id,bool) or not isinstance(raw_id,int) or raw_id < 1:
                            raise ForgeCognitionError("CONSUMED_SIGNAL_ID_INVALID")
                        ids.append(raw_id)
                    cur.execute(
                        """
                        UPDATE forge_cognition_inbox
                           SET status='CONSUMED',consumed_at=now()
                         WHERE forge_id=%s AND status='PENDING' AND signal_id = ANY(%s)
                        """,
                        (forge_id,ids),
                    )

                cur.execute("""
                    UPDATE forge_mind_state
                       SET mode=%s,state_json=%s::jsonb,state_version=%s,cycle_number=%s,
                           lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                     WHERE forge_id=%s
                    RETURNING mode,state_version,cycle_number,fencing_token,updated_at
                """,(next_mode.value,json.dumps(state),new_version,new_cycle,forge_id))
                updated=dict(cur.fetchone())
                telemetry=result.get("telemetry") or {}
                if not isinstance(telemetry,Mapping):
                    raise ForgeCognitionError("CYCLE_TELEMETRY_INVALID")
                event_payload={
                    "schema_version":RESULT_VERSION,
                    "summary":summary,
                    "next_mode":next_mode.value,
                    "state_digest":digest(state),
                    "goal_count":len(final_goals),
                    "worker_id":worker_id,
                    "fencing_token":fencing_token,
                    "telemetry":_json(dict(telemetry)),
                }
                cur.execute("""
                    INSERT INTO forge_mind_events(forge_id,state_version,cycle_number,event_type,payload)
                    VALUES (%s,%s,%s,'FORGE_COGNITION_CYCLE_COMMITTED',%s::jsonb)
                    RETURNING event_id
                """,(forge_id,new_version,new_cycle,json.dumps(event_payload)))
                updated["event_id"]=int(cur.fetchone()["event_id"])
                updated["updated_at"]=updated["updated_at"].isoformat()
                return updated

    def enqueue_signal(
        self,
        forge_id: str,
        *,
        dedupe_key: str,
        source: str,
        event_type: str,
        payload: Mapping[str, Any],
        priority: int = 50,
        wake: bool = True,
    ) -> dict[str, Any]:
        key=_text(dedupe_key,"DEDUPE_KEY",maximum=300)
        src=_text(source,"SIGNAL_SOURCE",maximum=200)
        kind=_text(event_type,"SIGNAL_TYPE",maximum=200)
        if isinstance(priority,bool) or not isinstance(priority,int) or not 1 <= priority <= 1000:
            raise ForgeCognitionError("SIGNAL_PRIORITY_INVALID")
        if type(wake) is not bool:
            raise ForgeCognitionError("SIGNAL_WAKE_INVALID")
        body=_mapping(payload,"SIGNAL_PAYLOAD")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT mode FROM forge_mind_state WHERE forge_id=%s FOR UPDATE",(forge_id,))
                row=cur.fetchone()
                if row is None:
                    raise ForgeCognitionError("FORGE_STATE_NOT_FOUND")
                cur.execute(
                    """
                    INSERT INTO forge_cognition_inbox(forge_id,dedupe_key,source,event_type,priority,payload)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (forge_id,dedupe_key) DO NOTHING
                    RETURNING signal_id,status,priority,created_at
                    """,
                    (forge_id,key,src,kind,priority,json.dumps(body)),
                )
                inserted=cur.fetchone()
                if inserted is None:
                    cur.execute(
                        """
                        SELECT signal_id,status,priority,created_at
                          FROM forge_cognition_inbox
                         WHERE forge_id=%s AND dedupe_key=%s
                        """,
                        (forge_id,key),
                    )
                    existing=cur.fetchone()
                    if existing is None:
                        raise ForgeCognitionError("SIGNAL_DEDUPE_LOOKUP_FAILED")
                    signal=dict(existing)
                    signal["deduplicated"]=True
                    signal["woke_forge"]=False
                    signal["created_at"]=signal["created_at"].isoformat()
                    return signal
                signal=dict(inserted)
                signal["deduplicated"]=False
                current=ForgeMode(str(row["mode"]))
                can_wake=current is ForgeMode.WAITING_FOR_DEPENDENCY or (
                    current is ForgeMode.WAITING_FOR_REQUIRED_INPUT and src.upper() in {"OWNER","HUMAN_SPECIALIST","ROOT_OWNER"}
                )
                if wake and can_wake:
                    cur.execute(
                        "UPDATE forge_mind_state SET mode=%s,updated_at=now() WHERE forge_id=%s",
                        (ForgeMode.RUNNING.value,forge_id),
                    )
                    signal["woke_forge"]=True
                else:
                    signal["woke_forge"]=False
                cur.execute(
                    "SELECT pg_notify('forge_cognition_wake', %s)",
                    (forge_id + ":" + str(signal["signal_id"]),),
                )
                signal["created_at"]=signal["created_at"].isoformat()
                return signal

    def wait_for_wake(self, forge_id: str, *, timeout_seconds: float = 300.0) -> Optional[dict[str, Any]]:
        """Wait cheaply for a committed cognition signal without polling.

        PostgreSQL LISTEN/NOTIFY is best-effort wake transport only; canonical
        signals remain durable in forge_cognition_inbox and are re-read before
        every cognition cycle.
        """
        if isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float)) or not 0.0 <= float(timeout_seconds) <= 3600.0:
            raise ForgeCognitionError("WAKE_TIMEOUT_INVALID")
        conn=self._connect()
        try:
            conn.set_session(autocommit=True)
            with conn.cursor() as cur:
                cur.execute("LISTEN forge_cognition_wake")
            if not select_module.select([conn],[],[],float(timeout_seconds))[0]:
                return None
            conn.poll()
            while conn.notifies:
                notice=conn.notifies.pop(0)
                payload=str(notice.payload or "")
                if payload.startswith(forge_id + ":"):
                    parts=payload.rsplit(":",1)
                    try:
                        signal_id=int(parts[1])
                    except (IndexError,ValueError):
                        signal_id=None
                    return {"forge_id":forge_id,"signal_id":signal_id,"channel":"forge_cognition_wake"}
            return None
        finally:
            conn.close()

    def pending_signals(self, forge_id: str, *, limit: int = 16) -> list[dict[str, Any]]:
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 64:
            raise ForgeCognitionError("SIGNAL_LIMIT_INVALID")
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT signal_id,source,event_type,priority,payload,created_at
                      FROM forge_cognition_inbox
                     WHERE forge_id=%s AND status='PENDING'
                     ORDER BY priority ASC, signal_id ASC
                     LIMIT %s
                    """,
                    (forge_id,limit),
                )
                out=[]
                for row in cur.fetchall():
                    item=dict(row); item["created_at"]=item["created_at"].isoformat(); out.append(item)
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


def validate_cycle_result_shape(result: Mapping[str,Any]) -> None:
    extras=set(result)-ALLOWED_CYCLE_RESULT_FIELDS
    if extras:
        if extras & IMMUTABLE_COGNITIVE_FIELDS:
            raise ForgeCognitionError(
                "IMMUTABLE_COGNITIVE_FIELD_MUTATION_FORBIDDEN:"
                + ",".join(sorted(extras & IMMUTABLE_COGNITIVE_FIELDS))
            )
        raise ForgeCognitionError("CYCLE_RESULT_FIELDS_INVALID:"+",".join(sorted(extras)))


def safe_error(exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAILED_CLOSED",
        "error": str(exc) if isinstance(exc, ForgeCognitionError) else type(exc).__name__,
    }
