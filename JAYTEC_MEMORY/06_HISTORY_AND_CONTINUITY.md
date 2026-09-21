# JAYTEC Durable Memory — History and Continuity

This file records durable project history useful for future reasoning. It avoids
embedding ephemeral credentials, secret values or stale live state.

## Historical architecture

JAYTEC evolved from a Notion-centric gateway/bridge model into a broader control
plane where ChatGPT coordinates specialists and durable infrastructure. Notion
remains a possible bus/state surface rather than execution authority.

Historically:
- Gemini served research;
- Codex served engineering;
- the engineering role was later migrated to SOL;
- Nemo/Nemotron was introduced as a zero-cost-capable secondary engineering
  specialist;
- DeepSeek was introduced as an independent adversarial/security reviewer;
- Manus became the bounded asynchronous automation/task carrier;
- WATCH became the unattended supervisor/recovery/traffic-control lane.

Legacy names may remain in code for wire compatibility. Semantic role matters
more than stale variable names. Example: historical "codex" wire keys can map to
the current engineering role, but exact model identity must still be checked.

## Reliability lessons

Repeated hardening priorities include:
- no duplicate workers;
- exact fences;
- preserved durable checkpoints;
- explicit provider/model identity;
- provider failure classified without leaking secrets;
- bounded internal handoffs;
- owner pauses not auto-resumed;
- no claim of completion from heartbeat alone;
- CI/runtime/proving evidence before promotion.

## Continuity

When resuming:
- inspect exact branch/PR/commit/deploy/task state;
- preserve already completed work;
- do not recreate a prior lane merely because a chat changed;
- reconcile against current durable state;
- record authoritative handoff references;
- keep candidate work isolated until accepted.
