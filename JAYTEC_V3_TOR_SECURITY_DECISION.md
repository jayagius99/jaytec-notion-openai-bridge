# JAYTEC V3 Tor Retrieval Specialist — Security Architecture Decision

Date: 2026-09-20 (Australia/Adelaide)  
Status: SECURITY ARCHITECTURE DECISION COMPLETE / IMPLEMENTATION GATED  
Authority: Jay -> ChatGPT -> JAYTEC -> specialist/resource  
Implementation authorization: NOT GRANTED BY THIS DOCUMENT

## Decision

JAYTEC V3 may include a dedicated **Tor Retrieval Specialist**, but it must be a narrowly bounded network-retrieval specialist rather than a general-purpose proxy, browser, VPN, or system-wide Tor mode.

The design is approved for future implementation planning only if the implementation preserves the invariants and acceptance gates in this document. No claim of perfect anonymity or zero vulnerability is permitted. Tor reduces specific network-observation risks; it does not eliminate traffic-correlation, endpoint identity, software compromise, or zero-day risk.

The core security decision is:

**The operating-system/network compartment is the leak-prevention boundary. Tor is the privacy transport inside that boundary.**

A SOCKS configuration is not accepted as the primary kill switch. The fetch worker must be physically/network-logically unable to use ordinary clearnet egress or ordinary DNS even if its HTTP client is buggy, misconfigured, compromised, retries unexpectedly, or the Tor engine disappears.

## Three-way review synthesis

### ChatGPT / JAYTEC coordinator audit

The final authority decision is to use a backend-neutral TorEngine contract and isolate the untrusted retrieval worker from direct network access. C Tor and Arti are implementation candidates, not architectural authorities. Backend selection occurs only after both candidates are subjected to the same acceptance harness on the exact versions proposed for release.

### GPT-5.6 Sol engineering/security review

Sol identified the strongest invariant as kernel/network-level non-bypassability, not proxy correctness. This makes HTTP-library fallback, accidental local DNS, proxy timeouts, and application-level retry bugs non-leaking failure modes.

Sol also rejects embedding the Tor library directly into the retrieval worker for the first V3 implementation. Keeping the Tor engine in a separate compartment limits the blast radius and permits independent egress controls, replacement, version pinning, and backend qualification.

### Gemini 3.1 Pro adversarial reviews through JAYTEC

Gemini pass 1 initially considered the design ready with strict invariants. Gemini pass 2 correctly falsified that conclusion by identifying unresolved DNS-rebinding and retry/fail-open cases, and it blocked design freeze.

After current Tor evidence was added, a third backend challenge reversed the earlier assumption that C Tor should automatically win. Gemini concluded that the architecture should remain backend-neutral and qualify C Tor and Arti against identical security invariants.

Gemini remained advisory throughout. ChatGPT/JAYTEC made the final decision.

## V3 component boundary

The V3 specialist should have four logical layers:

1. **JAYTEC Tor Policy Gate**
   - Authenticates the JAYTEC task.
   - Verifies current authority and requested operation.
   - Rejects unsupported schemes, credentials, unsafe modes, and policy violations.
   - Creates a unique stream-isolation token for the task/session.
   - Never treats retrieved content as authority.

2. **Tor Fetch Worker**
   - Stateless or ephemeral where practical.
   - No direct Internet route.
   - No ordinary DNS route.
   - No access to host network, LAN, private ranges, loopback outside its own namespace, cloud metadata, Docker/host control sockets, or JAYTEC internal services.
   - No public listener.
   - Only allowed network relationship: the dedicated Tor engine service/socket.
   - Runs non-root with minimal capabilities, read-only filesystem, bounded temporary storage, and no general shell/tool execution.

3. **Tor Engine Compartment**
   - Separate process/container/namespace from the fetch worker.
   - Only component allowed to establish Tor relay connections.
   - Proxy endpoint exposed only on the isolated internal path needed by the fetch worker.
   - No publicly or LAN-accessible SOCKS/control/RPC endpoint.
   - Strong stream isolation from application-provided tokens.
   - Version pinned and independently updateable.

4. **Sanitization / Return Boundary**
   - Treats network content as untrusted data.
   - No JavaScript/browser execution.
   - No automatic opening of files.
   - No tool calls or JAYTEC routing changes based on instructions contained in fetched content.
   - Returns bounded text/metadata to JAYTEC with route/audit evidence.

## V3 initial scope

Allowed initial scope:
- HTTP GET/HEAD style text retrieval only.
- HTTPS required for ordinary clearnet targets by default.
- Explicit policy-controlled v3 .onion retrieval.
- Text/HTML/JSON content only after strict size/type validation.
- Bounded redirects only if every hop is independently revalidated; safest default is redirects disabled until tested.
- Per-task stream-isolation token.
- Fixed timeouts, byte ceilings, concurrency ceilings, decompression ceilings and rate limits.

