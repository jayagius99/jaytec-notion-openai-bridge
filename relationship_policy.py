"""Machine-enforced relationship graph for JAYTEC participants.

This module makes the communication structure explicit and fail-closed.
A participant may only use an edge listed here, for an allowed purpose, and
subject to the current-task authority gate attached to that edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Actor(StrEnum):
    JAY = "jay"
    CHATGPT = "chatgpt"
    JAYTEC = "jaytec"
    MANUS = "manus"
    GEMINI = "gemini"
    ENGINEERING = "engineering_specialist"
    NOTION = "notion_gateway"
    GITHUB = "github"
    NEON = "neon"
    RENDER = "render"


class Purpose(StrEnum):
    DIRECTIVE = "directive"
    AUTHORIZE = "authorize"
    DELEGATE = "delegate"
    TASK_PACKET = "task_packet"
    RESULT = "result"
    ESCALATION = "escalation"
    SPECIALIST_REQUEST = "specialist_request"
    SPECIALIST_TASK = "specialist_task"
    TRANSFER_REQUEST = "transfer_request"
    TRANSFER_RESULT = "transfer_result"
    READ = "read"
    INSPECT = "inspect"
    DIAGNOSE = "diagnose"
    TEST = "test"
    WRITE = "write"
    DEPLOY = "deploy"
    DELETE = "delete"
    MIGRATE = "migrate"
    CREDENTIAL_CHANGE = "credential_change"


class RelationshipPolicyError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EdgeRule:
    source: Actor
    destination: Actor
    purposes: frozenset[Purpose]
    requires_current_authority: bool = False
    requires_jay_via_chatgpt_for_notion: bool = False


READISH = frozenset({
    Purpose.READ,
    Purpose.INSPECT,
    Purpose.DIAGNOSE,
    Purpose.TEST,
})
MUTATING = frozenset({
    Purpose.WRITE,
    Purpose.DEPLOY,
    Purpose.DELETE,
    Purpose.MIGRATE,
    Purpose.CREDENTIAL_CHANGE,
})

EDGE_RULES = (
    EdgeRule(Actor.JAY, Actor.CHATGPT, frozenset({Purpose.DIRECTIVE, Purpose.AUTHORIZE})),
    EdgeRule(Actor.CHATGPT, Actor.JAYTEC, frozenset({Purpose.DIRECTIVE, Purpose.AUTHORIZE, Purpose.DELEGATE})),
    EdgeRule(Actor.JAYTEC, Actor.CHATGPT, frozenset({Purpose.RESULT, Purpose.ESCALATION})),
    EdgeRule(Actor.JAYTEC, Actor.MANUS, frozenset({Purpose.DELEGATE, Purpose.TASK_PACKET}), True),
    EdgeRule(Actor.MANUS, Actor.JAYTEC, frozenset({Purpose.RESULT, Purpose.ESCALATION, Purpose.SPECIALIST_REQUEST})),
    EdgeRule(Actor.JAYTEC, Actor.GEMINI, frozenset({Purpose.SPECIALIST_TASK}), True),
    EdgeRule(Actor.GEMINI, Actor.JAYTEC, frozenset({Purpose.RESULT})),
    EdgeRule(Actor.JAYTEC, Actor.ENGINEERING, frozenset({Purpose.SPECIALIST_TASK}), True),
    EdgeRule(Actor.ENGINEERING, Actor.JAYTEC, frozenset({Purpose.RESULT})),
    EdgeRule(
        Actor.JAYTEC,
        Actor.NOTION,
        frozenset({Purpose.TRANSFER_REQUEST}),
        True,
        True,
    ),
    EdgeRule(Actor.NOTION, Actor.JAYTEC, frozenset({Purpose.TRANSFER_RESULT})),
    EdgeRule(Actor.MANUS, Actor.GITHUB, READISH | MUTATING, False),
    EdgeRule(Actor.MANUS, Actor.NEON, READISH | MUTATING, False),
    EdgeRule(Actor.MANUS, Actor.RENDER, READISH | MUTATING, False),
    EdgeRule(Actor.GITHUB, Actor.MANUS, frozenset({Purpose.RESULT})),
    EdgeRule(Actor.NEON, Actor.MANUS, frozenset({Purpose.RESULT})),
    EdgeRule(Actor.RENDER, Actor.MANUS, frozenset({Purpose.RESULT})),
)

_RULE_INDEX = {(r.source, r.destination): r for r in EDGE_RULES}


def authorize_relationship(
    *,
    source: str | Actor,
    destination: str | Actor,
    purpose: str | Purpose,
    current_task_authorized: bool = False,
    connector_mutation_authorized: bool = False,
    jay_authorized_notion_via_chatgpt: bool = False,
) -> EdgeRule:
    if type(current_task_authorized) is not bool:
        raise RelationshipPolicyError("RELATIONSHIP_CURRENT_AUTH_INVALID")
    if type(connector_mutation_authorized) is not bool:
        raise RelationshipPolicyError("RELATIONSHIP_MUTATION_AUTH_INVALID")
    if type(jay_authorized_notion_via_chatgpt) is not bool:
        raise RelationshipPolicyError("RELATIONSHIP_NOTION_AUTH_INVALID")

    try:
        src = source if isinstance(source, Actor) else Actor(str(source).strip().casefold())
        dst = destination if isinstance(destination, Actor) else Actor(str(destination).strip().casefold())
        use = purpose if isinstance(purpose, Purpose) else Purpose(str(purpose).strip().casefold())
    except ValueError as exc:
        raise RelationshipPolicyError("RELATIONSHIP_VALUE_INVALID") from exc

    rule = _RULE_INDEX.get((src, dst))
    if rule is None:
        raise RelationshipPolicyError(f"RELATIONSHIP_EDGE_BLOCKED:{src}->{dst}")
    if use not in rule.purposes:
        raise RelationshipPolicyError(
            f"RELATIONSHIP_PURPOSE_BLOCKED:{src}->{dst}:{use}"
        )

    # Connector reads are part of a delegated task. Mutations are a separate
    # authority class and are never implied by connector presence or ordinary
    # task authorization.
    is_connector_mutation = (
        src is Actor.MANUS
        and dst in {Actor.GITHUB, Actor.NEON, Actor.RENDER}
        and use in MUTATING
    )
    if is_connector_mutation:
        if not current_task_authorized or not connector_mutation_authorized:
            raise RelationshipPolicyError(
                "RELATIONSHIP_CONNECTOR_MUTATION_AUTH_REQUIRED"
            )
    elif rule.requires_current_authority and not current_task_authorized:
        raise RelationshipPolicyError("RELATIONSHIP_CURRENT_AUTH_REQUIRED")

    if rule.requires_jay_via_chatgpt_for_notion and not jay_authorized_notion_via_chatgpt:
        raise RelationshipPolicyError("RELATIONSHIP_NOTION_AUTH_REQUIRED")

    return rule


def manus_outbound_destinations() -> frozenset[Actor]:
    return frozenset(r.destination for r in EDGE_RULES if r.source is Actor.MANUS)


def notion_outbound_destinations() -> frozenset[Actor]:
    return frozenset(r.destination for r in EDGE_RULES if r.source is Actor.NOTION)


def specialist_outbound_destinations() -> dict[Actor, frozenset[Actor]]:
    return {
        actor: frozenset(r.destination for r in EDGE_RULES if r.source is actor)
        for actor in (Actor.GEMINI, Actor.ENGINEERING)
    }
