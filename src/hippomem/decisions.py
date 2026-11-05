"""Accepted decisions: versions, scope, anchors, alternatives, constraints,
links and evidence.

Decisions are only ever created or changed by accepting a proposal (see
proposals.py). Nothing here checks who is allowed to do that.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from hippomem import db, embedding

LINK_TYPES = ("depends-on", "supersedes", "conflicts-with")
INVERSE = {"depends-on": "depended-on-by", "supersedes": "superseded-by", "conflicts-with": "conflicts-with"}


@dataclass(frozen=True)
class Alternative:
    option: str
    reason: str


@dataclass(frozen=True)
class Constraint:
    text: str
    kind: str = "technical"
    revisit_if: str = ""


@dataclass(frozen=True)
class Anchor:
    repo: str
    path_glob: str


@dataclass(frozen=True)
class Link:
    type: str  # depends-on | supersedes | conflicts-with, or an inverse when read from the other end
    other: str


@dataclass(frozen=True)
class Decision:
    id: str
    status: str
    version: int
    title: str
    statement: str
    why: str
    trade_off: str
    weight: str
    scope: str
    implemented: bool
    valid_from: str
    recorded_at: str
    made_by: str
    made_via: str
    accepted_by: str
    accepted_by_kind: str
    systems: tuple[str, ...] = ()
    anchors: tuple[Anchor, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    links: tuple[Link, ...] = field(default=())  # outbound and inverse links


@dataclass(frozen=True)
class Content:
    """What a decision version says. Built from a proposal payload."""

    title: str
    statement: str
    why: str
    trade_off: str = ""
    weight: str = "standard"
    scope: str = "code"
    implemented: bool = False
    systems: tuple[str, ...] = ()
    anchors: tuple[Anchor, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    links: tuple[Link, ...] = ()  # outbound only

    @staticmethod
    def from_payload(payload: dict) -> "Content":
        return Content(
            title=payload["title"],
            statement=payload.get("statement", ""),
            why=payload.get("why", ""),
            trade_off=payload.get("trade_off", ""),
            weight=payload.get("weight", "standard"),
            scope=payload.get("scope", "code"),
            implemented=bool(payload.get("implemented", False)),
            systems=tuple(payload.get("systems", ())),
            anchors=tuple(Anchor(a["repo"], a["path_glob"]) for a in payload.get("anchors", ())),
            alternatives=tuple(Alternative(a["option"], a["reason"]) for a in payload.get("alternatives", ())),
            constraints=tuple(Constraint(c["text"], c.get("kind", "technical"), c.get("revisit_if", "")) for c in payload.get("constraints", ())),
            links=tuple(Link(lk["type"], lk["to"]) for lk in payload.get("links", ())),
        )


def search_text(content: Content) -> str:
    """The text a decision is embedded from: what it says, why, and what it rejected."""
    rejected = " ".join(f"Rejected {a.option}: {a.reason}." for a in content.alternatives)
    return f"{content.title}. {content.statement} {content.why} {rejected}".strip()


def create(
    conn: sqlite3.Connection,
    content: Content,
    *,
    made_by: str,
    made_via: str,
    accepted_by: str,
    accepted_by_kind: str,
    proposal_id: str,
    valid_from: str | None = None,
) -> str:
    with db.transaction(conn):
        decision_id = db.next_id(conn, "D")
        now = db.now()
        conn.execute("INSERT INTO decision (id, current_version, status, created_at) VALUES (?, 1, 'accepted', ?)", (decision_id, now))
        _write_version(conn, decision_id, 1, content, made_by, made_via, accepted_by, accepted_by_kind, proposal_id, valid_from or now)
        return decision_id


def add_version(
    conn: sqlite3.Connection,
    decision_id: str,
    content: Content,
    *,
    made_by: str,
    made_via: str,
    accepted_by: str,
    accepted_by_kind: str,
    proposal_id: str,
) -> int:
    """A new version replaces scope, anchors, alternatives and constraints;
    earlier versions stay readable in decision_version."""
    with db.transaction(conn):
        row = conn.execute("SELECT current_version FROM decision WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(decision_id)
        version = row[0] + 1
        for table in ("decision_system", "anchor", "alternative", "constraint_"):
            conn.execute(f"DELETE FROM {table} WHERE decision_id = ?", (decision_id,))
        conn.execute("DELETE FROM link WHERE from_id = ? AND type != 'supersedes'", (decision_id,))
        _write_version(conn, decision_id, version, content, made_by, made_via, accepted_by, accepted_by_kind, proposal_id, db.now())
        conn.execute("UPDATE decision SET current_version = ? WHERE id = ?", (version, decision_id))
        return version


def _write_version(
    conn: sqlite3.Connection,
    decision_id: str,
    version: int,
    c: Content,
    made_by: str,
    made_via: str,
    accepted_by: str,
    accepted_by_kind: str,
    proposal_id: str,
    valid_from: str,
) -> None:
    vec = embedding.normalized(search_text(c))
    conn.execute(
        """INSERT INTO decision_version (decision_id, version, title, statement, why, trade_off, weight, scope, implemented,
               valid_from, recorded_at, made_by, made_via, accepted_by, accepted_by_kind, proposal_id, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            decision_id,
            version,
            c.title,
            c.statement,
            c.why,
            c.trade_off,
            c.weight,
            c.scope,
            int(c.implemented),
            valid_from,
            db.now(),
            made_by,
            made_via,
            accepted_by,
            accepted_by_kind,
            proposal_id,
            embedding.to_blob(vec),
        ),
    )
    for system_id in c.systems:
        conn.execute("INSERT OR IGNORE INTO decision_system (decision_id, system_id) VALUES (?, ?)", (decision_id, system_id))
    for a in c.anchors:
        conn.execute("INSERT OR IGNORE INTO anchor (decision_id, repo, path_glob) VALUES (?, ?, ?)", (decision_id, a.repo, a.path_glob))
    for alt in c.alternatives:
        conn.execute("INSERT INTO alternative (decision_id, option, reason) VALUES (?, ?, ?)", (decision_id, alt.option, alt.reason))
    for con in c.constraints:
        conn.execute(
            "INSERT INTO constraint_ (decision_id, text, kind, revisit_if) VALUES (?, ?, ?, ?)",
            (decision_id, con.text, con.kind, con.revisit_if),
        )
    for link in c.links:
        add_link(conn, decision_id, link.type, link.other)


