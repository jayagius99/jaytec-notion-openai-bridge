# JAYTEC CHECKPOINT

Status: AUTHORITATIVE CHECKPOINT FOR CURRENT JAYTEC:READ / NOTION-GATE WORK  
Saved: 2026-09-19 (Australia/Adelaide)  
Execution state: STOPPED / NO ACTIVE REPAIR

## VERIFIED COMPLETED

- JAYTEC:READ backend is implemented and live.
- JAYTEC:READ is hard-locked to Gemini + JAYTEC-controlled web retrieval.
- Notion is not a JAYTEC:READ fallback.
- Codex and other agents are not JAYTEC:READ fallbacks.
- JAYTEC:READ fails closed when the exact requested source cannot be verified.
- Web retrieval uses a bounded engine sequence:
  1. openrouter
  2. exa
  3. parallel
- The exact shared ChatGPT test URL was recovered successfully in production.
- Live acceptance evidence:
  - title: Gemini Bridge Repair
  - VERIFIED: true
  - source-specific findings: 5
  - unresolved repair state recovered: yes
  - Gemini used: true
  - web retrieval used: true
  - Notion used: false
  - other agents used: []
- The one-time production startup acceptance test was disarmed after success.
- Production deployment is live on the hardened build.
- Notion ChatGPT app permission was changed from Allow all actions to Always ask.
- The Notion permission change was read back and verified.

## AUTHORITATIVE REFERENCES

- Repository: jayagius99/jaytec-notion-openai-bridge
- PR #14: Harden JAYTEC:READ and add Gemini web retrieval
- PR #15: Add live acceptance probe for JAYTEC:READ
- PR #16: Add bounded JAYTEC:READ fetch-engine fallback
- Production commit: 55c5e8bf573340adc6e1d25c08c88b2dd66bcb8b
- Successful acceptance deployment: dep-daml0lrncjis73dntd8g
- Final clean deployment after disarming one-time self-test: dep-daml1oqd0e5s73frrvt0

## IMPORTANT POLICY

Notion must not perform, route, retrieve, research, execute, repair, analyze, or act as fallback for a JAYTEC task unless Jay explicitly authorizes Notion in the current user message.

Historical permission, standing full-authority instructions, prior-chat approval, convenience routing, or old fallback behavior do not count as permission for a new task.

## CURRENT LIMITATION / NEXT WORK

A fresh ordinary ChatGPT chat does not currently have a registered JAYTEC plugin/app that directly exposes the jaytec_read MCP tool.

Therefore:
- the JAYTEC:READ machine/backend is working;
- Notion must not be used silently;
- seamless trigger-to-JAYTEC invocation from every fresh ChatGPT chat still requires a direct JAYTEC-to-ChatGPT connector/app/plugin path or another explicitly approved integration route.

Do not begin that connector work unless Jay explicitly resumes/authorizes it.

## FUTURE V3 RESEARCH ITEM — CONTROLLED TOR NETWORK ROUTE

Recorded: 2026-09-20 (Australia/Adelaide)  
Status: SECURITY ARCHITECTURE DECISION COMPLETE / IMPLEMENTATION NOT AUTHORIZED

Jay requested that Tor capability be researched for JAYTEC V3 and preserved so it is not forgotten during future V3 design or implementation.

