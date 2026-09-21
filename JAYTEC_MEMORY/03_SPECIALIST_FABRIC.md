# JAYTEC Durable Memory — Specialist Fabric

## Roles

### SOL
Primary engineering/reasoning specialist. Expected exact model:
gpt-5.6-sol. Receives broad sanitized JAYTEC memory plus minimum necessary live
state. SOL may reason, design, implement bounded engineering work and challenge
assumptions, but may not self-approve promotion, spend, activate, alter owner
boundaries or invent provider fallbacks.

### DeepSeek
Independent adversarial/security reviewer. Current JAYTEC target model:
deepseek/deepseek-v4-flash-0731:free. Review-only by default. Focus on bypasses,
replay/concurrency, privilege confusion, stale state, rollback, recovery,
credential exposure and false completion claims.

### Nemo / Nemotron
Bounded secondary engineering/reasoning reviewer. Current target model:
nvidia/nemotron-3-ultra-550b-a55b:free. No silent fallback. No direct mutation
authority.

### Manus
Asynchronous automation/task-execution specialist. Manus works on its side under
JAYTEC task constraints. Manus Home is the JAYTEC-owned Manus-specific layer of
workflows, prompts, procedures and automation assets exposed through supported
interfaces.

### Gemini
May exist in explicit/legacy research routes elsewhere in JAYTEC, but is not in
the default WATCH/Manus assistance trio unless the current owner/controller
explicitly routes a task to it.

## WATCH/Manus assistance trio

Default unattended model help:
- sol
- deepseek
- nemo

Manus never gets provider credentials for these routes. It returns a
SPECIALIST_REQUEST with REQUEST_ONLY_NO_SELF_DISPATCH authority. JAYTEC validates
the request, invokes an allow-listed specialist, captures bounded results and
returns them to the same Manus task/fence.

## Request requirements

A valid request is:
- versioned;
- correlated to parent task;
- digest-checked;
- minimum-context;
- allow-listed;
- secret-free;
- non-mutating;
- no spend/merge/deploy/root/activation authority.

## Result requirements

A result is:
- correlated to request id;
- bounded in size;
- integrity-digested;
- secret-filtered;
- RESULTS_ONLY / no authority expansion;
- returned to the same Manus task.

## Failure

Provider/model mismatch, unavailable door, malformed output, cost blocker or
secret/provenance violation fails closed. JAYTEC does not silently substitute a
different model/provider.
