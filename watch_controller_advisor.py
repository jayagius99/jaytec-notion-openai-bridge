"""Bounded SOL advisory path for JAYTEC WATCH controller decisions.

SOL is consulted as a subordinate engineering/reasoning specialist. The result
is advisory evidence only: it cannot grant assignment-owner, ROOT, spend,
credential, merge, deployment, or activation authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping

REQUEST_SCHEMA = "JAYTEC_WATCH_CONTROLLER_ADVICE_REQUEST_V1"
RESULT_SCHEMA = "JAYTEC_WATCH_CONTROLLER_ADVICE_RESULT_V1"
TASK_ID = "FORGE-GENESIS-ACTIVATION-001"
EXPECTED_SOL_MODEL = "gpt-5.6-sol"
ALLOWED_DECISIONS = frozenset(
    {"APPROVE_CONTINUE", "REDIRECT", "REJECT_EVIDENCE", "ESCALATE_JAY"}
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GATE_RE = re.compile(r"^G[0-9]{2,4}$")
MAX_TEXT = 1800
MAX_LIST = 32
MAX_REQUEST_BYTES = 24_000
MAX_RESULT_BYTES = 24_000


class WatchControllerAdvisorError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value: Any, name: str, *, maximum: int = MAX_TEXT, required: bool = True) -> str:
    out = str(value or "").strip()
    if required and not out:
        raise WatchControllerAdvisorError(name + "_REQUIRED")
    if len(out) > maximum:
        raise WatchControllerAdvisorError(name + "_TOO_LONG")
    return out


def _sha(value: Any, name: str) -> str:
    out = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(out):
        raise WatchControllerAdvisorError(name + "_INVALID")
    return out


def _string_list(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise WatchControllerAdvisorError(name + "_INVALID")
    out = [_text(item, name + "_ITEM", maximum=900) for item in value]
    if required and not out:
        raise WatchControllerAdvisorError(name + "_REQUIRED")
    return out


def make_request(
    *,
    gate_id: str,
    checkpoint_number: int,
    controller_generation: int,
    graph_sha256: str,
    gate_result_sha256: str,
    manifest_sha256: str,
    jaytec_review_id: str,
    jaytec_review_decision: str,
    evidence_reviewed: list[str],
    unresolved_items: list[str],
    worker_id: str,
    fencing_token: int,
    current_direction: str = "",
    candidate_next_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    gate = _text(gate_id, "GATE_ID", maximum=8)
    if not GATE_RE.fullmatch(gate):
        raise WatchControllerAdvisorError("GATE_ID_INVALID")
    if type(checkpoint_number) is not int or checkpoint_number < 1:
        raise WatchControllerAdvisorError("CHECKPOINT_INVALID")
    if type(controller_generation) is not int or controller_generation < 0:
        raise WatchControllerAdvisorError("CONTROLLER_GENERATION_INVALID")
    if type(fencing_token) is not int or fencing_token < 0:
        raise WatchControllerAdvisorError("FENCING_TOKEN_INVALID")

    review_decision = _text(
        jaytec_review_decision,
        "JAYTEC_REVIEW_DECISION",
        maximum=64,
    )
    if review_decision not in {"EVIDENCE_ACCEPTED", "REPLAN_REQUIRED"}:
        raise WatchControllerAdvisorError("JAYTEC_REVIEW_DECISION_INVALID")

    candidate = dict(candidate_next_gate or {})
    if candidate:
        allowed = {
            "gate_id",
            "title",
            "phase",
            "owner_boundary",
            "watch_eligible",
        }
        if set(candidate) != allowed:
            raise WatchControllerAdvisorError("CANDIDATE_NEXT_GATE_FIELDS_INVALID")
        if not GATE_RE.fullmatch(str(candidate.get("gate_id") or "")):
            raise WatchControllerAdvisorError("CANDIDATE_NEXT_GATE_ID_INVALID")
        _text(candidate.get("title"), "CANDIDATE_NEXT_GATE_TITLE", maximum=240)
        _text(candidate.get("phase"), "CANDIDATE_NEXT_GATE_PHASE", maximum=80)
        if type(candidate.get("owner_boundary")) is not bool:
            raise WatchControllerAdvisorError("CANDIDATE_NEXT_GATE_OWNER_FLAG_INVALID")
        if type(candidate.get("watch_eligible")) is not bool:
            raise WatchControllerAdvisorError("CANDIDATE_NEXT_GATE_WATCH_FLAG_INVALID")

    base = {
        "schema_version": REQUEST_SCHEMA,
        "task_id": TASK_ID,
        "gate_id": gate,
        "checkpoint_number": checkpoint_number,
        "controller_generation": controller_generation,
        "graph_sha256": _sha(graph_sha256, "GRAPH_SHA256"),
        "gate_result_sha256": _sha(gate_result_sha256, "GATE_RESULT_SHA256"),
        "manifest_sha256": _sha(manifest_sha256, "MANIFEST_SHA256"),
        "jaytec_review_id": _sha(jaytec_review_id, "JAYTEC_REVIEW_ID"),
        "jaytec_review_decision": review_decision,
        "evidence_reviewed": _string_list(
            evidence_reviewed, "EVIDENCE_REVIEWED", required=True
        ),
        "unresolved_items": _string_list(unresolved_items, "UNRESOLVED_ITEMS"),
        "worker_id": _text(worker_id, "WORKER_ID", maximum=240),
        "fencing_token": fencing_token,
        "current_direction": _text(
            current_direction,
            "CURRENT_DIRECTION",
            maximum=1200,
            required=False,
        ),
        "candidate_next_gate": candidate,
    }
    base["request_sha256"] = _digest(base)
    if len(_canonical(base)) > MAX_REQUEST_BYTES:
        raise WatchControllerAdvisorError("REQUEST_TOO_LARGE")
    return base


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WatchControllerAdvisorError("REQUEST_INVALID")
    expected = {
        "schema_version",
        "task_id",
        "gate_id",
        "checkpoint_number",
        "controller_generation",
        "graph_sha256",
        "gate_result_sha256",
        "manifest_sha256",
        "jaytec_review_id",
        "jaytec_review_decision",
        "evidence_reviewed",
        "unresolved_items",
        "worker_id",
        "fencing_token",
        "current_direction",
        "candidate_next_gate",
        "request_sha256",
    }
    if set(value) != expected:
        raise WatchControllerAdvisorError("REQUEST_FIELDS_INVALID")
    if value.get("schema_version") != REQUEST_SCHEMA:
        raise WatchControllerAdvisorError("REQUEST_SCHEMA_INVALID")
    if value.get("task_id") != TASK_ID:
        raise WatchControllerAdvisorError("REQUEST_TASK_INVALID")
    rebuilt = make_request(
        gate_id=str(value.get("gate_id") or ""),
        checkpoint_number=value.get("checkpoint_number"),
        controller_generation=value.get("controller_generation"),
        graph_sha256=str(value.get("graph_sha256") or ""),
        gate_result_sha256=str(value.get("gate_result_sha256") or ""),
        manifest_sha256=str(value.get("manifest_sha256") or ""),
        jaytec_review_id=str(value.get("jaytec_review_id") or ""),
        jaytec_review_decision=str(value.get("jaytec_review_decision") or ""),
        evidence_reviewed=value.get("evidence_reviewed"),
        unresolved_items=value.get("unresolved_items"),
        worker_id=str(value.get("worker_id") or ""),
        fencing_token=value.get("fencing_token"),
        current_direction=str(value.get("current_direction") or ""),
        candidate_next_gate=value.get("candidate_next_gate"),
    )
    if value.get("request_sha256") != rebuilt["request_sha256"]:
        raise WatchControllerAdvisorError("REQUEST_DIGEST_MISMATCH")
    return rebuilt


def build_sol_packet(request: Mapping[str, Any]) -> dict[str, Any]:
    req = validate_request(request)
    return {
        "task_id": req["task_id"],
        "subtask_id": "watch-controller-advice:" + req["request_sha256"][:16],
        "workflow_id": "JAYTEC_WATCH_SOL_CONTROLLER_ADVICE_V1",
        "request": (
            "Review the supplied JAYTEC gate evidence/controller state and return a "
            "bounded advisory decision for WATCH. Preserve the current worker/fence. "
            "Do not claim owner authority. Do not perform side effects. "
            "Allowed conclusion.decision values are APPROVE_CONTINUE, REDIRECT, "
            "REJECT_EVIDENCE, ESCALATE_JAY. conclusion.direction must be a precise "
            "bounded next instruction (empty only when APPROVE_CONTINUE needs no "
            "special direction). conclusion.rationale must explain the evidence basis."
        ),
        "objective": "Provide bounded SOL controller advice for the current WATCH gate.",
        "allowed_operations": [],
        "max_retries": 0,
        "required_context": {
            "authority_controller": "CHATGPT_OPENAI_LEAD",
            "specialist_authority": "SUBORDINATE_ADVISORY_ONLY",
            "source": "JAYTEC_WATCH_CONTROLLER_ADVISOR",
            "return_to": "WATCH_CONTROLLER",
            "controller_request": req,
        },
    }


def _validate_sol_result(raw: Mapping[str, Any], req: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("model") != EXPECTED_SOL_MODEL:
        raise WatchControllerAdvisorError("SOL_MODEL_MISMATCH")
    if raw.get("side_effects_attempted") not in (None, []):
        raise WatchControllerAdvisorError("SOL_SIDE_EFFECT_ATTEMPT")
    if raw.get("requested_operations") not in (None, []):
        raise WatchControllerAdvisorError("SOL_OPERATION_REQUEST_FORBIDDEN")
    if raw.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS", "NEEDS_VALIDATION"}:
        raise WatchControllerAdvisorError(
            "SOL_STATUS_NOT_ACCEPTABLE:" + str(raw.get("status") or "")
        )

    conclusion = raw.get("conclusion")
    if not isinstance(conclusion, Mapping):
        raise WatchControllerAdvisorError("SOL_CONCLUSION_INVALID")
    decision = str(conclusion.get("decision") or "").strip()
    if decision not in ALLOWED_DECISIONS:
        raise WatchControllerAdvisorError("SOL_DECISION_INVALID")
    direction = _text(
        conclusion.get("direction"),
        "SOL_DIRECTION",
        maximum=1600,
        required=(decision != "APPROVE_CONTINUE"),
    )
    rationale = _text(
        conclusion.get("rationale"),
        "SOL_RATIONALE",
        maximum=2200,
    )

    if req["jaytec_review_decision"] == "REPLAN_REQUIRED" and decision == "APPROVE_CONTINUE":
        raise WatchControllerAdvisorError("SOL_CANNOT_BYPASS_REPLAN")
    if req["unresolved_items"] and decision == "APPROVE_CONTINUE":
        raise WatchControllerAdvisorError("SOL_CANNOT_APPROVE_UNRESOLVED")
    candidate = req.get("candidate_next_gate") or {}
    if (
        isinstance(candidate, Mapping)
        and candidate.get("owner_boundary") is True
        and decision == "APPROVE_CONTINUE"
        and req["jaytec_review_decision"] != "EVIDENCE_ACCEPTED"
    ):
        raise WatchControllerAdvisorError("SOL_OWNER_BOUNDARY_BYPASS")

    findings = raw.get("findings") if isinstance(raw.get("findings"), list) else []
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    unresolved = (
        raw.get("unresolved_items")
        if isinstance(raw.get("unresolved_items"), list)
        else []
    )
    result = {
        "schema_version": RESULT_SCHEMA,
        "task_id": TASK_ID,
        "request_sha256": req["request_sha256"],
        "gate_id": req["gate_id"],
        "checkpoint_number": req["checkpoint_number"],
        "worker_id": req["worker_id"],
        "fencing_token": req["fencing_token"],
        "authority": "ADVISORY_ONLY_NO_ASSIGNMENT_OWNER_AUTHORITY",
        "model": EXPECTED_SOL_MODEL,
        "decision": decision,
        "direction": direction,
        "rationale": rationale,
        "findings": [str(x)[:900] for x in findings[:MAX_LIST]],
        "evidence": [str(x)[:900] for x in evidence[:MAX_LIST]],
        "unresolved_items": [str(x)[:900] for x in unresolved[:MAX_LIST]],
        "confidence": raw.get("confidence"),
        "provider_diagnostics": dict(raw.get("bridge_diagnostics") or {}),
    }
    unsigned = dict(result)
    result["result_sha256"] = _digest(unsigned)
    if len(_canonical(result)) > MAX_RESULT_BYTES:
        raise WatchControllerAdvisorError("RESULT_TOO_LARGE")
    return result


def advise(
    request: Mapping[str, Any],
    *,
    engineering_dispatch: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    req = validate_request(request)
    packet = build_sol_packet(req)
    try:
        raw = engineering_dispatch(packet)
    except Exception as exc:
        return {
            "schema_version": RESULT_SCHEMA,
            "status": "FAILED_CLOSED",
            "task_id": TASK_ID,
            "request_sha256": req["request_sha256"],
            "gate_id": req["gate_id"],
            "authority": "ADVISORY_ONLY_NO_ASSIGNMENT_OWNER_AUTHORITY",
            "reason": "SOL_ADVISOR_UNAVAILABLE:" + type(exc).__name__,
        }
    if not isinstance(raw, Mapping):
        raise WatchControllerAdvisorError("SOL_RESULT_INVALID")
    result = _validate_sol_result(raw, req)
    return {"status": "PASS", **result}


__all__ = [
    "ALLOWED_DECISIONS",
    "EXPECTED_SOL_MODEL",
    "REQUEST_SCHEMA",
    "RESULT_SCHEMA",
    "TASK_ID",
    "WatchControllerAdvisorError",
    "advise",
    "build_sol_packet",
    "make_request",
    "validate_request",
]
