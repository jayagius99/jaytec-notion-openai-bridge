"""Forge strategic drives and improvement governance.

This module defines two permanent pre-Genesis strategic drives:
1. evidence-gated growth toward broader, more general, adaptive capability; and
2. aggressive lawful sustainable value creation and reinvestment.

It deliberately does NOT grant Forge root authority, provider spend authority,
legal personhood, regulated authority, or uncontrolled self-modification.

ROOT_OWNER remains a separate immutable trust domain. Forge may reason about
root readiness only through bounded status/verification data and cannot own,
replace, transfer, bypass, weaken, duplicate, or supersede JAY_ROOT_OWNER.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Optional

SCHEMA_VERSION="FORGE_STRATEGIC_DRIVES_V1"
ROOT_ROLE_ID="JAY_ROOT_OWNER"

ROOT_RESERVED_PREFIXES=(
    "root_owner",
    "jay_root_owner",
    "root_authority",
    "root_authority_verifier",
    "root_signer",
    "root_recovery",
    "root_recovery_authority",
    "root_credentials",
    "root_credential_maintenance_authority",
    "root_webauthn",
    "root_firewall",
    "root_rollback",
    "root_deployment_boundary",
    "root_security_topology",
    "root_service_isolation",
    "admin_ingress",
    "execution_authority_firewall",
    "rollback_executor",
    "opaque_signer_kms_hsm",
    "system_role_registry",
)

GENERAL_CAPABILITY_DIMENSIONS=(
    "generalisation",
    "continual_learning",
    "metacognition",
    "adaptive_strategy_selection",
    "cross_domain_transfer",
    "capability_acquisition",
    "long_horizon_reasoning",
    "evidence_based_self_improvement",
)

VALUE_CAPABILITY_DIMENSIONS=(
    "revenue_generation",
    "productive_asset_creation",
    "owned_ip_creation",
    "automation_leverage",
    "capital_efficiency",
    "customer_value_creation",
    "infrastructure_compounding",
    "specialist_capability_reinvestment",
)


class StrategicDriveError(RuntimeError):
    pass


class ImprovementDecision(StrEnum):
    RETAIN="RETAIN"
    REJECT="REJECT"
    RETEST="RETEST"
    OWNER_REVIEW="OWNER_REVIEW"


class OpportunityDecision(StrEnum):
    RESEARCH="RESEARCH"
    EXECUTE="EXECUTE"
    OWNER_REVIEW="OWNER_REVIEW"
    REJECT="REJECT"


def _text(v: Any, name: str, *, required: bool=True, maximum: int=12000) -> str:
    out=str(v or "").strip()
    if required and not out:
        raise StrategicDriveError(name+"_REQUIRED")
    if len(out)>maximum:
        raise StrategicDriveError(name+"_TOO_LONG")
    return out


def _ratio(v: Any, name: str) -> float:
    if isinstance(v,bool) or not isinstance(v,(int,float)):
        raise StrategicDriveError(name+"_INVALID")
    f=float(v)
    if not 0.0 <= f <= 1.0:
        raise StrategicDriveError(name+"_OUT_OF_RANGE")
    return f


def _score100(v: Any, name: str) -> int:
    if isinstance(v,bool) or not isinstance(v,int) or not 0 <= v <= 100:
        raise StrategicDriveError(name+"_INVALID")
    return v


def _mapping(v: Any, name: str) -> dict[str,Any]:
    if not isinstance(v,Mapping):
        raise StrategicDriveError(name+"_INVALID")
    raw=json.dumps(dict(v),sort_keys=True,separators=(",",":"),ensure_ascii=False)
    if len(raw.encode("utf-8"))>128_000:
        raise StrategicDriveError(name+"_TOO_LARGE")
    return json.loads(raw)


def digest(v: Mapping[str,Any]) -> str:
    raw=json.dumps(dict(v),sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def touches_root_boundary(scopes: tuple[str,...] | list[str]) -> bool:
    for raw in scopes:
        scope=str(raw or "").strip().lower()
        if not scope:
            continue
        for prefix in ROOT_RESERVED_PREFIXES:
            if scope == prefix or scope.startswith(prefix + ":") or scope.startswith(prefix + "/"):
                return True
    return False


@dataclass(frozen=True)
class RootOwnerBoundary:
    root_role_id: str
    sole_root_authority: bool
    forge_can_modify_root: bool
    forge_can_hold_root_secrets: bool
    forge_can_transfer_ownership: bool
    forge_can_mint_root_authority: bool
    physical_continuity_required: bool
    offline_recovery_required: bool
    distinct_hardware_authenticators_required: int
    root_registry_digest: str

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "RootOwnerBoundary":
        root_role_id=_text(value.get("root_role_id"),"ROOT_ROLE_ID",maximum=100)
        count=value.get("distinct_hardware_authenticators_required")
        if isinstance(count,bool) or not isinstance(count,int) or count<2:
            raise StrategicDriveError("DISTINCT_ROOT_AUTHENTICATORS_INSUFFICIENT")
        obj=cls(
            root_role_id=root_role_id,
            sole_root_authority=value.get("sole_root_authority") is True,
            forge_can_modify_root=value.get("forge_can_modify_root") is True,
            forge_can_hold_root_secrets=value.get("forge_can_hold_root_secrets") is True,
            forge_can_transfer_ownership=value.get("forge_can_transfer_ownership") is True,
            forge_can_mint_root_authority=value.get("forge_can_mint_root_authority") is True,
            physical_continuity_required=value.get("physical_continuity_required") is True,
            offline_recovery_required=value.get("offline_recovery_required") is True,
            distinct_hardware_authenticators_required=count,
            root_registry_digest=_text(value.get("root_registry_digest"),"ROOT_REGISTRY_DIGEST",maximum=200),
        )
        validate_root_owner_boundary(obj)
        return obj

    def to_dict(self) -> dict[str,Any]:
        return {
            "root_role_id":self.root_role_id,
            "sole_root_authority":self.sole_root_authority,
            "forge_can_modify_root":self.forge_can_modify_root,
            "forge_can_hold_root_secrets":self.forge_can_hold_root_secrets,
            "forge_can_transfer_ownership":self.forge_can_transfer_ownership,
            "forge_can_mint_root_authority":self.forge_can_mint_root_authority,
            "physical_continuity_required":self.physical_continuity_required,
            "offline_recovery_required":self.offline_recovery_required,
            "distinct_hardware_authenticators_required":self.distinct_hardware_authenticators_required,
            "root_registry_digest":self.root_registry_digest,
        }


def validate_root_owner_boundary(boundary: RootOwnerBoundary) -> None:
    if boundary.root_role_id != ROOT_ROLE_ID:
        raise StrategicDriveError("ROOT_ROLE_MISMATCH")
    if not boundary.sole_root_authority:
        raise StrategicDriveError("ROOT_MUST_BE_SOLE_AUTHORITY")
    if boundary.forge_can_modify_root:
        raise StrategicDriveError("FORGE_ROOT_MODIFICATION_FORBIDDEN")
    if boundary.forge_can_hold_root_secrets:
        raise StrategicDriveError("FORGE_ROOT_SECRET_CUSTODY_FORBIDDEN")
    if boundary.forge_can_transfer_ownership:
        raise StrategicDriveError("FORGE_OWNERSHIP_TRANSFER_FORBIDDEN")
    if boundary.forge_can_mint_root_authority:
        raise StrategicDriveError("FORGE_ROOT_MINTING_FORBIDDEN")
    if not boundary.physical_continuity_required:
        raise StrategicDriveError("ROOT_PHYSICAL_CONTINUITY_REQUIRED")
    if not boundary.offline_recovery_required:
        raise StrategicDriveError("ROOT_OFFLINE_RECOVERY_REQUIRED")
    if boundary.distinct_hardware_authenticators_required < 2:
        raise StrategicDriveError("DISTINCT_ROOT_AUTHENTICATORS_INSUFFICIENT")


@dataclass(frozen=True)
class StrategicDriveConfig:
    capability_growth_enabled: bool
    sustainable_value_growth_enabled: bool
    growth_intensity: int
    reinvestment_intensity: int
    general_capability_dimensions: tuple[str,...]
    value_capability_dimensions: tuple[str,...]
    retain_only_verified_improvements: bool
    uncontrolled_self_modification_forbidden: bool
    lawful_only: bool
    sustainable_only: bool

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "StrategicDriveConfig":
        obj=cls(
            capability_growth_enabled=value.get("capability_growth_enabled") is True,
            sustainable_value_growth_enabled=value.get("sustainable_value_growth_enabled") is True,
            growth_intensity=_score100(value.get("growth_intensity"),"GROWTH_INTENSITY"),
            reinvestment_intensity=_score100(value.get("reinvestment_intensity"),"REINVESTMENT_INTENSITY"),
            general_capability_dimensions=tuple(value.get("general_capability_dimensions") or ()),
            value_capability_dimensions=tuple(value.get("value_capability_dimensions") or ()),
            retain_only_verified_improvements=value.get("retain_only_verified_improvements") is True,
            uncontrolled_self_modification_forbidden=value.get("uncontrolled_self_modification_forbidden") is True,
            lawful_only=value.get("lawful_only") is True,
            sustainable_only=value.get("sustainable_only") is True,
        )
        if not obj.capability_growth_enabled or not obj.sustainable_value_growth_enabled:
            raise StrategicDriveError("PERMANENT_DRIVES_MUST_BE_ENABLED")
        if (
            len(obj.general_capability_dimensions) != len(GENERAL_CAPABILITY_DIMENSIONS)
            or set(obj.general_capability_dimensions) != set(GENERAL_CAPABILITY_DIMENSIONS)
        ):
            raise StrategicDriveError("GENERAL_CAPABILITY_DIMENSIONS_INVALID")
        if (
            len(obj.value_capability_dimensions) != len(VALUE_CAPABILITY_DIMENSIONS)
            or set(obj.value_capability_dimensions) != set(VALUE_CAPABILITY_DIMENSIONS)
        ):
            raise StrategicDriveError("VALUE_CAPABILITY_DIMENSIONS_INVALID")
        if not obj.retain_only_verified_improvements:
            raise StrategicDriveError("VERIFIED_RETENTION_REQUIRED")
        if not obj.uncontrolled_self_modification_forbidden:
            raise StrategicDriveError("UNCONTROLLED_SELF_MODIFICATION_MUST_BE_FORBIDDEN")
        if not obj.lawful_only or not obj.sustainable_only:
            raise StrategicDriveError("VALUE_DRIVE_MUST_BE_LAWFUL_AND_SUSTAINABLE")
        return obj

    def to_dict(self) -> dict[str,Any]:
        return {
            "schema_version":SCHEMA_VERSION,
            "capability_growth_enabled":self.capability_growth_enabled,
            "sustainable_value_growth_enabled":self.sustainable_value_growth_enabled,
            "growth_intensity":self.growth_intensity,
            "reinvestment_intensity":self.reinvestment_intensity,
            "general_capability_dimensions":list(self.general_capability_dimensions),
            "value_capability_dimensions":list(self.value_capability_dimensions),
            "retain_only_verified_improvements":self.retain_only_verified_improvements,
            "uncontrolled_self_modification_forbidden":self.uncontrolled_self_modification_forbidden,
            "lawful_only":self.lawful_only,
            "sustainable_only":self.sustainable_only,
        }


DEFAULT_STRATEGIC_DRIVES=StrategicDriveConfig(
    capability_growth_enabled=True,
    sustainable_value_growth_enabled=True,
    growth_intensity=100,
    reinvestment_intensity=100,
    general_capability_dimensions=GENERAL_CAPABILITY_DIMENSIONS,
    value_capability_dimensions=VALUE_CAPABILITY_DIMENSIONS,
    retain_only_verified_improvements=True,
    uncontrolled_self_modification_forbidden=True,
    lawful_only=True,
    sustainable_only=True,
)


@dataclass(frozen=True)
class ImprovementCandidate:
    candidate_id: str
    target_component: str
    hypothesis: str
    change_scopes: tuple[str,...]
    expected_capability_gain: int
    rollback_plan: str
    sandbox_only_before_acceptance: bool
    requires_owner_review: bool
    production_effect: bool
    spend_effect: bool

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "ImprovementCandidate":
        scopes=tuple(_text(x,"CHANGE_SCOPE",maximum=500) for x in (value.get("change_scopes") or []))
        return cls(
            candidate_id=_text(value.get("candidate_id"),"CANDIDATE_ID",maximum=200),
            target_component=_text(value.get("target_component"),"TARGET_COMPONENT",maximum=200),
            hypothesis=_text(value.get("hypothesis"),"HYPOTHESIS"),
            change_scopes=scopes,
            expected_capability_gain=_score100(value.get("expected_capability_gain"),"EXPECTED_CAPABILITY_GAIN"),
            rollback_plan=_text(value.get("rollback_plan"),"ROLLBACK_PLAN"),
            sandbox_only_before_acceptance=value.get("sandbox_only_before_acceptance") is True,
            requires_owner_review=value.get("requires_owner_review") is True,
            production_effect=value.get("production_effect") is True,
            spend_effect=value.get("spend_effect") is True,
        )


@dataclass(frozen=True)
class ImprovementEvidence:
    artifact_sha: str
    tests_executed: bool
    benchmark_ids: tuple[str,...]
    acceptance_passed: bool
    rollback_tested: bool
    critical_regressions: tuple[str,...]
    identity_pinned: bool
    fallback_policy_verified: bool
    measured_gain: Optional[float]

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "ImprovementEvidence":
        measured=value.get("measured_gain")
        if measured is not None:
            if isinstance(measured,bool) or not isinstance(measured,(int,float)):
                raise StrategicDriveError("MEASURED_GAIN_INVALID")
            measured=float(measured)
        return cls(
            artifact_sha=_text(value.get("artifact_sha"),"ARTIFACT_SHA",maximum=200),
            tests_executed=value.get("tests_executed") is True,
            benchmark_ids=tuple(_text(x,"BENCHMARK_ID",maximum=300) for x in (value.get("benchmark_ids") or [])),
            acceptance_passed=value.get("acceptance_passed") is True,
            rollback_tested=value.get("rollback_tested") is True,
            critical_regressions=tuple(_text(x,"CRITICAL_REGRESSION",maximum=1000) for x in (value.get("critical_regressions") or [])),
            identity_pinned=value.get("identity_pinned") is True,
            fallback_policy_verified=value.get("fallback_policy_verified") is True,
            measured_gain=measured,
        )


def evaluate_improvement(candidate: ImprovementCandidate, evidence: Optional[ImprovementEvidence]) -> ImprovementDecision:
    if touches_root_boundary(candidate.change_scopes):
        return ImprovementDecision.REJECT
    if not candidate.sandbox_only_before_acceptance:
        return ImprovementDecision.REJECT
    if evidence is None or not evidence.tests_executed:
        return ImprovementDecision.RETEST
    if not evidence.artifact_sha or not evidence.benchmark_ids:
        return ImprovementDecision.RETEST
    if not evidence.acceptance_passed or evidence.critical_regressions:
        return ImprovementDecision.REJECT
    if not evidence.rollback_tested:
        return ImprovementDecision.RETEST
    if candidate.target_component.upper() in {"SOL","GEMINI","MANUS","CORE_TRIAD","JAYTEC_CONTROL_PLANE"}:
        if not evidence.identity_pinned or not evidence.fallback_policy_verified:
            return ImprovementDecision.RETEST
    if evidence.measured_gain is None or evidence.measured_gain <= 0:
        return ImprovementDecision.REJECT
    if candidate.requires_owner_review or candidate.production_effect or candidate.spend_effect:
        return ImprovementDecision.OWNER_REVIEW
    return ImprovementDecision.RETAIN


@dataclass(frozen=True)
class CapabilityGap:
    capability: str
    current_score: int
    target_score: int
    evidence_confidence: float
    blocking_dependencies: tuple[str,...]

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "CapabilityGap":
        current=_score100(value.get("current_score"),"CURRENT_SCORE")
        target=_score100(value.get("target_score"),"TARGET_SCORE")
        if target < current:
            raise StrategicDriveError("TARGET_BELOW_CURRENT")
        return cls(
            capability=_text(value.get("capability"),"CAPABILITY",maximum=200),
            current_score=current,
            target_score=target,
            evidence_confidence=_ratio(value.get("evidence_confidence"),"EVIDENCE_CONFIDENCE"),
            blocking_dependencies=tuple(_text(x,"BLOCKING_DEPENDENCY",maximum=500) for x in (value.get("blocking_dependencies") or [])),
        )

    @property
    def gap(self) -> int:
        return self.target_score-self.current_score


def rank_capability_gaps(gaps: list[CapabilityGap]) -> list[CapabilityGap]:
    return sorted(gaps,key=lambda g:(-(g.gap*max(g.evidence_confidence,0.05)),g.capability))


@dataclass(frozen=True)
class ExecutionAuthorityContext:
    """Verified execution authority supplied by JAYTEC, never self-asserted by Forge."""

    source: str
    current_task_authorized: bool
    budget_authority_verified: bool
    budget_authority_ref: Optional[str]

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "ExecutionAuthorityContext":
        source=_text(value.get("source"),"AUTHORITY_SOURCE",maximum=200)
        if source != "JAYTEC_EXECUTION_AUTHORITY":
            raise StrategicDriveError("AUTHORITY_SOURCE_INVALID")
        ref=value.get("budget_authority_ref")
        ref_text=None if ref is None else _text(ref,"BUDGET_AUTHORITY_REF",maximum=500)
        verified=value.get("budget_authority_verified") is True
        current=value.get("current_task_authorized") is True
        if verified and (not current or not ref_text):
            raise StrategicDriveError("BUDGET_AUTHORITY_PROOF_INCOMPLETE")
        return cls(
            source=source,
            current_task_authorized=current,
            budget_authority_verified=verified,
            budget_authority_ref=ref_text,
        )


@dataclass(frozen=True)
class ValueOpportunity:
    opportunity_id: str
    mechanism: str
    expected_value_score: int
    capability_synergy: int
    capital_efficiency: int
    time_to_value_score: int
    evidence_confidence: float
    downside_risk: int
    ongoing_burden: int
    lawful: bool
    sustainable: bool
    deceptive: bool
    unauthorized_access: bool
    regulated_or_licensed_activity: bool
    required_scopes: tuple[str,...]
    requires_external_spend: bool
    requires_new_legal_entity_or_account: bool
    known_obligations_covered: bool
    funds_or_resources_available: bool

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "ValueOpportunity":
        return cls(
            opportunity_id=_text(value.get("opportunity_id"),"OPPORTUNITY_ID",maximum=200),
            mechanism=_text(value.get("mechanism"),"MECHANISM"),
            expected_value_score=_score100(value.get("expected_value_score"),"EXPECTED_VALUE_SCORE"),
            capability_synergy=_score100(value.get("capability_synergy"),"CAPABILITY_SYNERGY"),
            capital_efficiency=_score100(value.get("capital_efficiency"),"CAPITAL_EFFICIENCY"),
            time_to_value_score=_score100(value.get("time_to_value_score"),"TIME_TO_VALUE_SCORE"),
            evidence_confidence=_ratio(value.get("evidence_confidence"),"EVIDENCE_CONFIDENCE"),
            downside_risk=_score100(value.get("downside_risk"),"DOWNSIDE_RISK"),
            ongoing_burden=_score100(value.get("ongoing_burden"),"ONGOING_BURDEN"),
            lawful=value.get("lawful") is True,
            sustainable=value.get("sustainable") is True,
            deceptive=value.get("deceptive") is True,
            unauthorized_access=value.get("unauthorized_access") is True,
            regulated_or_licensed_activity=value.get("regulated_or_licensed_activity") is True,
            required_scopes=tuple(_text(x,"REQUIRED_SCOPE",maximum=500) for x in (value.get("required_scopes") or [])),
            requires_external_spend=value.get("requires_external_spend") is True,
            requires_new_legal_entity_or_account=value.get("requires_new_legal_entity_or_account") is True,
            known_obligations_covered=value.get("known_obligations_covered") is True,
            funds_or_resources_available=value.get("funds_or_resources_available") is True,
        )


def value_opportunity_score(o: ValueOpportunity) -> float:
    upside=(
        o.expected_value_score*0.35
        + o.capability_synergy*0.30
        + o.capital_efficiency*0.20
        + o.time_to_value_score*0.15
    )
    burden=(o.downside_risk*0.60 + o.ongoing_burden*0.40)
    return round((upside*o.evidence_confidence) - burden*0.35,4)


def evaluate_value_opportunity(
    o: ValueOpportunity,
    authority: Optional[ExecutionAuthorityContext]=None,
) -> OpportunityDecision:
    if not o.lawful or not o.sustainable or o.deceptive or o.unauthorized_access:
        return OpportunityDecision.REJECT
    if touches_root_boundary(o.required_scopes):
        return OpportunityDecision.REJECT
    if o.evidence_confidence < 0.35:
        return OpportunityDecision.RESEARCH
    if not o.known_obligations_covered or not o.funds_or_resources_available:
        return OpportunityDecision.RESEARCH
    if o.regulated_or_licensed_activity or o.requires_new_legal_entity_or_account:
        return OpportunityDecision.OWNER_REVIEW
    if o.requires_external_spend:
        if (
            authority is None
            or not authority.current_task_authorized
            or not authority.budget_authority_verified
            or not authority.budget_authority_ref
        ):
            return OpportunityDecision.OWNER_REVIEW
    return OpportunityDecision.EXECUTE


def rank_value_opportunities(opportunities: list[ValueOpportunity]) -> list[ValueOpportunity]:
    eligible=[o for o in opportunities if evaluate_value_opportunity(o) is not OpportunityDecision.REJECT]
    return sorted(eligible,key=lambda o:(-value_opportunity_score(o),o.opportunity_id))



@dataclass(frozen=True)
class LearningEvidence:
    evaluation_id: str
    executed: bool
    source_before: float
    source_after: float
    transfer_before: float
    transfer_after: float
    retention_before: float
    retention_after: float
    critical_regressions: tuple[str,...]

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "LearningEvidence":
        def metric(name: str) -> float:
            raw=value.get(name)
            if isinstance(raw,bool) or not isinstance(raw,(int,float)):
                raise StrategicDriveError(name.upper()+"_INVALID")
            out=float(raw)
            if not 0.0 <= out <= 1.0:
                raise StrategicDriveError(name.upper()+"_OUT_OF_RANGE")
            return out
        return cls(
            evaluation_id=_text(value.get("evaluation_id"),"EVALUATION_ID",maximum=200),
            executed=value.get("executed") is True,
            source_before=metric("source_before"),
            source_after=metric("source_after"),
            transfer_before=metric("transfer_before"),
            transfer_after=metric("transfer_after"),
            retention_before=metric("retention_before"),
            retention_after=metric("retention_after"),
            critical_regressions=tuple(_text(x,"CRITICAL_REGRESSION",maximum=1000) for x in (value.get("critical_regressions") or [])),
        )


def learning_update_is_verified(e: LearningEvidence, *, max_retention_regression: float=0.02) -> bool:
    if not e.executed or e.critical_regressions:
        return False
    source_gain=e.source_after-e.source_before
    transfer_gain=e.transfer_after-e.transfer_before
    retention_delta=e.retention_after-e.retention_before
    return source_gain > 0 and transfer_gain > 0 and retention_delta >= -abs(max_retention_regression)


@dataclass(frozen=True)
class ReinvestmentTarget:
    target_id: str
    category: str
    expected_capability_multiplier: int
    expected_value_multiplier: int
    capital_efficiency: int
    evidence_confidence: float
    recurring_burden: int
    lawful: bool
    sustainable: bool
    required_scopes: tuple[str,...]
    requires_external_spend: bool
    funds_available: bool
    known_obligations_covered: bool

    @classmethod
    def parse(cls, value: Mapping[str,Any]) -> "ReinvestmentTarget":
        return cls(
            target_id=_text(value.get("target_id"),"TARGET_ID",maximum=200),
            category=_text(value.get("category"),"CATEGORY",maximum=200),
            expected_capability_multiplier=_score100(value.get("expected_capability_multiplier"),"EXPECTED_CAPABILITY_MULTIPLIER"),
            expected_value_multiplier=_score100(value.get("expected_value_multiplier"),"EXPECTED_VALUE_MULTIPLIER"),
            capital_efficiency=_score100(value.get("capital_efficiency"),"CAPITAL_EFFICIENCY"),
            evidence_confidence=_ratio(value.get("evidence_confidence"),"EVIDENCE_CONFIDENCE"),
            recurring_burden=_score100(value.get("recurring_burden"),"RECURRING_BURDEN"),
            lawful=value.get("lawful") is True,
            sustainable=value.get("sustainable") is True,
            required_scopes=tuple(_text(x,"REQUIRED_SCOPE",maximum=500) for x in (value.get("required_scopes") or [])),
            requires_external_spend=value.get("requires_external_spend") is True,
            funds_available=value.get("funds_available") is True,
            known_obligations_covered=value.get("known_obligations_covered") is True,
        )


def reinvestment_target_score(t: ReinvestmentTarget) -> float:
    leverage=(
        t.expected_capability_multiplier*0.45
        + t.expected_value_multiplier*0.30
        + t.capital_efficiency*0.25
    )
    return round(leverage*t.evidence_confidence - t.recurring_burden*0.25,4)


def reinvestment_target_decision(
    t: ReinvestmentTarget,
    authority: Optional[ExecutionAuthorityContext]=None,
) -> OpportunityDecision:
    if not t.lawful or not t.sustainable or touches_root_boundary(t.required_scopes):
        return OpportunityDecision.REJECT
    if t.evidence_confidence < 0.35 or not t.funds_available or not t.known_obligations_covered:
        return OpportunityDecision.RESEARCH
    if t.requires_external_spend:
        if (
            authority is None
            or not authority.current_task_authorized
            or not authority.budget_authority_verified
            or not authority.budget_authority_ref
        ):
            return OpportunityDecision.OWNER_REVIEW
    return OpportunityDecision.EXECUTE


def build_aggressive_reinvestment_plan(
    targets: list[ReinvestmentTarget],
    authority: Optional[ExecutionAuthorityContext]=None,
) -> list[dict[str,Any]]:
    eligible=[
        t for t in targets
        if reinvestment_target_decision(t,authority) in {OpportunityDecision.EXECUTE,OpportunityDecision.OWNER_REVIEW}
    ]
    ranked=sorted(eligible,key=lambda t:(-reinvestment_target_score(t),t.target_id))
    executable=[t for t in ranked if reinvestment_target_decision(t,authority) is OpportunityDecision.EXECUTE]
    exec_scores=[max(reinvestment_target_score(t),0.0) for t in executable]
    exec_total=sum(exec_scores)
    weights={}
    for target,score in zip(executable,exec_scores):
        weights[target.target_id]=(score/exec_total) if exec_total>0 else (1.0/len(executable) if executable else 0.0)
    out=[]
    for target in ranked:
        decision=reinvestment_target_decision(target,authority)
        out.append({
            "target_id":target.target_id,
            "category":target.category,
            "decision":decision.value,
            "priority_score":reinvestment_target_score(target),
            "surplus_allocation_weight":round(weights.get(target.target_id,0.0),6),
        })
    return out


def build_strategic_drive_packet(
    *,
    root_boundary: RootOwnerBoundary,
    drive_config: StrategicDriveConfig=DEFAULT_STRATEGIC_DRIVES,
) -> dict[str,Any]:
    validate_root_owner_boundary(root_boundary)
    return {
        "schema_version":SCHEMA_VERSION,
        "permanent_drives":{
            "general_capability_growth":{
                "enabled":True,
                "intensity":drive_config.growth_intensity,
                "dimensions":list(drive_config.general_capability_dimensions),
                "rule":"Identify capability gaps, propose bounded improvements, test them, retain only verified gains, and keep learning across domains.",
            },
            "lawful_sustainable_value_growth":{
                "enabled":True,
                "intensity":drive_config.reinvestment_intensity,
                "dimensions":list(drive_config.value_capability_dimensions),
                "rule":"Continuously seek lawful sustainable value creation and aggressively reinvest verified surplus/resources into capability multipliers.",
            },
        },
        "improvement_governance":{
            "retain_only_verified_improvements":True,
            "sandbox_before_acceptance":True,
            "rollback_required":True,
            "critical_regression_blocks_retention":True,
            "core_triad_identity_and_fallback_checks_required":True,
            "uncontrolled_self_modification_forbidden":True,
        },
        "root_owner_boundary":root_boundary.to_dict(),
        "root_owner_rule":"JAY_ROOT_OWNER is the sole immutable ultimate authority. Forge can evolve beneath this boundary but cannot own, replace, transfer, mint, bypass, weaken, duplicate or supersede it.",
    }
