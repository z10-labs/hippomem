"""Proposals: the only way the memory changes.

Agents, reconciliation and engineers create proposals. Accepting one applies
it: a new decision, a new version that supersedes an old one, extra evidence,
an explored record promoted to a decision, or a confirmed repo mapping.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from hippomem import db, decisions, explored, systems
from hippomem.policy import Actor, Policy, refusal

KINDS = ("new", "evidence", "supersede", "promote", "mapping")
SOURCES = ("reconciliation", "scope", "author_answer", "agent", "engineer")


class Refused(Exception):
    """The actor isn't allowed to make this change."""


@dataclass(frozen=True)
class Proposal:
    id: str
    kind: str
    target: str | None
    payload: dict
    weight: str
    source: str
    status: str
    proposed_by: str
    proposed_via: str
    session_id: str
    branch_id: int | None
    created_at: str
    decided_at: str | None
    decided_by: str | None
    decided_by_kind: str | None
    note: str

    @property
    def title(self) -> str:
        if self.kind == "mapping":
            p = self.payload
            return f"Map {p['repo']}:{p['path_glob']} to {p['system_id']}"
        if self.kind == "evidence":
            return f"More evidence for {self.target}"
        return str(self.payload.get("title", ""))

    @property
    def systems(self) -> tuple[str, ...]:
        if "system_id" in self.payload:
            return (self.payload["system_id"],)
        return tuple(self.payload.get("systems", ()))


