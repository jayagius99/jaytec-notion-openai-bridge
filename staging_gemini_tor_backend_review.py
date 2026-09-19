"""One-shot Gemini backend challenge for JAYTEC V3 Tor specialist.

STAGING / RESEARCH ONLY. Advisory only. No Tor changes, no Notion, no Manus.
"""
from __future__ import annotations

import json
import os

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch

ENABLED = os.environ.get("RUN_LIVE_GEMINI_TOR_BACKEND_REVIEW", "0").strip() == "1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = min(max(float(os.environ.get("GEMINI_TIMEOUT_S", "90")), 10), 180)

EVIDENCE = [
    {
        "source": "Tor SOCKS extensions spec",
        "url": "https://spec.torproject.org/socks-extensions",
        "facts": [
            "Modern Tor and Arti support application-provided stream isolation parameters via SOCKS5.",
            "Arti supports the defined isolation extension formats; different isolation parameters cannot share a circuit."
        ]
    },
    {
        "source": "Tor stream-isolation spec",
        "url": "https://spec.torproject.org/path-spec/stream-isolation.html",
        "facts": [
            "Application-provided isolation tokens are considered strong isolation properties.",
            "Streams from different accounts/applications/sessions should generally not share circuits."
        ]
    },
    {
        "source": "Arti FAQ / current docs",
        "url": "https://arti.torproject.org/FAQs/",
        "facts": [
            "As of January 2026 Arti is ready for client/proxy use.",
            "Arti still does not have every C Tor feature."
        ]
    },
    {
        "source": "Arti 2.6.0 release",
        "url": "https://blog.torproject.org/arti_2_6_0_released/",
        "facts": [
            "Arti 2.6.0 was released September 1, 2026.",
            "Development includes DNS stream handling, testing, documentation and bug fixes."
        ]
    },
    {
        "source": "Tor Project oniux isolation design",
        "url": "https://blog.torproject.org/introducing-oniux-tor-isolation-using-linux-namespaces/",
        "facts": [
            "Tor Project explicitly identifies proxy-only routing as vulnerable to application bypass.",
            "oniux uses Linux namespaces so an application lacks access to system-wide network interfaces and routes through Tor using Arti/onionmasq.",
            "The Tor Project also labels oniux itself experimental."
        ]
    },
    {
        "source": "Arti memory-safety rationale",
        "url": "https://arti.torproject.org/about/",
        "facts": [
            "Tor Project says Rust eliminates or makes unlikely a substantial class of historical C Tor memory-safety flaws.",
            "This is only one security dimension and does not prove feature completeness or correct deployment."
        ]
    },
    {
        "source": "Arti docs caveat",
        "url": "https://arti.torproject.org/guides/capability-limitations/",
        "facts": [
            "This page explicitly warns that details are out of date.",
            "It still illustrates that documentation maturity and feature parity must be checked version-by-version."
        ]
    }
]

REQUEST = """JAYTEC V3 TOR BACKEND CHALLENGE — RESEARCH ONLY.

You previously recommended C Tor as the default mainly because of mature
SafeSocks/IsolateSOCKSAuth behavior. New primary-source evidence shows modern
Arti supports strong application-provided stream isolation and is currently
described by Tor Project as ready for client/proxy use. Tor Project's oniux
work also demonstrates kernel/network-namespace isolation with Arti, though
oniux itself is experimental.

Re-evaluate WITHOUT defending your prior answer.

The architecture under review removes the most dangerous dependency on proxy
correctness:
- an untrusted fetch worker runs in a separate network namespace/container;
- that worker has NO direct clearnet route, NO ordinary DNS route, NO access to
  JAYTEC internal/LAN/metadata networks, and NO public listener;
- its only permitted network path is to a separate Tor engine compartment;
- network policy is enforced below the application at kernel/container level;
- each JAYTEC task provides a strong stream-isolation token;
- Tor outage, timeout, invalid destination, redirect violation, or uncertainty
  fails closed;
- no browser engine, active content, downloaded-document opening, credentials,
  cookies, arbitrary protocols, or system-wide proxying are in V3 scope.

Question: For the INITIAL V3 Tor Retrieval Specialist, should we:
A) pin C Tor,
B) pin Arti 2.6.x,
C) keep a backend-neutral TorEngine contract and qualify BOTH with the exact same
acceptance harness, selecting only the implementation that passes every required
invariant at release time?

Security is the priority, not novelty, language preference, or preserving a
previous recommendation.

Return conclusion with EXACT keys:
- verdict
- preferred_option
- why
- c_tor_security_strengths
- c_tor_security_weaknesses
- arti_security_strengths
- arti_security_weaknesses
- backend_independent_invariants
- backend_specific_tests
- evidence_that_would_change_the_decision
- residual_risks
- ready_for_architecture_decision

No side effects. No implementation. No Notion. No Manus. Do not claim either
engine can eliminate Tor's traffic-correlation threat.

PRIMARY_SOURCE_EVIDENCE:
""" + json.dumps(EVIDENCE, ensure_ascii=False, sort_keys=True)

def main() -> int:
    if not ENABLED:
        print(json.dumps({"event":"JAYTEC_GEMINI_TOR_BACKEND_REVIEW","status":"SKIP"}, sort_keys=True), flush=True)
        return 0
    if not OPENROUTER_API_KEY:
        print(json.dumps({"event":"JAYTEC_GEMINI_TOR_BACKEND_REVIEW","status":"FAILED_CLOSED","error":"OPENROUTER_API_KEY_NOT_CONFIGURED"}, sort_keys=True), flush=True)
        return 4

    packet = {
        "task_id": "JAYTEC-V3-TOR-BACKEND",
        "subtask_id": "GEMINI-BACKEND-CHALLENGE-3",
        "request": REQUEST,
        "intent": "Adversarial backend-neutral security decision for V3 Tor retrieval specialist",
        "workflow_id": "JAYTEC_V3_TOR_SECURITY_RESEARCH",
        "risk_level": "read_only_security_review",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read","research","analyze","validate"],
        "expected_output": "Backend decision advisory grounded only in explicit invariants and current evidence",
        "validation_requirements": [
            "no side effects",
            "challenge prior C Tor preference",
            "do not select by programming language alone",
            "treat kernel egress isolation as independent of Tor engine",
            "preserve traffic-correlation residual risk",
            "state falsifiable backend qualification tests"
        ],
        "side_effect_policy": "none",
        "idempotency_key": "gemini-v3-tor-backend-challenge-v1",
        "deadline": "2099-01-01T00:00:00Z",
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": {"primary_source_evidence": EVIDENCE},
        "constraints": [
            "review only",
            "do not use Notion",
            "do not use Manus",
            "do not perform engineering writes",
            "do not claim perfect anonymity"
        ]
    }

    dispatch = build_gemini_dispatch(
        openrouter_client=OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL),
        gemini_model=GEMINI_MODEL,
        gemini_timeout_s=GEMINI_TIMEOUT_S,
        circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=60),
    )
    result = dict(dispatch(packet))
    print(json.dumps({
        "event":"JAYTEC_GEMINI_TOR_BACKEND_REVIEW",
        "status":"COMPLETE_ADVISORY",
        "model":GEMINI_MODEL,
        "result":result,
        "authority":"advisory_only_chatgpt_must_audit",
        "side_effects":[]
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
