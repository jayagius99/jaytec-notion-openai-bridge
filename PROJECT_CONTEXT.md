# JAYTEC shared project context

This bridge is intended to let Notion AI request a second opinion from an OpenAI model.

Primary working principle:
- Build real, testable engineering outputs rather than simulations or pretend integrations.
- Prefer verified evidence over confident guesses.
- Keep source material, assumptions, findings, and validation status clearly separated.
- For software work, preserve maintainability and future extensibility.
- For calibration/reverse-engineering work, identify exact binary/definition/version context before treating an address, table, checksum, protocol behavior, or patch as verified.

The Notion agent should send the specific current project/page/file context with each tool call. This file is only a stable baseline and is not a substitute for live project evidence.


JAYTEC command policy:
- JAYTEC_COMMAND_POLICY.md is authoritative for trigger/tool routing.
- Notion is not an implicit fallback. It may be used only when Jay explicitly
  authorizes Notion in the current user message for that task.
- JAYTEC:READ is Gemini + web retrieval only and must fail closed rather than
  routing through Notion or another specialist.
