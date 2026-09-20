from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone

from forge_cognition import (
    ForgeGoal,
    ForgeMindState,
    ForgeMode,
    build_delta_context,
    digest,
)

ITERATIONS=500

def make_state():
    world={f"world_{i}":{"value":"x"*256,"n":i} for i in range(180)}
    capability={f"cap_{i}":{"state":"READY","score":i} for i in range(180)}
    goals=tuple(
        ForgeGoal(
            goal_id=f"g{i}",
            objective=f"Goal {i}",
            priority=i+1,
            complexity=10 if i < 8 else 45,
            uncertainty=5 if i < 8 else 45,
            parallel_safe=i < 3,
        )
        for i in range(128)
    )
    return ForgeMindState(
        forge_id="FORGE",
        mode=ForgeMode.RUNNING,
        genesis_event_id="GENESIS_EVENT_0001",
        life_goal="Compound verified capability.",
        constitutional_invariants=("Preserve ROOT_OWNER override.",),
        long_horizon_objectives=("Improve Forge, Sol, and Human Specialist.",),
        human_specialist_doctrine={"role":"HUMAN_SPECIALIST"},
        root_owner_continuity={"override_authority":"ABSOLUTE","physical_continuity_required":True},
        specialist_roster={"SOL":{"role":"PRIMARY_ENGINEERING"}},
        world_model=world,
        capability_frontier=capability,
        world_model_digest=digest(world),
        capability_frontier_digest=digest(capability),
        working_memory={"focus":"benchmark","facts":["a","b","c"]},
        goals=goals,
        unresolved_questions=("benchmark?",),
        current_focus="performance",
        last_cycle_summary=None,
        cycle_number=100,
        fencing_token=9,
        state_version=22,
        updated_at=datetime.now(timezone.utc),
    )

def main() -> int:
    state=make_state()
    events=[{"event_id":i,"event_type":"BENCH","payload":{"n":i}} for i in range(24)]

    cached=[]
    recompute=[]
    packet=None
    for _ in range(ITERATIONS):
        t0=time.perf_counter()
        packet=build_delta_context(state,recent_events=events,max_events=24)
        cached.append((time.perf_counter()-t0)*1000.0)

        t1=time.perf_counter()
        digest(dict(state.world_model))
        digest(dict(state.capability_frontier))
        recompute.append((time.perf_counter()-t1)*1000.0)

    packet_bytes=len(json.dumps(packet,separators=(",",":")).encode("utf-8"))
    result={
        "schema_version":"FORGE_COGNITION_BENCH_V1",
        "iterations":ITERATIONS,
        "packet_bytes":packet_bytes,
        "cached_packet_avg_ms":round(statistics.mean(cached),4),
        "cached_packet_p95_ms":round(sorted(cached)[int(len(cached)*0.95)-1],4),
        "avoided_digest_avg_ms":round(statistics.mean(recompute),4),
        "full_world_model_in_packet":"world_model" in packet,
        "parallel_goal_batch_size":len(packet["parallel_goal_batch"]),
        "reasoning_tier":packet["reasoning_tier"],
    }
    print(json.dumps(result,sort_keys=True))
    if packet_bytes > 65536:
        raise SystemExit("packet too large")
    if result["full_world_model_in_packet"]:
        raise SystemExit("full world model leaked into working packet")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
