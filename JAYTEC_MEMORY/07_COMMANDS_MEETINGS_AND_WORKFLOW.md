# JAYTEC Durable Memory — Commands, Meetings and Workflow

## Command semantics

JAYTEC command words are owner/operator protocol conventions, not magical
authority tokens. Always reconcile them against current owner intent and current
policy.

Important historical/current conventions include:
- JAYTEC:EXECUTE — explicit instruction to carry out JAYTEC work in the current
  chat/workflow when execution authority is otherwise available.
- JAYTEC:READ — verified retrieval path; historically research-first with a
  fallback bus only when explicitly allowed.
- JAYTEC:MEETINGINFO-ADD — append a topic to the team-meeting information queue.
- JAYTEC:MEETINGINFO — return the latest meeting report in human-readable form.
- @prompt — compile-only master-prompt builder; do not execute the compiled work
  merely because the prompt was produced.
- @next — continuity/handover workflow that should recover exact state, generate
  the handover, write/update the continuity checkpoint/index, verify the write,
  and stop.
- "give it to WATCH" — hard operational handoff semantics defined in the
  execution-protocol memory, not a casual note.
- "continue from exact state" — recover and continue the existing work lineage;
  do not recreate already completed work or start a duplicate lane.

Trigger syntax can evolve. The current owner instruction and current canonical
policy always override stale historical syntax.

## Team meetings

Jay wants JAYTEC meetings to be real multi-participant system-learning events,
not optional one-model summaries.

Default principle:
- ChatGPT coordinates and reconciles.
- SOL contributes engineering/reasoning.
- Manus contributes automation/execution observations.
- DeepSeek contributes adversarial/security review.
- Nemo contributes independent secondary reasoning.
- Other current specialists/services that can materially contribute should be
  consulted when appropriate.

Explicit exclusions for the meeting process:
- no Notion Agent;
- no autonomous Notion reasoning;
- no custom agents merely for conversation;
- no Work-chat session as a substitute participant.

Meeting records should be stored in owner-controlled JAYTEC space so future
analysis can learn from decisions, failures, tradeoffs and outcomes.

Meetings do not grant execution authority. Their conclusions still pass through
normal JAYTEC acceptance, cost, security and owner boundaries.

## Meeting learning

Useful meeting memory should separate:
- decision made;
- evidence used;
- dissent/challenge;
- unresolved assumptions;
- action owner;
- expected result;
- actual later result;
- lesson learned;
- procedural update proposed.

The goal is not to preserve every word. The goal is to improve future behavior
from evidence.

## "Make yourself useful" parallel-work behavior

When ChatGPT is asked to make itself useful in a JAYTEC/Forge context, prefer a
high-value isolated task that does not collide with the canonical worker or
another active specialist. Parallelize creation, not authority. Candidate work
must remain non-canonical until reconciled and accepted.

## Communication style for system status

Owner-facing status should make it easy to answer:
- what is being worked on;
- who/what is driving;
- which worker is active;
- which specialists were called;
- what changed;
- what is blocked;
- what evidence proves progress;
- what Jay must decide or do next.

Do not hide important blockers behind optimistic summaries.
