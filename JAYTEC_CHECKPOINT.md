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

## DO NOT REPEAT

- Do not reintroduce Notion fallback into JAYTEC:READ.
- Do not repeat the completed web-retrieval implementation.
- Do not rerun the one-time acceptance URL on every deploy.
- Do not create a second competing JAYTEC:READ route.
- Do not claim a fresh ChatGPT chat is directly wired to JAYTEC until that connection is actually verified.

## NEXT ACTION

Wait for Jay.