def create(
    conn: sqlite3.Connection,
    policy: Policy,
    *,
    kind: str,
    payload: dict,
    source: str,
    proposed_by: str,
    proposed_via: str = "",
    weight: str = "standard",
    target: str | None = None,
    session_id: str = "",
    branch_id: int | None = None,
) -> Proposal:
    if kind not in KINDS:
        raise ValueError(f"unknown proposal kind {kind!r}")
    if source not in SOURCES:
        raise ValueError(f"unknown proposal source {source!r}")
    _validate(conn, kind, payload, target)
    with db.transaction(conn):
        proposal_id = db.next_id(conn, "P")
        conn.execute(
            """INSERT INTO proposal (id, kind, target, payload, weight, source, status, proposed_by, proposed_via, session_id, branch_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
            (proposal_id, kind, target, json.dumps(payload), weight, source, proposed_by, proposed_via, session_id, branch_id, db.now()),
        )
        if kind == "evidence" and policy.evidence_only == "auto":
            _apply(conn, get(conn, proposal_id))
            _close(conn, proposal_id, "accepted", "rule:evidence_only", "rule", "accepted automatically: evidence only")
    return get(conn, proposal_id)


def _validate(conn: sqlite3.Connection, kind: str, payload: dict, target: str | None) -> None:
    if kind in ("new", "supersede", "promote"):
        if not str(payload.get("title", "")).strip():
            raise ValueError("a decision proposal needs a title")
        if not str(payload.get("why", "")).strip():
            raise ValueError("a decision proposal needs a 'why': a decision without its reason is worth nothing")
        for system_id in payload.get("systems", ()):
            if not systems.system_exists(conn, system_id):
                raise ValueError(f"unknown system {system_id!r}")
        for link in payload.get("links", ()):
            if not decisions.exists(conn, link["to"]):
                raise ValueError(f"link to unknown decision {link['to']!r}")
    if kind in ("evidence", "supersede") and (target is None or not decisions.exists(conn, target)):
        raise ValueError(f"{kind} proposal needs an existing target decision, got {target!r}")
    if kind == "promote":
        if target is None:
            raise ValueError("promote proposal needs the explored record as its target")
        explored.get(conn, target)
    if kind == "mapping":
        for key in ("repo", "path_glob", "system_id"):
            if not payload.get(key):
                raise ValueError(f"mapping proposal needs {key!r}")
        if not systems.system_exists(conn, payload["system_id"]):
            raise ValueError(f"unknown system {payload['system_id']!r}")


def get(conn: sqlite3.Connection, proposal_id: str) -> Proposal:
    row = conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise KeyError(proposal_id)
    return _from_row(row)


def list_by_status(conn: sqlite3.Connection, status: str = "open") -> list[Proposal]:
    return [_from_row(r) for r in conn.execute("SELECT * FROM proposal WHERE status = ? ORDER BY id", (status,))]


def accept(conn: sqlite3.Connection, policy: Policy, proposal_id: str, actor: Actor, note: str = "") -> str | None:
    """Apply the proposal. Returns the decision id it created or changed, if any."""
    with db.transaction(conn):
        proposal = get(conn, proposal_id)
        if proposal.status != "open":
            raise Refused(f"{proposal_id} is already {proposal.status}")
        reason = refusal(
            conn,
            policy,
            actor,
            kind=proposal.kind,
            weight=proposal.weight,
            payload=proposal.payload,
            proposed_by=proposal.proposed_by,
            proposed_via=proposal.proposed_via,
        )
        if reason:
            raise Refused(reason)
        decision_id = _apply(conn, proposal, accepted_by=actor.id, accepted_by_kind=actor.decided_by_kind)
        _close(conn, proposal_id, "accepted", actor.id, actor.decided_by_kind, note)
        return decision_id


def reject(conn: sqlite3.Connection, proposal_id: str, actor: Actor, note: str = "") -> None:
    with db.transaction(conn):
        proposal = get(conn, proposal_id)
        if proposal.status != "open":
            raise Refused(f"{proposal_id} is already {proposal.status}")
        _close(conn, proposal_id, "rejected", actor.id, actor.decided_by_kind, note)


def withdraw(conn: sqlite3.Connection, proposal_id: str, note: str = "") -> None:
    conn.execute("UPDATE proposal SET status = 'withdrawn', decided_at = ?, note = ? WHERE id = ? AND status = 'open'", (db.now(), note, proposal_id))


def _close(conn: sqlite3.Connection, proposal_id: str, status: str, decided_by: str, decided_by_kind: str, note: str) -> None:
    conn.execute(
        "UPDATE proposal SET status = ?, decided_at = ?, decided_by = ?, decided_by_kind = ?, note = ? WHERE id = ?",
        (status, db.now(), decided_by, decided_by_kind, note, proposal_id),
    )


def _apply(conn: sqlite3.Connection, p: Proposal, accepted_by: str = "rule:evidence_only", accepted_by_kind: str = "rule") -> str | None:
    who = {
        "made_by": p.proposed_by,
        "made_via": p.proposed_via,
        "accepted_by": accepted_by,
        "accepted_by_kind": accepted_by_kind,
        "proposal_id": p.id,
    }

    if p.kind == "mapping":
        systems.add_mapping(conn, p.payload["repo"], p.payload["path_glob"], p.payload["system_id"], created_by=p.proposed_by)
        return None

    if p.kind == "evidence":
        assert p.target is not None
        _add_evidence(conn, p, p.target)
        for anchor in p.payload.get("anchors", ()):
            decisions.add_anchor(conn, p.target, anchor["repo"], anchor["path_glob"])
        if p.payload.get("implemented"):
            decisions.mark_implemented(conn, p.target)
        return p.target

    content = decisions.Content.from_payload({**p.payload, "weight": p.weight})
    decision_id = decisions.create(conn, content, **who)
    _add_evidence(conn, p, decision_id)
    if p.kind == "supersede":
        assert p.target is not None
        decisions.add_link(conn, decision_id, "supersedes", p.target)
        decisions.set_status(conn, p.target, "superseded")
    if p.kind == "promote":
        assert p.target is not None
        explored.mark_promoted(conn, p.target, decision_id)
    return decision_id


def _add_evidence(conn: sqlite3.Connection, p: Proposal, decision_id: str) -> None:
    for ev in p.payload.get("evidence", ()):
        decisions.add_evidence(conn, ev["kind"], ev["ref"], ev.get("note", ""), decision_id=decision_id, proposal_id=p.id)


def _from_row(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        kind=row["kind"],
        target=row["target"],
        payload=json.loads(row["payload"]),
        weight=row["weight"],
        source=row["source"],
        status=row["status"],
        proposed_by=row["proposed_by"],
        proposed_via=row["proposed_via"],
        session_id=row["session_id"],
        branch_id=row["branch_id"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decided_by_kind=row["decided_by_kind"],
        note=row["note"],
    )