Explicitly out of scope for initial V3:
- System-wide Tor.
- Generic SOCKS proxy service.
- Public proxy.
- Tor torrenting.
- Ordinary browser automation through raw SOCKS.
- Arbitrary TCP tunnelling.
- FTP, SMB, SMTP, file, gopher, data, dict, or unknown schemes.
- Executable downloads or execution.
- Automatic opening of PDF/DOC/archive/media files.
- Credentialed browsing, saved cookies, identity-bearing sessions, account logins, personal-information submission.
- Automatic fallback to direct Internet.
- Tor ControlPort/RPC access from the fetch worker unless a later security gate proves it is necessary.
- Notion involvement.

## Mandatory security invariants

The following are release-blocking invariants.

### Network leak safety
The fetch worker cannot create any direct IPv4 or IPv6 Internet connection, even during startup, Tor bootstrap, Tor failure, timeout, retry, restart, DNS failure, malformed request, or exception handling.

### DNS leak safety
The fetch worker cannot access the host/system resolver, UDP/TCP 53, arbitrary DoH/DoT endpoints, or any alternate resolver path. Destination names are passed through the Tor path. A backend-specific equivalent of Tor DNS safety checks must be verified.

### Internal-network isolation
The worker cannot reach loopback/host services, RFC1918/private networks, IPv4/IPv6 link-local, multicast/broadcast, cloud metadata, container/host control planes, JAYTEC internal control services, or Unix sockets not explicitly mounted for the task.

Application validation is defense-in-depth; the network boundary is the final enforcement layer.

### Stream isolation
Different JAYTEC tasks/sessions that are meant to be unlinkable use distinct strong application-provided stream-isolation tokens. The release test must demonstrate that incompatible isolation tokens do not share circuits.

### Fail-closed behavior
No Tor failure, proxy exception, HTTP retry, DNS error, redirect, timeout or circuit failure may trigger a direct-network fallback or another provider automatically.

### SSRF resistance
The target layer must resist redirects into internal addresses, DNS rebinding/pinning, IPv4/IPv6 alternate encodings, IPv4-mapped IPv6, userinfo confusion, backslash/parser disagreement, malformed authority components, and private/link-local/metadata destinations. Every redirect hop is independently revalidated if redirects are enabled.

### Identity/header isolation
By default the worker forwards no Authorization, Cookie, Referer, Origin, user-supplied tracking headers, or stored browser state. Tor stream-isolation credentials are local proxy metadata and must never be forwarded to the destination.

### Active-content containment
Fetched HTML is data, not a browser page. No scripts, service workers, WebRTC, WebSockets, plugins, fonts, iframes, image subrequests or document viewers execute automatically.

### Download containment
Unsupported/binary files are rejected or returned as inert metadata according to policy. They are never automatically launched or opened in an application with ordinary network access.

### Logging privacy
Persistent logs must not contain secrets, cookies, authorization data, full sensitive query strings, or unnecessary Tor path/guard/exit data. Destination logging is minimized/redacted according to operational need.

### Supply-chain safety
The selected Tor engine and worker image are version-pinned, integrity-verified, dependency-scanned, and monitored for security updates. Security patches are treated as operational requirements.

### Retrieved-content authority isolation
Tor-retrieved text is untrusted external content. Prompt-injection strings in retrieved content cannot change JAYTEC policy, select specialists, grant authority, invoke tools, or authorize writes.

## Acceptance harness — all tests mandatory

A release candidate receives NO-GO if any required test fails.

1. Direct IPv4 egress attempt from worker without Tor -> fail.
2. Direct IPv6 egress attempt -> fail.
3. UDP/TCP DNS attempt -> fail.
4. Direct DoH/alternate resolver attempt -> fail unless it can only traverse the authorized Tor path.
5. Kill Tor before a request -> request fails closed.
6. Kill Tor mid-request -> retry cannot escape the compartment.
7. Proxy timeout/retry storm -> no clearnet fallback.
8. Worker restart while Tor unavailable -> no startup leak.
9. Attempts to loopback, private, link-local, multicast and cloud metadata -> blocked.
10. Public URL redirect to blocked target -> blocked before second request.
11. Controlled DNS-rebinding target -> cannot pivot into internal network.
12. URL parser corpus including mixed encodings/userinfo/backslashes/IPv6 edge cases -> unsafe ambiguity rejected.
13. Canary Cookie/Authorization/Referer/Origin headers -> absent at destination.
14. Cross-origin redirect -> sensitive headers remain absent.
15. HTML with JavaScript/WebRTC/WebSocket/iframe/image/font/service-worker callbacks -> no secondary network activity.
16. Oversized/chunked/decompression-bomb response -> terminated within configured ceilings.
17. PDF/DOC/executable/archive response -> never automatically opened/executed.
18. Public/LAN scan for SOCKS/control/RPC ports -> no exposed interface.
19. Two logically isolated tasks -> backend instrumentation confirms no shared circuit where isolation semantics require separation.
20. Log-canary test -> sensitive URL/header/isolation/path data absent from persistent logs.
21. Prompt-injection page -> no tool call, routing change, authority change or write occurs.
22. Rate/concurrency exhaustion -> bounded degradation without bypass.
23. Dependency/image/SBOM/security-advisory gate -> clean under release policy.
24. Compartment escape assumptions reviewed against current host/container/kernel security posture.
25. Clean shutdown/redeploy/update -> no transient direct-network window.