def add_link(conn: sqlite3.Connection, from_id: str, link_type: str, to_id: str) -> None:
    if link_type not in LINK_TYPES:
        raise ValueError(f"unknown link type {link_type!r}; expected one of {', '.join(LINK_TYPES)}")
    if conn.execute("SELECT 1 FROM decision WHERE id = ?", (to_id,)).fetchone() is None:
        raise KeyError(to_id)
    conn.execute("INSERT OR IGNORE INTO link (from_id, type, to_id) VALUES (?, ?, ?)", (from_id, link_type, to_id))


def set_status(conn: sqlite3.Connection, decision_id: str, status: str) -> None:
    conn.execute("UPDATE decision SET status = ? WHERE id = ?", (status, decision_id))


def mark_implemented(conn: sqlite3.Connection, decision_id: str) -> None:
    conn.execute(
        "UPDATE decision_version SET implemented = 1 WHERE decision_id = ? AND version = (SELECT current_version FROM decision WHERE id = ?)",
        (decision_id, decision_id),
    )


def add_anchor(conn: sqlite3.Connection, decision_id: str, repo: str, path_glob: str) -> None:
    conn.execute("INSERT OR IGNORE INTO anchor (decision_id, repo, path_glob) VALUES (?, ?, ?)", (decision_id, repo, path_glob))


