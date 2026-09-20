from __future__ import annotations
import json
import statistics
import time

from forge_strategic_drives import CapabilityGap, ValueOpportunity
from forge_strategy_engine import synthesize_strategic_goals

ITERATIONS=1000

def make_inputs():
    gaps=[
        CapabilityGap.parse({
            "capability":f"cap-{i}",
            "current_score":20+(i%30),
            "target_score":70+(i%20),
            "evidence_confidence":0.55+(i%40)/100.0,
            "blocking_dependencies":[],
        })
        for i in range(64)
    ]
    opps=[
        ValueOpportunity.parse({
            "opportunity_id":f"opp-{i}",
            "mechanism":f"lawful product {i}",
            "expected_value_score":60+(i%40),
            "capability_synergy":55+(i%45),
            "capital_efficiency":60+(i%40),
            "time_to_value_score":50+(i%50),
            "evidence_confidence":0.55+(i%40)/100.0,
            "downside_risk":10+(i%30),
            "ongoing_burden":10+(i%25),
            "lawful":True,"sustainable":True,"deceptive":False,"unauthorized_access":False,
            "regulated_or_licensed_activity":False,"required_scopes":["forge:product"],
            "requires_external_spend":False,"requires_new_legal_entity_or_account":False,
            "known_obligations_covered":True,"funds_or_resources_available":True,
        })
        for i in range(64)
    ]
    return gaps,opps

def main():
    gaps,opps=make_inputs()
    elapsed=[]
    result=None
    for _ in range(ITERATIONS):
        t0=time.perf_counter()
        result=synthesize_strategic_goals(gaps,opps,max_goals=8)
        elapsed.append((time.perf_counter()-t0)*1000.0)
    payload={
        "schema_version":"FORGE_STRATEGY_BENCH_V1",
        "iterations":ITERATIONS,
        "avg_ms":round(statistics.mean(elapsed),4),
        "p95_ms":round(sorted(elapsed)[int(len(elapsed)*0.95)-1],4),
        "goal_count":len(result),
        "drives":sorted({g.drive for g in result}),
        "model_calls":0,
    }
    print(json.dumps(payload,sort_keys=True))
    if set(payload["drives"])!={"GENERAL_CAPABILITY_GROWTH","LAWFUL_SUSTAINABLE_VALUE_GROWTH"}:
        raise SystemExit("drive starvation")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
