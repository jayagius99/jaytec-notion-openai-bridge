# FORGE COGNITIVE CONTINUITY V1

Status: PRE-GENESIS INFRASTRUCTURE. This does not activate Forge.

## Purpose

Forge's long-lived executive continuity must not depend on one ChatGPT window,
one model invocation, or one provider session. Individual reasoning calls are
disposable. Canonical Forge executive state is durable JAYTEC state.

This is an engineering continuity model. It does not claim biological,
phenomenal, or human-like consciousness.

## Core loop

1. Receive a durable signal or discover an actionable goal.
2. Atomically claim one cognition cycle with a lease + fencing token.
3. Build a compact working packet from:
   - life goal and constitutional invariants;
   - current working memory;
   - selected goal;
   - relevant recent events/signals;
   - digests/references to larger world/capability state.
4. Pick the smallest reasoning tier that can safely do the work:
   - REFLEX: deterministic/no-model path when possible;
   - FAST: simple bounded reasoning;
   - STANDARD: normal planning/reasoning;
   - DEEP: high-complexity/high-uncertainty/high-impact work.
5. Delegate independent work in bounded parallel batches only when explicitly
   marked parallel-safe.
6. Commit result with the same fencing token + expected state version.
7. Persist telemetry and new state.
8. If another goal is actionable, continue immediately.
9. If no goal is actionable, perform one strategic reflection. If that still
   yields no active goal, sleep in WAITING_FOR_DEPENDENCY until a new signal
   wakes Forge.

## Performance law

WATCH liveness cadence is not cognition cadence.

The five-minute WATCH loop proves continuity/liveness and detects recovery
conditions. It must not force an LLM call every five minutes.

Cognition is event-driven:
- owner/human input can wake immediately;
- specialist/tool results can wake immediately;
- fault/security signals can preempt lower-priority goals;
- completed work immediately returns to the goal hierarchy;
- idle Forge sleeps without losing identity/state.

## Context efficiency

The cognition packet does not replay the full conversation history or full
world model every cycle. Stable state remains in durable storage and is
referenced by digest/version. The worker receives compact working memory and
recent deltas/signals, retrieving larger state only when required.

## Speed and cost controls

- deterministic routing and verification should remain outside model calls;
- REFLEX should be preferred when a model adds no value;
- DEEP reasoning is reserved for genuinely hard/high-impact work;
- repeated empty reflection loops are forbidden;
- independent work may run in bounded parallel;
- duplicate/stale workers cannot commit;
- telemetry is stored with committed cycles so latency/token/tool bottlenecks
  can be tuned from evidence.

## Safety and authority

Cognition never creates authority merely by reasoning about it.

Forge operational state cannot alter:
- ROOT_OWNER cryptographic authority;
- owner/operator pause semantics;
- provider/spend authority;
- specialist identity constraints;
- physical ROOT continuity requirements.

Urgent owner/safety signals may preempt normal goals. ROOT_OWNER remains the
physically anchored continuity/override authority while Forge remains the broad
operational executive inside its legitimate capability envelope.

## Proving Grounds resource

Forge may use PROVING_GROUNDS through JAYTEC as a specialist-like validation
resource. It is not a model, planner or authority source. It can execute only
registered suites, returns evidence bound to the runtime commit, and cannot
modify production or choose new work. The Genesis roster must include its
zero-authority contract.

## Genesis gate

Before mode may become RUNNING:
- final Genesis packet is owner-approved;
- ROOT physical/live gates required by the activation plan have passed;
- Forge execution worker route is certified JAYTEC_CALLABLE;
- PROVING_GROUNDS is registered and its zero-authority contract is certified;
- cycle invoker and checkpoint verifier are wired;
- final hostile review passes;
- Jay explicitly authorizes activation.

GENESIS_EVENT_0001 begins Forge operational history. Pre-Genesis engineering
chats remain construction provenance, not Forge autobiographical history.