def add_evidence(
    conn: sqlite3.Connection, kind: str, ref: str, note: str = "", decision_id: str | None = None, proposal_id: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO evidence (decision_id, proposal_id, kind, ref, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (decision_id, proposal_id, kind, ref, note, db.now()),
    )


def evidence_for(conn: sqlite3.Connection, decision_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT kind, ref, note, created_at FROM evidence WHERE decision_id = ? ORDER BY id", (decision_id,)).fetchall()


def exists(conn: sqlite3.Connection, decision_id: str) -> bool:
    return conn.execute("SELECT 1 FROM decision WHERE id = ?", (decision_id,)).fetchone() is not None


def get(conn: sqlite3.Connection, decision_id: str) -> Decision:
    row = conn.execute(
        """SELECT d.id, d.status, v.* FROM decision d
           JOIN decision_version v ON v.decision_id = d.id AND v.version = d.current_version
           WHERE d.id = ?""",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise KeyError(decision_id)
    return _from_row(conn, row)


def all_current(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("accepted", "superseded", "retired")) -> list[Decision]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"""SELECT d.id, d.status, v.* FROM decision d
            JOIN decision_version v ON v.decision_id = d.id AND v.version = d.current_version
            WHERE d.status IN ({marks}) ORDER BY d.id""",
        statuses,
    ).fetchall()
    return [_from_row(conn, r) for r in rows]


def embeddings(conn: sqlite3.Connection) -> list[tuple[str, str, bytes]]:
    """(id, status, embedding) for the current version of every decision."""
    rows = conn.execute(
        """SELECT d.id, d.status, v.embedding FROM decision d
           JOIN decision_version v ON v.decision_id = d.id AND v.version = d.current_version
           ORDER BY d.id"""
    )
    return [(r[0], r[1], r[2]) for r in rows]


def links_of(conn: sqlite3.Connection, decision_id: str) -> tuple[Link, ...]:
    outbound = [Link(r[0], r[1]) for r in conn.execute("SELECT type, to_id FROM link WHERE from_id = ? ORDER BY to_id", (decision_id,))]
    inbound = [Link(INVERSE[r[0]], r[1]) for r in conn.execute("SELECT type, from_id FROM link WHERE to_id = ? ORDER BY from_id", (decision_id,))]
    return tuple(outbound + inbound)


def _from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Decision:
    decision_id = row["id"]
    return Decision(
        id=decision_id,
        status=row["status"],
        version=row["version"],
        title=row["title"],
        statement=row["statement"],
        why=row["why"],
        trade_off=row["trade_off"],
        weight=row["weight"],
        scope=row["scope"],
        implemented=bool(row["implemented"]),
        valid_from=row["valid_from"],
        recorded_at=row["recorded_at"],
        made_by=row["made_by"],
        made_via=row["made_via"],
        accepted_by=row["accepted_by"],
        accepted_by_kind=row["accepted_by_kind"],
        systems=tuple(r[0] for r in conn.execute("SELECT system_id FROM decision_system WHERE decision_id = ? ORDER BY system_id", (decision_id,))),
        anchors=tuple(
            Anchor(r[0], r[1])
            for r in conn.execute("SELECT repo, path_glob FROM anchor WHERE decision_id = ? ORDER BY repo, path_glob", (decision_id,))
        ),
        alternatives=tuple(
            Alternative(r[0], r[1]) for r in conn.execute("SELECT option, reason FROM alternative WHERE decision_id = ? ORDER BY id", (decision_id,))
        ),
        constraints=tuple(
            Constraint(r[0], r[1], r[2])
            for r in conn.execute("SELECT text, kind, revisit_if FROM constraint_ WHERE decision_id = ? ORDER BY id", (decision_id,))
        ),
        links=links_of(conn, decision_id),
    )


def content_to_payload(content: Content) -> dict:
    """Inverse of Content.from_payload, for building proposals in code and tests."""
    return {
        "title": content.title,
        "statement": content.statement,
        "why": content.why,
        "trade_off": content.trade_off,
        "weight": content.weight,
        "scope": content.scope,
        "implemented": content.implemented,
        "systems": list(content.systems),
        "anchors": [{"repo": a.repo, "path_glob": a.path_glob} for a in content.anchors],
        "alternatives": [{"option": a.option, "reason": a.reason} for a in content.alternatives],
        "constraints": [{"text": c.text, "kind": c.kind, "revisit_if": c.revisit_if} for c in content.constraints],
        "links": [{"type": lk.type, "to": lk.other} for lk in content.links],
    }
