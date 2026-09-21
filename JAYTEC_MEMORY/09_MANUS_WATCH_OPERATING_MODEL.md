# JAYTEC Durable Memory — Manus and WATCH Operating Model

## Separation of roles

WATCH is supervisor/traffic control.
Manus is the bounded asynchronous worker/task carrier.
JAYTEC is the routing/policy/authority layer.
Specialists are callable expertise.
ChatGPT/JAYTEC remains the primary controller/convergence layer on Jay's behalf.

Do not collapse these roles.

## Canonical cycle

1. Jay gives ChatGPT/JAYTEC an objective.
2. ChatGPT reconciles it against current JAYTEC state and constraints.
3. The work is handed to WATCH through the discoverable canonical registry.
4. WATCH binds/observes the canonical assignment, worker and fence.
5. WATCH delegates the bounded work packet to Manus.
6. Manus works asynchronously within its allowed connector/authority envelope.
7. If Manus needs expertise, it returns a SPECIALIST_REQUEST to JAYTEC.
8. JAYTEC validates the request and invokes the appropriate allowed specialist.
9. The specialist returns evidence/advice to JAYTEC.
10. JAYTEC returns the bounded result to the same Manus task.
11. Manus continues and returns completion evidence to WATCH.
12. WATCH validates the evidence.
13. At a gate/direction boundary, JAYTEC performs deterministic evidence review
    and SOL may be consulted as a bounded controller advisor.
14. JAYTEC converts validated advice into the existing controller/assignment
    contract; SOL itself does not gain authority.
15. WATCH delivers the next bounded direction to the same Manus lineage or
    advances only when gate evidence/authority permits.
16. The cycle repeats until success or a genuine owner/resource boundary.

## Why a single worker lane matters

The single worker/fence model prevents:
- Manus editing one state while SOL independently edits another;
- duplicate recovery workers;
- conflicting gate ownership;
- untraceable provider side effects;
- specialists accidentally becoming competing executors.

"Single lane" means one auditable authority/execution stream, not one
intelligence.

## Manus Home

Manus Home is the JAYTEC-owned Manus-specific layer: prompts, reusable workflows,
procedures, organization, tests and automation assets exposed through supported
interfaces.

Manus may improve this layer only within explicit scope. It cannot use
self-improvement as a reason to modify JAYTEC core policy, authority, provider
rules, canonical checkpoints or owner controls.

## Specialist requests

The default WATCH/Manus model-assistance trio is:
- SOL — engineering/reasoning;
- DeepSeek — adversarial/security review;
- Nemo — secondary engineering/reasoning.

DeepSeek and Nemo are configured for exact free-only routes in the no-spend
WATCH lane. Global/legacy OpenRouter routing remains separately locked unless
explicitly authorized.

SOL uses the exact gpt-5.6-sol route and remains subject to current provider-door
and spend policy. If the provider door is not authorized, WATCH must fail closed
or escalate rather than silently substituting another model for SOL.

## Provider failures

Provider failure does not:
- create a second worker;
- reset a fence;
- authorize paid fallback;
- authorize a different model;
- authorize weaker evidence;
- authorize broader connectors.

The assignment remains durable and recoverable.

## Headless work visibility

API workers do not necessarily create ChatGPT sidebar conversations. Therefore
JAYTEC must preserve durable evidence of:
- driver;
- worker id;
- fence;
- task/gate;
- specialist request id;
- exact model;
- provider identity where observable;
- result digest;
- current direction;
- commit/PR/artifact;
- acceptance status.

The owner should never have to guess who is doing the work.
