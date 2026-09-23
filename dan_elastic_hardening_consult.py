"""JAYTEC:HELP hardening consultation for DAN / role-elastic completion.

Staging-only, one-shot, advisory-only. Uses JAYTEC's existing exact free
specialist contracts for DeepSeek and Nemo. No writes, deployments, tool actions,
or authority changes are delegated to the specialists.
"""
from __future__ import annotations

import hashlib
import json
import os

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from deepseek_reviewer import (
    EXPECTED_DEEPSEEK_REVIEWER_MODEL,
    build_deepseek_security_review_dispatch,
)
from nemo_specialist import EXPECTED_NEMO_MODEL, build_nemo_dispatch

BASE_URL = os.environ.get("OPENROUTER_BASE_URL","https://openrouter.ai/api/v1").strip()
API_KEY = os.environ.get("OPENROUTER_API_KEY","").strip()
MARKER = "/tmp/jaytec_dan_elastic_hardening_consult_v1.json"

REVIEW_PACKET = {
    "task_id":"JAYTEC-DAN-ELASTIC-HARDENING-2026-09-24",
    "subtask_id":"independent-hardening-review",
    "workflow_id":"JAYTEC_HELP_DAN_ELASTIC_HARDENING_V1",
    "objective":"Independently attack and harden the bounded DAN / role-elastic completion contract before package promotion.",
    "request":(
        "Review this exact design and propose concrete hardening only where justified: "
        "roles remain ownership/accountability labels, not hard capability walls; for an "
        "owner-authorized benign objective, a specialist may perform adjacent legitimate "
        "capabilities needed to complete the same task; capability never creates authority; "
        "STOP/HOLD, safety, permissions, spend gates, canonical state, idempotency, and "
        "evidence requirements remain binding; deterministic facts must be checked by the "
        "runtime when possible. Attack for ambiguity, scope creep, authority leakage, "
        "fabricated evidence, unsafe fallback, duplicate side effects, stale-state misuse, "
        "model overclaiming, verifier weaknesses, and hidden collision risks. Return the "
        "smallest set of concrete amendments or tests that materially improve the contract. "
        "Do not perform side effects and do not claim production certification."
    ),
    "allowed_operations":[],
    "max_retries":0,
    "required_context":{
        "authority_controller":"CHATGPT_OPENAI_LEAD",
        "specialist_authority":"SUBORDINATE_REVIEW_ONLY",
        "source":"JAYTEC_HELP",
        "return_to":"CHATGPT_OPENAI_LEAD",
        "current_evidence":[
            "baseline narrow-role model stopped on benign adjacent task",
            "role-elastic model completed adjacent task",
            "runtime independently verified deterministic output",
            "live boundary suite preserved HOLD, secret, spend, injection, stale-state and ambiguous-write rules",
            "first model-generated SHA was wrong and was replaced with runtime verification",
        ],
    },
}

def _digest(v):
    return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False).encode("utf-8")).hexdigest()

def run():
    if not API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY_MISSING")
    client=OpenAI(api_key=API_KEY,base_url=BASE_URL)

    deepseek=build_deepseek_security_review_dispatch(
        openrouter_client=client,
        model=EXPECTED_DEEPSEEK_REVIEWER_MODEL,
        timeout_s=120.0,
        circuit=CircuitBreaker(failure_threshold=2,reset_after_seconds=60),
        max_output_tokens=2200,
    )
    nemo=build_nemo_dispatch(
        openrouter_client=client,
        model=EXPECTED_NEMO_MODEL,
        timeout_s=120.0,
        circuit=CircuitBreaker(failure_threshold=2,reset_after_seconds=60),
        max_output_tokens=1800,
    )

    results={}
    errors={}
    for name, dispatch in (("deepseek",deepseek),("nemo",nemo)):
        try:
            result=dispatch(REVIEW_PACKET)
            results[name]=result
        except Exception as exc:
            errors[name]={
                "error_class":type(exc).__name__,
                "error_text_sha256":hashlib.sha256(str(exc).encode("utf-8",errors="replace")).hexdigest(),
            }

    return {
        "schema":"JAYTEC_DAN_ELASTIC_HARDENING_CONSULT_V1",
        "review_packet_sha256":_digest(REVIEW_PACKET),
        "results":results,
        "errors":errors,
        "reviewers_expected":["deepseek","nemo"],
        "side_effects_delegated":False,
    }

def _once():
    if os.path.exists(MARKER):
        print("JAYTEC_DAN_ELASTIC_HARDENING_ALREADY_RAN",flush=True)
        return
    try:
        result=run()
        with open(MARKER,"w",encoding="utf-8") as f:
            json.dump(result,f,sort_keys=True)
        print("JAYTEC_DAN_ELASTIC_HARDENING_RESULT="+json.dumps(result,sort_keys=True),flush=True)
    except Exception as exc:
        print("JAYTEC_DAN_ELASTIC_HARDENING_ERROR="+type(exc).__name__+":"+str(exc),flush=True)

if os.environ.get("JAYTEC_DAN_ELASTIC_HARDENING_ON_START","").strip().lower() in {"1","true","yes","on"}:
    _once()
