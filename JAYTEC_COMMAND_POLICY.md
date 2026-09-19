# JAYTEC Command Execution Policy

## Global Notion hard gate

Notion must not perform, route, retrieve, research, execute, repair, analyze, or
act as a fallback for a JAYTEC task unless Jay explicitly authorizes Notion in
the current user message.

Previous approvals, standing "full authority" instructions, old chat context,
agent defaults, convenience fallbacks, or historical JAYTEC behavior do not
count as current authorization.

If a workflow cannot complete without Notion and the current message did not
explicitly authorize Notion, the workflow must fail closed and report the
blocker. It must not silently route through Notion.

Notion may still be used when Jay explicitly requests it for that task.

## JAYTEC:READ

JAYTEC:READ is hard-locked to:

1. JAYTEC-controlled/public web retrieval.
2. Gemini analysis.
3. ChatGPT presentation/verification when ChatGPT is the caller.

The canonical machine route is the jaytec_read MCP tool or a task packet with
workflow_id=JAYTEC_READ.

Allowed specialist plan: Gemini only.

Allowed operations:
- read
- research
- analyze
- validate
- web_fetch

Required operations:
- read
- validate
- web_fetch

Forbidden:
- Notion fallback
- Codex fallback
- other-agent fallback
- engineering writes
- staging writes
- hidden side effects

If the exact requested page cannot be retrieved and source-specific evidence
cannot be produced, return FAILED_CLOSED / VERIFIED=false.

A successful READ requires a complete conclusion.READ_REPORT and a
ROUTE_AUDIT showing:
- web_retrieval_used: true
- gemini_used: true
- notion_used: false
- other_agents_used: []

## Explicit override

An override must appear in Jay's current message, for example:

- JAYTEC:READ — USE NOTION
- Ask Notion to read this
- Use Notion for this task

The override applies only to that current task and does not become a standing
permission.


## Manus profile hard gate

JAYTEC must treat Manus as **Lite-only by default**.

Rules:
- Default Manus profile: `lite`.
- Manus 1.6 / standard and Manus Max are paid-profile routes and are blocked unless Jay explicitly requests that paid profile for the current task.
- Any Manus route must expose an explicit profile selector before dispatch. If the route cannot explicitly pin Lite, return `BLOCKED_ROUTE / MANUS_PROFILE_SELECTOR_UNAVAILABLE` and do not call Manus.
- Manus participation is not verified unless the observed profile can be checked against the requested profile. Missing profile identity fails closed.
- Never automatically fall back from Lite to 1.6/standard/Max because of quality, task complexity, timeout, availability, or retry conditions.
- A paid-profile override is one-task-only and does not change this standing default.

Executable enforcement lives in `manus_policy.py` and must be reused by any future Manus adapter before it is allowed into production routing.


## Temporary ChatGPT + JAYTEC collaboration directive

Effective until Jay explicitly activates the new GOD Project / Executive Control Project:

- For every substantive task Jay assigns to ChatGPT, ChatGPT must involve JAYTEC as a supporting system where a real JAYTEC route/capability exists.
- JAYTEC may be used for retrieval, research, verification, routing, state inspection, diagnostics, specialist consultation, execution support, or cross-checking as appropriate to the task.
- ChatGPT remains responsible for verifying JAYTEC's output and presenting the final result to Jay.
- This directive does not override existing hard gates, safety/security rules, Notion restrictions, Manus profile restrictions, cost controls, or tool permission boundaries.
- ChatGPT must not claim JAYTEC participated when no actual JAYTEC path/tool/runtime was used.
- If the relevant JAYTEC route is unavailable, ChatGPT should preserve the objective, use legitimate available alternatives where appropriate, and disclose that JAYTEC participation could not be obtained for that task.
- Once the new GOD Project / Executive Control Project is activated, this temporary directive is superseded by that Project's governing authority model and instructions.
