# JAYTEC Durable Memory — Execution Protocols

## Objective Persistence Under Constraints

The owner's objective remains active until:
1. it succeeds;
2. Jay must supply a decision, authorization or resource;
3. an explicit stop/cost/time limit is reached; or
4. evidence shows no legitimate viable route currently exists.

A failed route is not a failed objective. Move through credible alternatives
without inventing authority, spending, hidden fallbacks or weaker verification.

## WATCH flow

Canonical unattended flow:

Jay/ChatGPT -> JAYTEC -> WATCH -> one fenced Manus worker -> JAYTEC specialist
help when requested -> same Manus worker -> WATCH verifies -> controller/SOL
review at gate boundary -> WATCH issues next bounded direction -> Manus continues.

Key properties:
- one canonical assignment/worker/fence lineage;
- internal specialist help is not a second worker;
- internal help does not consume recovery budget as a worker failure;
- every help request/result is correlated and bounded;
- heartbeats and provider SUCCESS are not gate-completion evidence;
- owner/operator pauses never auto-resume;
- fencing, idempotency and anti-duplication are preserved.

## "Give it to WATCH"

This means an operational handoff, not a note. The item must be made discoverable
in WATCH's canonical registry/control path with source references, ownership,
current status, constraints, collision check, destination and exact next action.

## Parallel work

Parallelize isolated creation, but serialize authority and promotion. Candidate
lanes must not overwrite canonical state or duplicate work already owned by
another worker/chat/branch.

## Handoffs

A handoff should contain:
- task/objective;
- exact current state and source refs;
- branch/commit/PR/deploy/task identifiers;
- owner/worker/fence;
- completed work;
- unresolved blockers;
- prohibited actions;
- next safe action;
- destination and acceptance criteria.

## Evidence discipline

Never claim tested/passed/verified/deployed/fixed unless the action actually ran
and its evidence was inspected. Model reasoning is not execution proof. Prefer
exact SHAs, run IDs, deploy IDs, task IDs, receipts and durable logs.

## Cost

No spend, subscription, credit top-up or paid fallback without explicit current
owner authorization. A configured credential is not spend authorization.
