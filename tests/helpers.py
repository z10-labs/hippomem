"""Helpers shared by tests."""

from __future__ import annotations

from hippomem import decisions, proposals
from hippomem.policy import Actor, Policy

POLICY = Policy()
ALEX = Actor("@alex", "engineer")
SAM = Actor("@sam", "engineer")
KIM = Actor("@kim", "admin")
WATCHMAN = Actor("watchman", "reviewer")


def accepted_decision(conn, title: str, why: str = "because", *, systems_=("billing",), scope="code", weight="standard", links=(), alternatives=()):
    """Create and accept a decision proposed by @sam via an agent, accepted by @alex."""
    payload = decisions.content_to_payload(
        decisions.Content(
            title=title,
            statement=title,
            why=why,
            scope=scope,
            systems=tuple(systems_),
            links=tuple(links),
            alternatives=tuple(alternatives),
        )
    )
    p = proposals.create(conn, POLICY, kind="new", payload=payload, source="agent", proposed_by="@sam", proposed_via="claude-code", weight=weight)
    accepter = KIM if scope != "code" else ALEX
    decision_id = proposals.accept(conn, POLICY, p.id, accepter)
    assert decision_id is not None
    return decision_id
