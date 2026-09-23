# JAYTEC Single Canonical Writer V1

This control closes the normal JAYTEC code-change path around one operational editor.

## Command chain

`Jay / ROOT_OWNER → ChatGPT → WATCH → worker → WATCH → ChatGPT → CANONICAL-WRITER → canonical state/deploy`

Jay remains ultimate authority. ChatGPT is the priority operational canonical decision-maker and the only routine canonical writer identity. WATCH coordinates, delegates and independently reviews. Workers produce bounded candidate branches, pull requests and evidence only.

## Durable flow

1. A normal assignment is persisted in the existing JAYTEC job fabric.
2. WATCH delegates bounded execution to a worker seat.
3. The worker may mutate only its fenced candidate branch and returns immutable evidence.
4. WATCH independently reads the result back and records ACCEPT / REWORK / BLOCK / ESCALATE.
5. Only an assignment explicitly marked `canonical_write_intent=true` can enter the canonical writer queue.
6. WATCH ACCEPT creates a durable `PENDING_CHATGPT_APPROVAL` item. WATCH cannot approve it.
7. ChatGPT or ROOT_OWNER approval binds the exact WATCH review, candidate head SHA, expected base SHA, candidate digest and shared-state version.
8. `CHATGPT-CANONICAL-WRITER` claims one item under the global writer lease/fence. A database unique index permits only one `IN_FLIGHT` canonical write.
9. The editor performs the bounded merge/deploy externally. The queue module deliberately contains no GitHub, Render, shell, merge, deploy or workflow client.
10. Completion is accepted only after canonical readback evidence. An expired writer lease or uncertain effect is quarantined and is never blindly retried.

## Ledger

Every material queue transition is appended to `jaytec_canonical_write_ledger`. Entries form a global SHA-256 hash chain. Database triggers reject UPDATE and DELETE, making normal runtime history append-only. The writer readiness check verifies the complete chain before work is accepted.

## Recovery and duplicate safety

The WATCH-to-writer handoff is restart-safe: accepted canonical candidates are reconciled from durable WATCH reviews, while `review_id`, `idempotency_key`, and job/handoff uniqueness collapse duplicates. Shared-state drift supersedes stale approved work. Fencing rejects stale editors. Expired in-flight edits are quarantined for reconciliation rather than retried.

## Scope

This extends the existing five-seat Postgres fabric; it does not create a second job source of truth. Ordinary non-canonical worker PRs remain outside the canonical queue.
