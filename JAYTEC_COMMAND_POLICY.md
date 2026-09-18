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
