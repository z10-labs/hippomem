"""Explored records: dead ends worth remembering, never decisions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from hippomem import db, embedding


@dataclass(frozen=True)
class Explored:
    id: str
    title: str
    tried: str
    outcome: str
    why: str
    revisit_if: str
    system_id: str | None
    repo: str
    branch_id: int | None
    promoted_to: str | None
    created_at: str


def search_text(title: str, tried: str, why: str) -> str:
    return f"{title}. Tried: {tried}. Dropped because {why}"


def create(
    conn: sqlite3.Connection,
    *,
    title: str,
    tried: str,
    outcome: str,
    why: str,
    revisit_if: str,
    system_id: str | None = None,
    repo: str = "",
    branch_id: int | None = None,
) -> str:
    if not revisit_if.strip():
        raise ValueError("an explored record needs 'revisit_if': what would make this worth trying again")
    with db.transaction(conn):
        explored_id = db.next_id(conn, "X")
        vec = embedding.normalized(search_text(title, tried, why))
        conn.execute(
            """INSERT INTO explored (id, title, tried, outcome, why, revisit_if, system_id, repo, branch_id, created_at, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (explored_id, title, tried, outcome, why, revisit_if, system_id, repo, branch_id, db.now(), embedding.to_blob(vec)),
        )
        return explored_id


def get(conn: sqlite3.Connection, explored_id: str) -> Explored:
    row = conn.execute(
        "SELECT id, title, tried, outcome, why, revisit_if, system_id, repo, branch_id, promoted_to, created_at FROM explored WHERE id = ?",
        (explored_id,),
    ).fetchone()
    if row is None:
        raise KeyError(explored_id)
    return Explored(*tuple(row))


def all_records(conn: sqlite3.Connection) -> list[Explored]:
    rows = conn.execute(
        "SELECT id, title, tried, outcome, why, revisit_if, system_id, repo, branch_id, promoted_to, created_at FROM explored ORDER BY id"
    )
    return [Explored(*tuple(r)) for r in rows]


def embeddings(conn: sqlite3.Connection) -> list[tuple[str, bytes]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT id, embedding FROM explored WHERE promoted_to IS NULL ORDER BY id")]


def mark_promoted(conn: sqlite3.Connection, explored_id: str, decision_id: str) -> None:
    conn.execute("UPDATE explored SET promoted_to = ? WHERE id = ?", (decision_id, explored_id))