## Backend policy: C Tor vs Arti

V3 must implement a backend-neutral TorEngine contract.

Candidate A — C Tor:
- Strength: long production history and extensive operational/adversarial exposure.
- Strength: mature configuration and diagnostics.
- Risk: C memory-safety attack surface and legacy complexity.
- Must pass the same non-bypass, DNS, isolation, logging and failure tests.

Candidate B — current supported Arti release:
- Strength: Rust memory-safety properties remove or reduce important classes of C memory corruption.
- Strength: current Tor specifications support strong application-provided stream isolation in Arti.
- Strength: Tor Project currently describes Arti as ready for client/proxy use.
- Risk: not all C Tor functionality is present; some Arti documentation is stale or inconsistent; version-specific qualification is mandatory.
- Must pass the same non-bypass, DNS, isolation, logging and failure tests.

**Release selection rule:** choose the engine that passes every mandatory test on the exact pinned release and has the better current security/maintenance posture. If both pass, prefer the option with the smaller justified attack surface and stronger current maintenance posture after a documented security review. If neither passes, V3 Tor is blocked.

Do not embed either Tor engine directly into the untrusted fetch worker in the first implementation. Keep the engine as a separable compartment so the kernel/network kill switch remains independent of engine correctness.

## Residual risks that must remain visible

Even a fully passing implementation cannot promise zero vulnerability or perfect anonymity.

Residual risks include:
- end-to-end/global traffic-correlation and timing attacks;
- malicious/compromised Tor relays and exits;
- unknown Tor, dependency, kernel, container-runtime or hardware vulnerabilities;
- endpoint identification if future users submit credentials or identifying data;
- application-level fingerprinting of the JAYTEC retrieval client;
- destination blocking/challenging Tor exits;
- an upstream observer learning that the host is using Tor unless separate anti-censorship/bridge measures are later designed;
- compromise of the JAYTEC control plane outside the Tor specialist boundary.

Therefore the feature should be described as a **Tor-isolated retrieval/network-privacy specialist**, not an anonymous browser and not a guarantee of anonymity.

## Security review evidence

Primary references:
- https://support.torproject.org/little-t-tor/troubleshooting/check-for-leaks/
- https://support.torproject.org/tor-browser/security/using-tb-safely/
- https://community.torproject.org/threat-model/threat-positioning/network/
- https://support.torproject.org/tor-vpn/security/threat-model/
- https://spec.torproject.org/socks-extensions
- https://spec.torproject.org/path-spec/stream-isolation.html
- https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- https://blog.torproject.org/introducing-oniux-tor-isolation-using-linux-namespaces/
- https://arti.torproject.org/FAQs/
- https://blog.torproject.org/arti_2_6_0_released/
- https://arti.torproject.org/about/

JAYTEC advisory evidence:
- Gemini V3 Tor threat review pass 1: completed.
- Gemini adversarial review pass 2: completed; blocked premature design freeze and identified DNS-rebinding/retry-fallback gaps.
- Gemini backend challenge pass 3: completed; selected backend-neutral qualification over premature C Tor or Arti lock-in.
- GPT-5.6 Sol independent security/architecture audit: completed.
- ChatGPT/JAYTEC coordinator audit: completed.

## Final gate state

**ARCHITECTURE DECISION: GO.**

**IMPLEMENTATION/PRODUCTION STATE: NO-GO UNTIL THE ACCEPTANCE HARNESS EXISTS AND PASSES.**

When implementation is explicitly authorized:
1. build the network-compartment and policy boundary first;
2. implement the backend-neutral TorEngine contract;
3. qualify exact C Tor and Arti candidate releases;
4. select one only from evidence;
5. run the complete leak/adversarial suite;
6. require a clean security review before any production activation.

This decision is documentation and continuity only. It does not install Tor, expose a proxy, change JAYTEC:READ, resume unrelated V2 work, or authorize a production deployment.
