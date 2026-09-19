"""One-shot two-pass Gemini security review for proposed JAYTEC V3 Tor specialist.

STAGING / RESEARCH ONLY.
- No Tor installation or runtime change.
- No Notion or Manus use.
- No engineering writes.
- Exactly two bounded Gemini calls through the existing JAYTEC Gemini adapter.
- Results are advisory and must be independently audited by ChatGPT/Sol.
"""
from __future__ import annotations

import json
import os

from openai import OpenAI

from circuit_breaker import CircuitBreaker
from specialist_adapters import EXPECTED_GEMINI_MODEL, build_gemini_dispatch

ENABLED = os.environ.get("RUN_LIVE_GEMINI_TOR_SECURITY_REVIEW", "0").strip() == "1"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", EXPECTED_GEMINI_MODEL).strip()
GEMINI_TIMEOUT_S = min(max(float(os.environ.get("GEMINI_TIMEOUT_S", "90")), 10), 180)

EVIDENCE = [
    {
        "source": "Tor Project DNS leak guidance",
        "url": "https://support.torproject.org/little-t-tor/troubleshooting/check-for-leaks/",
        "facts": [
            "SOCKS-using applications can still leak DNS if they resolve names themselves.",
            "Tor recommends TestSocks to detect unsafe SOCKS usage and SafeSocks to reject it."
        ],
    },
    {
        "source": "Tor Browser best practices",
        "url": "https://support.torproject.org/tor-browser/security/using-tb-safely/",
        "facts": [
            "Tor only protects applications correctly configured to use Tor.",
            "Logging into identity-bearing accounts or supplying personal information defeats anonymity to the destination.",
            "Downloaded documents opened by external applications can fetch outside Tor and expose the real IP.",
            "Torrenting over Tor is explicitly unsafe."
        ],
    },
    {
        "source": "Tor Project threat model - network attackers",
        "url": "https://community.torproject.org/threat-model/threat-positioning/network/",
        "facts": [
            "Malicious relays and traffic manipulation remain threats.",
            "Traffic-pattern correlation between entry and exit observations can contribute to deanonymization."
        ],
    },
    {
        "source": "Tor VPN threat model",
        "url": "https://support.torproject.org/tor-vpn/security/threat-model/",
        "facts": [
            "Network leak safety requires applications to be unable to bypass the protected route, including startup and failure states.",
            "Network app isolation requires separate Tor circuits for independently isolated application activity."
        ],
    },
    {
        "source": "C Tor manual / SOCKS isolation",
        "url": "https://manpages.debian.org/testing/tor/tor.1.en.html",
        "facts": [
            "Exposing SocksPort beyond localhost is dangerous because SOCKS is unencrypted and commonly unauthenticated.",
            "IsolateSOCKSAuth can prevent streams with different SOCKS credentials from sharing circuits.",
            "SafeSocks rejects unsafe variants where the application resolved DNS before Tor."
        ],
    },
    {
        "source": "OWASP SSRF Prevention",
        "url": "https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html",
        "facts": [
            "URL fetchers require strict validation and network-layer restrictions.",
            "Redirect following can bypass validation and should be disabled or each hop independently revalidated.",
            "DNS rebinding/pinning can turn apparently safe hostnames into internal destinations."
        ],
    },
    {
        "source": "Tor HTTPS guidance",
        "url": "https://support.torproject.org/about-tor/security/https-encryption-and-tor/",
        "facts": [
            "Tor protects routing metadata/IP exposure but HTTPS is still required for end-to-end confidentiality on ordinary clearnet sites."
        ],
    },
    {
        "source": "Arti FAQ",
        "url": "https://arti.torproject.org/FAQs/",
        "facts": [
            "As of January 2026 Arti is ready for client/proxy use and embedding experiments.",
            "Arti still lacks some C Tor functionality and remains under active development."
        ],
    },
    {
        "source": "Arti capability/limitations page",
        "url": "https://arti.torproject.org/guides/capability-limitations/",
        "facts": [
            "The page warns some details are out of date and describes incomplete or experimental features.",
            "This conflicts in tone with the newer FAQ and therefore backend selection must be evidence-tested, not assumed."
        ],
    },
    {
        "source": "Tor Project September 2026 release cadence",
        "url": "https://blog.torproject.org/",
        "facts": [
            "Tor/Tor Browser security releases are frequent, so patch freshness is an operational security requirement rather than a one-time setup task."
        ],
    },
]

