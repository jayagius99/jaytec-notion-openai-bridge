# JAYTEC `execute_task_packet` v1 — staging test report

Date: 2026-09-15  
Task: `JAYTEC-2026-0001`  
Workflow: `WORKFLOW_ARCHITECTURE_DECISION`  
Status: **24/24 local unit tests PASS; source not production-deployed**

Command used: `python -m unittest -v test_orchestration.py`

Covered tests:

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

Not yet production-verified:

- real Codex dispatch through current Notion Custom Agent (currently blocked at tool execution/approval layer)
- real non-trivial Gemini research dispatch (current worker JSON serialization defect)
- live combined fan-out/fan-in
- durable idempotency store across Render process restarts (current staging core uses in-memory registry; production requires persistent storage)
- circuit breaker state across processes
- real Notion visible approval prompt count / Always Ask vs Run Automatically vs Always Allow comparison
- production deployment, rollback, and agent publication
