"""Who may accept which proposal.

The rules come from docs/hippomem.md ("Who accepts"):

- Evidence added to an accepted decision: any reviewer, or accepted
  automatically by rule.
- A new decision of ordinary weight: a reviewer agent or an engineer.
- A heavy decision, or one that supersedes or conflicts with another: an
  engineer.
- A decision covering a module, service or team, and a repo mapping: an owner
  of one of its systems. A system with no owners falls back to any engineer.
- Nobody approves their own proposal: not the engineer it came from, and not
  the agent that wrote it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from hippomem import systems

ENGINEER_KINDS = ("engineer", "admin")


@dataclass(frozen=True)
class Policy:
    evidence_only: str = "auto"  # auto | review
    self_approval: str = "never"  # never | standard (a solo engineer may accept their own standard proposals)


@dataclass(frozen=True)
class Actor:
    id: str
    kind: str  # engineer | reviewer | admin
    agent: str = ""  # the agent acting for an engineer, if any

    @property
    def decided_by_kind(self) -> str:
        return "agent" if self.kind == "reviewer" else "engineer"


def required_level(kind: str, weight: str, payload: dict) -> str:
    """One of: evidence, standard, heavy, scope."""
    if kind == "evidence":
        return "evidence"
    if kind == "mapping" or payload.get("scope") in ("system", "team"):
        return "scope"
    if weight == "heavy" or kind == "supersede" or any(lk.get("type") == "conflicts-with" for lk in payload.get("links", ())):
        return "heavy"
    return "standard"


def refusal(
    conn: sqlite3.Connection,
    policy: Policy,
    actor: Actor,
    *,
    kind: str,
    weight: str,
    payload: dict,
    proposed_by: str,
    proposed_via: str,
) -> str | None:
    """Why `actor` may not accept this proposal, or None if they may."""
    level = required_level(kind, weight, payload)

    if actor.kind == "reviewer":
        if proposed_via and proposed_via == actor.id:
            return "an agent cannot accept a proposal it wrote"
        if level in ("heavy", "scope"):
            return f"a {level} proposal needs an engineer"
        return None

    if actor.kind not in ENGINEER_KINDS:
        return f"unknown actor kind {actor.kind!r}"

    if actor.id == proposed_by and level != "evidence":
        if policy.self_approval == "never" or level != "standard":
            return "nobody approves their own proposal; another engineer has to accept it"

    if level == "scope" and actor.kind != "admin":
        owned = _systems_in(payload)
        owners = {o for s in owned for o in systems.owners_of(conn, s)}
        if owners and actor.id not in owners:
            return f"needs an owner of {', '.join(owned)} ({', '.join(sorted(owners))})"
    return None


def _systems_in(payload: dict) -> list[str]:
    if "system_id" in payload:
        return [payload["system_id"]]
    return list(payload.get("systems", ()))
