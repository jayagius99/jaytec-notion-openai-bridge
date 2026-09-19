# JAYTEC Relationship Contract

Version: `JAYTEC_RELATIONSHIP_CONTRACT_V1`

This document defines the only normal communication structure between JAYTEC
participants. The executable form is `relationship_policy.py`.

## Authority chain

`Jay -> ChatGPT -> JAYTEC -> Manus / specialists`

Jay supplies intent and ultimate user authority. ChatGPT interprets and applies
that authority. JAYTEC is the controlled orchestration layer. Manus and the
specialists are workers, not alternative control planes.

## Manus

JAYTEC may delegate a current task to Manus through a strict task packet.
Manus returns results, evidence, escalation requests, or specialist requests to
JAYTEC.

Manus does not directly dispatch Gemini, the engineering specialist, Notion,
OpenAI, or OpenRouter. A Manus specialist request always returns to JAYTEC for
routing.

Manus may directly use only GitHub, Neon, and Render, but that is a maximum
allowlist rather than an automatic grant. Each task receives the minimum
explicit subset it needs; default connector scope is NONE. Project/user
connector defaults are never accepted as JAYTEC authority.
Read/inspect/diagnose/test is allowed only inside that explicit task scope.
Mutation requires fresh current-task authority.

## Gemini and engineering specialist

JAYTEC sends bounded specialist tasks. Specialists return results to JAYTEC.
They do not delegate to Manus, each other, Notion, or infrastructure services.

## JAYTEC versus Notion Agent identity

JAYTEC and the Notion Agent are never synonyms.

JAYTEC means the JAYTEC system, architecture, infrastructure, control plane,
bridges, routing layer, runtime, state, policies and specialist pathways.

NOTION means the Notion Agent only.

A request such as "use JAYTEC", "ask JAYTEC", "check with JAYTEC", or "send
this through JAYTEC" always targets JAYTEC itself. It never authorizes or
selects the Notion Agent.

If a required JAYTEC path is unavailable, the caller reports that missing path.
It must not substitute the Notion Agent.

## Notion Agent

The Notion Agent is strict pass-through transport only and is absent from the
normal authority chain. The only allowed edge is:

`JAYTEC -> Notion Agent (current explicit Jay request + exact ChatGPT-controlled transport packet)`

followed by:

`Notion Agent -> JAYTEC (transfer result/failure evidence)`

ChatGPT must specify the exact request, destination, JAYTEC tool/bridge/path,
arguments/instructions and required return evidence.

The Notion Agent does not execute JAYTEC work, research, reason about the task,
choose tools/providers/specialists, determine routing/authority, expand scope,
create follow-up work, troubleshoot independently, retry through an alternate
path, or become a fallback route.

If the exact transport action cannot be completed, the Notion Agent stops and
returns the exact failure evidence and mechanical condition required to restore
that exact route.

## Infrastructure connectors

GitHub, Neon, and Render are resources Manus may inspect within a delegated
task. Their existence does not grant control-plane authority and they cannot be
used to route around JAYTEC.

## Hidden-path rule

Any communication edge not explicitly represented in
`relationship_policy.py` fails closed. No actor may infer a route from
convenience, previous permissions, connector presence, historical behavior, or
another actor's permissions.

## Fresh-authority rule

Authority is task-scoped. A prior approval cannot be replayed to authorize a
new JAYTEC change, production mutation, external spend, or Notion use.

## Verification rule

Every actor reports only what it can evidence. JAYTEC validates specialist and
Manus outputs before they are accepted as completed work. An unavailable,
unobservable, mismatched, or policy-violating worker fails closed instead of
silently falling back.

## Project connector resolution

The Manus project is the approved project container, but project-level connector
defaults are never treated as sufficient isolation by themselves. JAYTEC must
explicitly send the connector list for every new Manus task. An explicit empty
list means no connector use for that task. On follow-up turns JAYTEC must either
send the approved connector IDs again or explicitly clear the connector set; it
must never omit connector control and silently inherit/reuse broader defaults.

The task must also explicitly request the Lite profile and JAYTEC must verify
the observed profile from Manus before accepting participation as valid.
