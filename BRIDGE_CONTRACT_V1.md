# JAYTEC Bridge Contract v1 — staging

Status: **STAGING / NOT PRODUCTION DEPLOYED**

Authoritative workflow: `WORKFLOW_ARCHITECTURE_DECISION`  
Parent task: `JAYTEC-2026-0001`

## Request: `execute_task_packet`

Required fields:

- `packet_version` = `1.0`
- `task_id`
- `subtask_id`
- `request`
- `intent`
- `workflow_id`
- `risk_level`
- `specialist_plan` — ordered subset of `codex`, `gemini`
- `allowed_operations` — allowlisted, non-production operations only
- `expected_output`
- `validation_requirements`
- `side_effect_policy` — `none` or `staging_only`
- `idempotency_key`
- `deadline` — timezone-aware ISO-8601
- `max_fanout` — 1..2
- `max_retries` — 0..3
- `return_schema_version` = `1.0`

Optional fields: `parent_task_id`, `required_context`, `context_digests`, `known_facts`, `constraints`.

Unknown top-level fields fail closed. Context is capped at 256 KB. Expired packets are rejected before dispatch.

## Return envelope

The normalized return includes:

`execution_id`, `task_id`, `subtask_id`, `overall_status`, `answer`, `findings`, `evidence`, `confidence`, `codex_result`, `gemini_result`, `conflicts`, `unresolved_items`, `files_or_artifacts`, `architecture_changes_required`, `knowledge_writeback_proposal`, `side_effects_attempted`, `approval_required`, `retry_trace`, `worker_trace`, `timing`, `usage_summary`, `packet_hash`, `integrity`, and `return_schema_version`.

Allowed overall statuses:

`SUCCESS`, `PARTIAL_SUCCESS`, `NEEDS_VALIDATION`, `POLICY_BLOCKED`, `FAILED_CLOSED`, `INVALID_PACKET`, `TIMEOUT`, `RATE_LIMITED`.

## Controls implemented in the staging core

- strict schema validation and unknown-field rejection
- bounded specialist fan-out (`max_fanout <= 2`)
- qualified engineering-specialist contract with exact current primary `gpt-5.6-sol` (legacy TaskPacket key `codex`) plus exact Gemini research model `google/gemini-3.1-pro-preview`; no silent model substitution
- deterministic fan-in order (`codex`, then `gemini`)
- deterministic request SHA-256 hash and execution ID
- idempotent replay and conflicting-duplicate rejection
- configurable idempotency TTL / stale-cache expiry
- deadline validation and deadline-aware retry budget
- `Retry-After`-aware bounded retries
- timeout and provider-unavailable normalization
- secret/key/token redaction
- worker-output size cap
- unsafe worker requested-operation blocking
- no arbitrary tool execution
- no production write operation in the allowlist

## Current transport truth

### Engineering specialist (legacy TaskPacket key: `codex`)

TaskPacket v1 retains the historical `codex` key and `CODEX_MODEL` environment name only for wire/config compatibility. The semantic role is now the provider-neutral JAYTEC engineering specialist. The approved current primary is exact `gpt-5.6-sol`. Any route must prove exact model identity, satisfy this contract, fail closed on mismatch, and preserve the same safety/validation gates. The legacy `gpt-5.3-codex` dependency is superseded and must not block G1/V2 simply because that model or its API project is unavailable.

### Gemini

The attached Notion Worker can reach exact model `google/gemini-3.1-pro-preview`; capability checks pass. Current non-trivial `sendGeminiResearchTask` packets fail before reaching Gemini with a JSON `CONTRACT_VIOLATION` (`Unterminated string`). The worker has no verified external endpoint callable by Render. Therefore Render→existing Notion Worker transport is **UNVERIFIED / NOT ESTABLISHED**.

The staging production direction is a thin server-side Gemini adapter (or repaired externally callable worker) that keeps `OPENROUTER_API_KEY` server-side, validates the same task identity/contract, and returns the normalized envelope. No key may be placed in prompts, logs, task packets, Notion pages, Codex messages, or Gemini output.

## Activation gate

Do not merge/deploy this staging architecture as production until:

1. At least one approved engineering-specialist route (current primary: exact `gpt-5.6-sol`) passes identity, contract, no-side-effect and evidence validation.
2. Gemini non-trivial structured research packet passes deterministically, with exact model identity and contract injection.
3. Combined engineering-specialist + Gemini pilot passes task/subtask preservation, retry behavior, duplicate handling, deterministic fan-in, conflict reporting and final validation.
4. Approval behavior is measured in the real Notion UI/runtime.
5. Rollback and fallback paths are recorded.
