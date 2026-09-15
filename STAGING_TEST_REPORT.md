# JAYTEC `execute_task_packet` v1 — staging test report

Date: 2026-09-15  
Task: `JAYTEC-2026-0001`  
Workflow: `WORKFLOW_ARCHITECTURE_DECISION`  
Status: **27/27 staging unit tests PASS in GitHub Actions; source not production-deployed**

Command used by CI: `python -m unittest discover -v`

Covered orchestration tests:

1. valid packet
2. malformed JSON
3. unknown top-level fields fail closed
4. missing TASK_ID
5. missing deadline
6. unknown specialist
7. oversized context
8. unauthorized operation / side effect request
9. expired deadline
10. successful fan-in and deterministic specialist ordering
11. one specialist failure → partial success
12. both specialists fail → failed closed
13. conflicting specialist conclusions → needs validation
14. idempotent replay does not re-dispatch
15. stale idempotency cache expires and re-executes
16. conflicting duplicate fails closed
17. model mismatch fails closed
18. malicious/unauthorized worker operation → policy blocked
19. Codex timeout
20. Gemini timeout
21. rate limit with Retry-After then success
22. rate-limit retry budget exhausted
23. provider unavailable retry path fails closed after budget
24. credential/Authorization redaction

Circuit-breaker tests:

25. opens after bounded failure threshold and blocks further dispatch
26. successful dispatch resets failure state
27. reset window re-allows a half-open probe

Latest verified GitHub Actions run for staging head `4562f7f645ca2de7350638143356120663a960f2`: **SUCCESS**. The workflow successfully ran full unit-test discovery and Python compile checks.

Additional staging controls now present:

- dedicated per-specialist circuit breakers
- Gemini OpenRouter provider sorting explicitly set to `price` while preserving exact model `google/gemini-3.1-pro-preview`
- provider fallbacks remain enabled
- staging server uses the same proven `StaticTokenVerifier` authentication pattern as production

Not yet production-verified:

- real Codex dispatch through current Notion Custom Agent (currently blocked at the MCP approval/tool-execution layer)
- real non-trivial Gemini research dispatch through the existing Notion Worker (current worker JSON serialization defect)
- direct server-side Gemini staging dispatch with the OpenRouter key configured
- live combined Codex + Gemini fan-out/fan-in
- durable idempotency/circuit state across Render process restarts (current staging uses process memory; production requires persistent storage if restart-safe guarantees are required)
- real Notion visible approval prompt count after the MCP connection is changed to `Always allow` or `Run automatically`
- separate staging deployment (the automated Render service-creation action was blocked by the connector safety gate)
- production merge/deployment, rollback test, and agent publication