BASE_REQUEST = """JAYTEC V3 SECURITY RESEARCH ONLY.

Act as a hostile security architect reviewing a proposed future Tor specialist.
You have NO authority to implement anything. Do not call tools, write files, use
Notion, use Manus, spend credits beyond this already-authorized bounded review,
or suggest weakening JAYTEC authority.

Proposed role:
- Tor is NOT system-wide networking.
- It is a specialist/provider invoked only through Jay -> ChatGPT -> JAYTEC.
- It initially performs bounded HTTP/HTTPS text retrieval and explicit .onion
  retrieval when policy allows.
- It is intended to improve network privacy for selected research/retrieval work,
  not guarantee perfect anonymity.
- The fetch worker should have no direct clearnet egress; only the Tor daemon may
  establish Tor relay connections.
- Unknown/failure states fail closed.
- Notion is not involved.

Analyze this as if compromise or deanonymization would be unacceptable.
Distinguish:
1. real-IP or DNS leaks,
2. identity/application-layer leaks,
3. Tor-network limitations such as timing correlation,
4. SSRF/internal-network pivoting,
5. malicious exit/content risks,
6. credential/cookie/header leaks,
7. downloaded/active-content risks,
8. proxy/control-port exposure,
9. supply-chain/update risks,
10. logging/telemetry leaks,
11. denial-of-service/resource exhaustion,
12. cross-specialist/circuit linkability,
13. unsafe assumptions in choosing C Tor vs Arti,
14. fail-open/fallback hazards,
15. tests that can actually prove each security invariant.

Return a conclusion object with EXACT keys:
- verdict
- must_have_invariants
- critical_weaknesses
- attack_scenarios
- required_architecture
- mandatory_tests
- backend_selection_advice
- residual_risks_that_cannot_be_eliminated
- conditions_to_block_v3_tor
- ready_for_design_freeze

Set ready_for_design_freeze=true ONLY if the architecture can be made
meaningfully safer by explicit invariant testing and the remaining Tor limits
are honestly represented. This is not an implementation approval.

PRIMARY_SOURCE_EVIDENCE:
""" + json.dumps(EVIDENCE, ensure_ascii=False, sort_keys=True)


def _packet(task_id: str, subtask_id: str, request: str, context: dict) -> dict:
    return {
        "task_id": task_id,
        "subtask_id": subtask_id,
        "request": request,
        "intent": "Independent hostile security research for a future JAYTEC V3 Tor specialist",
        "workflow_id": "JAYTEC_V3_TOR_SECURITY_RESEARCH",
        "risk_level": "read_only_security_review",
        "specialist_plan": ["gemini"],
        "allowed_operations": ["read", "research", "analyze", "validate"],
        "expected_output": "Strict security findings and testable invariants only",
        "validation_requirements": [
            "no side effects",
            "no implementation",
            "separate anonymity claims from network privacy",
            "identify fail-open routes",
            "identify DNS/direct-egress leaks",
            "identify SSRF and redirect/rebinding hazards",
            "identify credential/log/content leaks",
            "state unfixable Tor threat-model limitations",
            "provide falsifiable tests",
        ],
        "side_effect_policy": "none",
        "idempotency_key": subtask_id.lower(),
        "deadline": "2099-01-01T00:00:00Z",
        "max_fanout": 1,
        "max_retries": 1,
        "return_schema_version": "1.0",
        "required_context": context,
        "constraints": [
            "review only",
            "do not use Notion",
            "do not use Manus",
            "do not perform engineering writes",
            "do not claim perfect anonymity",
            "do not treat Rust memory safety as proof of production security",
        ],
    }


def main() -> int:
    if not ENABLED:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_TOR_SECURITY_REVIEW",
            "status": "SKIP",
            "reason": "RUN_LIVE_GEMINI_TOR_SECURITY_REVIEW_DISABLED",
        }, sort_keys=True), flush=True)
        return 0

    if not OPENROUTER_API_KEY:
        print(json.dumps({
            "event": "JAYTEC_GEMINI_TOR_SECURITY_REVIEW",
            "status": "FAILED_CLOSED",
            "error": "OPENROUTER_API_KEY_NOT_CONFIGURED",
        }, sort_keys=True), flush=True)
        return 4

    dispatch = build_gemini_dispatch(
        openrouter_client=OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL),
        gemini_model=GEMINI_MODEL,
        gemini_timeout_s=GEMINI_TIMEOUT_S,
        circuit=CircuitBreaker(failure_threshold=1, reset_after_seconds=60),
    )

    pass1 = dict(dispatch(_packet(
        "JAYTEC-V3-TOR-SECURITY",
        "GEMINI-THREAT-RESEARCH-1",
        BASE_REQUEST,
        {"primary_source_evidence": EVIDENCE},
    )))

    pass2_request = """Adversarially review the first-pass Gemini findings below.
Try to falsify them. Flag unsupported claims, missing leak paths, hidden
fail-open behavior, unsafe operational assumptions, or tests that would pass
while the system is still vulnerable. Then produce a corrected final conclusion
using the SAME exact conclusion keys required in the original request.
Do not merely agree with pass 1.

FIRST_PASS:
""" + json.dumps(pass1, ensure_ascii=False, sort_keys=True)

    pass2 = dict(dispatch(_packet(
        "JAYTEC-V3-TOR-SECURITY",
        "GEMINI-ADVERSARIAL-REVIEW-2",
        pass2_request,
        {"primary_source_evidence": EVIDENCE, "first_pass": pass1},
    )))

    print(json.dumps({
        "event": "JAYTEC_GEMINI_TOR_SECURITY_REVIEW",
        "status": "COMPLETE_ADVISORY",
        "model": GEMINI_MODEL,
        "calls": 2,
        "pass1": pass1,
        "pass2": pass2,
        "authority": "advisory_only_chatgpt_must_audit",
        "side_effects": [],
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
