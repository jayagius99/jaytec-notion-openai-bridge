# JAYTEC PROVING GROUNDS V1

Status: PRE-GENESIS / STAGING RESOURCE

## Role

PROVING_GROUNDS is a first-class JAYTEC resource that Forge may request in
the same general way it requests a specialist.

It is intentionally not an AI specialist and has no independent cognition,
authority, routing, provider choice, or production mutation power.

Authority path:

Jay -> ChatGPT/Forge -> JAYTEC -> PROVING_GROUNDS

Proving Grounds may return evidence. It may never turn evidence into authority.

## Allowed behavior

- run a named, pre-registered validation suite;
- run bounded adversarial/unit/benchmark validation;
- bind each result to the exact runtime commit;
- return structured PASS/FAIL/TIMEOUT/FAILED_CLOSED evidence;
- expose a read-only catalog of registered suites.

## Forbidden behavior

- arbitrary shell commands;
- user-supplied file paths or module names;
- choosing a new test suite on its own;
- planning, routing or creating follow-up work;
- modifying production;
- spending money or selecting providers;
- granting authority;
- changing ROOT_OWNER, Genesis, secrets or credentials;
- claiming a change is proven without executed evidence.

## Forge relationship

The Forge Genesis specialist/resource roster must contain SOL as the primary
engineering/cognition specialist contract and PROVING_GROUNDS as the
evidence/adversarial-validation resource.

Genesis seeding fails closed if Proving Grounds is missing or its zero-authority
contract drifts.

Forge can request a Proving Grounds run when it needs to validate:

1. a candidate self-improvement;
2. a cognition or memory change;
3. a routing/authority change;
4. a recovery/WATCH change;
5. a specialist-governance change;
6. a future V2/V3 upgrade candidate.

A successful Proving Grounds run is evidence, not automatic promotion.
Production/Genesis/upgrade promotion remains governed by the relevant JAYTEC
gate and ROOT_OWNER/owner approval rules.

## V1 suites

- forge-cognition-core
- authority-boundaries
- watch-recovery
- manus-governance

The registry is code-defined and allow-listed. New suites require reviewed code
and regression tests; they cannot be injected through request content.

## Upgrade direction

V2 should add isolated disposable environments, richer fault injection, fixture
snapshots and retained evidence artifacts.

V3 should add a Full Control Lab capable of testing whole candidate runtimes
under network/provider/database/worker/memory/security failures while preserving
production rollback and ROOT_OWNER control.
