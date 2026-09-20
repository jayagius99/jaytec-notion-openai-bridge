"""Deterministic strategic-drive controller for Forge.

This controller converts capability gaps and lawful value opportunities into
bounded goal proposals. It does not execute external effects and it cannot
grant itself authority. The purpose is to keep both permanent drives active
without requiring a model call just to decide what category of work comes next.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from forge_strategic_drives import (
    CapabilityGap,
    OpportunityDecision,
    ValueOpportunity,
    evaluate_value_opportunity,
    rank_capability_gaps,
    rank_value_opportunities,
    value_opportunity_score,
)


def _slug(value: str) -> str:
    out=re.sub(r"[^a-z0-9]+","-",value.lower()).strip("-")
    return out[:80] or "unnamed"


@dataclass(frozen=True)
class StrategicGoalProposal:
    goal_id: str
    drive: str
    objective: str
    priority: int
    complexity: int
    uncertainty: int
    parallel_safe: bool
    authority_state: str
    evidence_basis: str

    def to_forge_goal(self) -> dict:
        return {
            "goal_id":self.goal_id,
            "objective":self.objective,
            "priority":self.priority,
            "status":"ACTIVE",
            "dependencies":[],
            "complexity":self.complexity,
            "uncertainty":self.uncertainty,
            "parallel_safe":self.parallel_safe,
        }


def capability_goal(gap: CapabilityGap, *, ordinal: int) -> StrategicGoalProposal:
    uncertainty=max(0,min(100,round((1.0-gap.evidence_confidence)*100)))
    priority=max(1,min(999,20+ordinal))
    return StrategicGoalProposal(
        goal_id="drive-capability-"+_slug(gap.capability),
        drive="GENERAL_CAPABILITY_GROWTH",
        objective=(
            f"Improve {gap.capability} from measured score {gap.current_score} toward "
            f"{gap.target_score}; establish evidence, sandbox candidate changes, benchmark, "
            "retain only verified gains, and preserve rollback."
        ),
        priority=priority,
        complexity=max(35,min(90,40+gap.gap//2)),
        uncertainty=uncertainty,
        parallel_safe=not bool(gap.blocking_dependencies),
        authority_state="CURRENT_BOUNDED_AUTHORITY",
        evidence_basis=f"gap={gap.gap};confidence={gap.evidence_confidence:.3f}",
    )


def value_goal(o: ValueOpportunity, *, ordinal: int) -> StrategicGoalProposal:
    decision=evaluate_value_opportunity(o)
    score=value_opportunity_score(o)
    if decision is OpportunityDecision.RESEARCH:
        verb="Research and validate"
        authority="RESEARCH_ONLY"
    elif decision is OpportunityDecision.OWNER_REVIEW:
        verb="Prepare an owner-review execution packet for"
        authority="OWNER_REVIEW_REQUIRED"
    else:
        verb="Pursue"
        authority="CURRENT_BOUNDED_AUTHORITY"
    return StrategicGoalProposal(
        goal_id="drive-value-"+_slug(o.opportunity_id),
        drive="LAWFUL_SUSTAINABLE_VALUE_GROWTH",
        objective=(
            f"{verb} lawful sustainable opportunity '{o.mechanism}'. Verify real value creation, "
            "measure downside/ongoing burden, and route verified surplus/resources toward the "
            "highest-evidence capability multipliers."
        ),
        priority=max(1,min(999,40+ordinal)),
        complexity=max(30,min(90,45+o.downside_risk//3)),
        uncertainty=max(0,min(100,round((1.0-o.evidence_confidence)*100))),
        parallel_safe=decision is OpportunityDecision.RESEARCH,
        authority_state=authority,
        evidence_basis=f"value_score={score:.4f};decision={decision.value}",
    )


def synthesize_strategic_goals(
    capability_gaps: Iterable[CapabilityGap],
    opportunities: Iterable[ValueOpportunity],
    *,
    max_goals: int=8,
) -> list[StrategicGoalProposal]:
    if isinstance(max_goals,bool) or not isinstance(max_goals,int) or not 2 <= max_goals <= 32:
        raise ValueError("MAX_GOALS_INVALID")
    gaps=rank_capability_gaps(list(capability_gaps))
    opps=rank_value_opportunities(list(opportunities))

    cap=[capability_goal(g,ordinal=i) for i,g in enumerate(gaps[:max_goals])]
    val=[value_goal(o,ordinal=i) for i,o in enumerate(opps[:max_goals])]

    out:list[StrategicGoalProposal]=[]
    # Fair interleave: neither permanent drive can starve the other when both
    # have viable work.
    while (cap or val) and len(out)<max_goals:
        if cap and len(out)<max_goals:
            out.append(cap.pop(0))
        if val and len(out)<max_goals:
            out.append(val.pop(0))
    return out


def should_run_metacognitive_review(
    *,
    cycles_since_review: int,
    consecutive_failures: int,
    uncertainty_drift: int,
    strategy_switches: int,
) -> bool:
    for name,v in (
        ("cycles_since_review",cycles_since_review),
        ("consecutive_failures",consecutive_failures),
        ("uncertainty_drift",uncertainty_drift),
        ("strategy_switches",strategy_switches),
    ):
        if isinstance(v,bool) or not isinstance(v,int) or v<0:
            raise ValueError(name.upper()+"_INVALID")
    return (
        cycles_since_review>=25
        or consecutive_failures>=2
        or uncertainty_drift>=20
        or strategy_switches>=5
    )
