# JAYTEC Controlled Execution Bridge

Historical repository name: `jaytec-notion-openai-bridge`.

The current architecture is **not** a free-form Notion-to-OpenAI bridge. The production surface is a JAYTEC-controlled execution bridge with bounded task packets, exact provider/model policy, durable idempotency, fail-closed cost/provider behavior, and explicit HTTP trust configuration.

## Current authority boundary

- Jay supplies owner authority.
- ChatGPT coordinates on Jay's behalf.
- JAYTEC is the control plane.
- Specialists receive bounded task packets.
- The Notion Agent is optional strict pass-through transport only and is **not** the authenticated production execution principal.

Production MCP authentication must identify the client as `jaytec-control-plane`. A Notion Agent identity is not production-ready.

## Production tools

The governed execution surface is:
- `orchestration_status()`
- `execute_task_packet(packet_json)`
- `bridge_status()`

Legacy free-form `ask_openai`, `review_notion_answer`, and free-form `collaborate` are disabled in production and may exist only behind an explicit staging-only compatibility switch.

## Readiness semantics

A true bridge readiness result means **this bridge instance only** has satisfied its own prerequisites. It never means the whole JAYTEC/GOD Mode system is ready for production or activation.

## Required production controls

- durable Postgres idempotency;
- exact approved engineering and research model identities;
- explicit JAYTEC control-plane MCP auth identity;
- explicit Host/Origin allowlist;
- pinned provider endpoints;
- no free-form legacy provider path;
- provider credentials remain server-side.

ROOT_OWNER, hardware-key, production topology, signer custody, rollback, hostile-review and final owner-activation gates remain separate and must all pass independently.


## Pre-activation execution boundary

Production provider dispatch is deliberately fail-closed until the V2 dispatch-authority contract is implemented and verified end-to-end. The bridge may be tested and staged, but a production TaskPacket cannot reach a provider merely because the bridge has credentials or receives a valid MCP request. V2 cost/approval/provider authority must become the source of a packet-bound dispatch authorization before this gate can be changed.