Research direction:
- Treat Tor as a bounded, provider-neutral JAYTEC network route/adapter, NOT as a machine-wide default proxy.
- Normal outbound routing remains unchanged unless a request is explicitly authorized or policy-routed through Tor.
- ChatGPT/JAYTEC remains coordinator and decision authority. Notion remains transport/state only and must not independently invoke, route, research, retry, or decide Tor use.
- Specialists/workers must not silently enable Tor or bypass JAYTEC authority, approval, cost, security, audit, verification, anti-duplication, or checkpoint gates.
- Prefer an isolated gateway/fetcher whose only destination path is a local Tor client; the fetcher must have no direct Internet egress path. The Tor process itself is the only component permitted to establish Tor relay connections.
- Resolve destination hostnames through Tor rather than local DNS; add explicit DNS/direct-egress leak tests and fail closed on leak evidence.
- Bind proxy/control interfaces to localhost or isolated local IPC. Do not expose an open Tor proxy to the LAN or public Internet.
- Use strong per-request/session stream isolation where supported so unrelated JAYTEC requests do not needlessly share circuits.
- Restrict the initial machine interface to bounded HTTP/HTTPS text retrieval. No torrenting, arbitrary generic proxying, executable launch, unrestricted browser automation, or automatic opening of downloaded documents.
- Block loopback, private-network, link-local, cloud-metadata and other SSRF-sensitive destinations from the retrieval surface.
- Enforce request size, response size, timeout, concurrency and rate limits.
- Require HTTPS for ordinary clearnet destinations where available; Tor protects the route to the exit, not plaintext traffic from an exit relay to a non-HTTPS destination.
- .onion access, if enabled, must be explicit and policy-controlled. A Tor route is not permission to bypass authentication, paywalls, access controls, CAPTCHAs, legal restrictions or JAYTEC safety rules.
- Credentialed or identity-bearing sessions are OFF by default because logging into an account or submitting identifying information can defeat the intended anonymity/privacy property.
- Keep audit evidence without unnecessarily recording sensitive URL query strings, credentials, cookies or secrets. Logs should identify the initiating JAYTEC request, route choice, policy result, timing, destination class, status and bounded transfer metadata.
- Use a separate Tor Browser/human-interactive lane for true browser activity rather than pointing an ordinary browser at a Tor SOCKS port.

Backend decision is RESOLVED at architecture level:
- V3 uses a backend-neutral TorEngine contract.
- C Tor and the current supported Arti release are candidates, not hard-coded architectural dependencies.
- Both must be tested against the same release-blocking leak/isolation/failure harness on the exact versions proposed for V3.
- Only an engine that passes every mandatory invariant may ship.
- The Tor engine remains in a separate compartment from the untrusted fetch worker so the kernel/network leak boundary does not depend on proxy correctness or engine choice.
- Full security decision: JAYTEC_V3_TOR_SECURITY_DECISION.md.

Mandatory pre-implementation gate:
1. explicit V3 implementation authorization from Jay;
2. threat model and abuse/scope review;
3. DNS-leak and direct-egress tests;
4. SSRF/private-network rejection tests;
5. circuit/session isolation tests;
6. credential/cookie/header leakage tests;
7. content-type/download containment tests;
8. audit-log privacy review;
9. fail-closed behavior when Tor is unavailable;
10. confirmation that Tor integration does not weaken existing JAYTEC authority, Notion, cost, security, verification, anti-duplication, or checkpoint rules.

Official research basis captured for later design review:
- Tor Project: Tor Browser best practices — https://support.torproject.org/tor-browser/security/using-tb-safely/
- Tor Project: SOCKS/DNS leak testing — https://support.torproject.org/little-t-tor/troubleshooting/check-for-leaks/
- Tor Specifications: stream isolation and circuit sharing — https://spec.torproject.org/path-spec/stream-isolation.html
- Tor Specifications: SOCKS extensions — https://spec.torproject.org/socks-extensions
- Arti documentation — https://arti.torproject.org/
- Arti capability/status documentation — https://arti.torproject.org/FAQs/

Security-hardening review completed 2026-09-20:
- GPT-5.6 Sol independent architecture/security audit completed.
- JAYTEC Gemini hostile review pass 1 completed.
- JAYTEC Gemini adversarial pass 2 falsified the premature readiness result and identified DNS-rebinding and retry/fail-open gaps.
- JAYTEC Gemini backend challenge pass 3 completed after current Arti/Tor stream-isolation evidence was added.
- ChatGPT/JAYTEC final audit resolved the architecture: network compartment is the leak-prevention boundary; Tor is the privacy transport inside it.
- Architecture decision: GO.
- Implementation/production decision: NO-GO until the complete acceptance harness is implemented and passes.
- Authoritative detailed decision: JAYTEC_V3_TOR_SECURITY_DECISION.md.

This entry is continuity only. It does not resume V2, alter the current JAYTEC:READ route, install Tor, create a service, expose a proxy, spend money, or authorize implementation.

## DO NOT REPEAT

- Do not reintroduce Notion fallback into JAYTEC:READ.
- Do not repeat the completed web-retrieval implementation.
- Do not rerun the one-time acceptance URL on every deploy.
- Do not create a second competing JAYTEC:READ route.
- Do not claim a fresh ChatGPT chat is directly wired to JAYTEC until that connection is actually verified.

## NEXT ACTION

Wait for Jay.
