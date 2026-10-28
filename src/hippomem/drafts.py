"""Branches and their draft trails.

A draft is written the moment an agent or engineer decides, weighs or drops
something. Drafts belong to the branch, not the session: one branch often
spans several sessions and agents.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from hippomem import db, embedding, systems


@dataclass(frozen=True)
class Draft:
    id: int
    branch_id: int
    kind: str  # decided | considering
    title: str
    statement: str
    why: str
    scope: str
    system_id: str | None
    paths: tuple[str, ...]
    engineer: str
    agent: str
    session_id: str
    status: str  # open | dropped | proposed | alternative | discarded | explored
    dropped_reason: str
    replaced_by: int | None
    backed_by: str | None
    outcome: str | None
    created_at: str


@dataclass(frozen=True)
class Branch:
    id: int
    repo: str
    name: str
    state: str
    created_at: str
    last_activity_at: str
    ended_at: str | None
    head_commit: str | None


BRANCH_COLUMNS = "id, repo, name, state, created_at, last_activity_at, ended_at, head_commit"


def ensure_branch(conn: sqlite3.Connection, repo: str, name: str) -> int:
    """The active branch row for repo/name, created on first sight."""
    with db.transaction(conn):
        row = conn.execute("SELECT id FROM branch WHERE repo = ? AND name = ? AND state = 'active'", (repo, name)).fetchone()
        if row:
            conn.execute("UPDATE branch SET last_activity_at = ? WHERE id = ?", (db.now(), row[0]))
            return int(row[0])
        now = db.now()
        cur = conn.execute(
            "INSERT INTO branch (repo, name, state, created_at, last_activity_at) VALUES (?, ?, 'active', ?, ?) RETURNING id",
            (repo, name, now, now),
        )
        return int(cur.fetchone()[0])


def get_branch(conn: sqlite3.Connection, branch_id: int) -> Branch:
    row = conn.execute(
        "SELECT id, repo, name, state, created_at, last_activity_at, ended_at, head_commit FROM branch WHERE id = ?", (branch_id,)
    ).fetchone()
    if row is None:
        raise KeyError(branch_id)
    return Branch(*tuple(row))


def find_active(conn: sqlite3.Connection, repo: str, name: str) -> Branch | None:
    row = conn.execute(
        f"SELECT {BRANCH_COLUMNS} FROM branch WHERE repo = ? AND name = ? AND state = 'active'",
        (repo, name),
    ).fetchone()
    return Branch(*tuple(row)) if row else None


def end_branch(conn: sqlite3.Connection, branch_id: int, state: str) -> None:
    if state not in ("merged", "closed"):
        raise ValueError(state)
    conn.execute("UPDATE branch SET state = ?, ended_at = ? WHERE id = ?", (state, db.now(), branch_id))


def add(
    conn: sqlite3.Connection,
    branch_id: int,
    *,
    title: str,
    why: str,
    engineer: str,
    agent: str = "",
    session_id: str = "",
    kind: str = "decided",
    statement: str = "",
    scope: str = "code",
    system_id: str | None = None,
    paths: tuple[str, ...] = (),
    replaces: int | None = None,
    replaced_because: str = "",
) -> int:
    """Record a draft. `replaces` drops an earlier draft on the same branch in
    favour of this one; `replaced_because` says why it was dropped."""
    if kind not in ("decided", "considering"):
        raise ValueError("draft kind must be 'decided' or 'considering'")
    if scope not in ("code", "system", "team"):
        raise ValueError("scope must be code, system or team")
    if not title.strip():
        raise ValueError("a draft needs a title")
    if system_id and not systems.system_exists(conn, system_id):
        raise ValueError(f"unknown system {system_id!r}")
    with db.transaction(conn):
        if replaces is not None:
            _check_on_branch(conn, replaces, branch_id)
        now = db.now()
        vec = embedding.normalized(f"{title}. {statement} {why}")
        cur = conn.execute(
            """INSERT INTO draft (branch_id, kind, title, statement, why, scope, system_id, paths, engineer, agent, session_id,
                   status, created_at, updated_at, embedding)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?) RETURNING id""",
            (
                branch_id,
                kind,
                title,
                statement,
                why,
                scope,
                system_id,
                json.dumps(list(paths)),
                engineer,
                agent,
                session_id,
                now,
                now,
                embedding.to_blob(vec),
            ),
        )
        draft_id = int(cur.fetchone()[0])
        if replaces is not None:
            drop(conn, replaces, replaced_because or why, replaced_by=draft_id)
        conn.execute("UPDATE branch SET last_activity_at = ? WHERE id = ?", (now, branch_id))
        return draft_id


def _check_on_branch(conn: sqlite3.Connection, draft_id: int, branch_id: int) -> None:
    row = conn.execute("SELECT branch_id, status FROM draft WHERE id = ?", (draft_id,)).fetchone()
    if row is None or row[0] != branch_id:
        raise ValueError(f"draft #{draft_id} is not on this branch")
    if row[1] != "open":
        raise ValueError(f"draft #{draft_id} is already {row[1]}")


def drop(conn: sqlite3.Connection, draft_id: int, reason: str, replaced_by: int | None = None) -> None:
    """Mark a draft as reversed during work. The reason is kept: it becomes
    the 'why rejected' on the decision that wins, or an explored record."""
    if not reason.strip():
        raise ValueError("say why the draft was dropped; that reason is the valuable part")
    conn.execute(
        "UPDATE draft SET status = 'dropped', dropped_reason = ?, replaced_by = ?, updated_at = ? WHERE id = ? AND status = 'open'",
        (reason, replaced_by, db.now(), draft_id),
    )


def set_outcome(conn: sqlite3.Connection, draft_id: int, status: str, outcome: str | None = None) -> None:
    conn.execute("UPDATE draft SET status = ?, outcome = ?, updated_at = ? WHERE id = ?", (status, outcome, db.now(), draft_id))


def get(conn: sqlite3.Connection, draft_id: int) -> Draft:
    row = conn.execute("SELECT * FROM draft WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise KeyError(draft_id)
    return _from_row(row)


def trail(conn: sqlite3.Connection, branch_id: int, statuses: tuple[str, ...] = ("open", "dropped")) -> list[Draft]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(f"SELECT * FROM draft WHERE branch_id = ? AND status IN ({marks}) ORDER BY id", (branch_id, *statuses))
    return [_from_row(r) for r in rows]


def count_since(conn: sqlite3.Connection, session_id: str, since: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM draft WHERE session_id = ? AND created_at >= ?", (session_id, since)).fetchone()
    return int(row[0])


def mark_backed(conn: sqlite3.Connection, branch_id: int, commit: str, files: list[str]) -> list[int]:
    """Drafts on the branch not yet backed by code, whose paths the commit
    touched, or that named no paths at all, are now backed by this commit."""
    backed = []
    for d in trail(conn, branch_id, ("open",)):
        if d.backed_by:
            continue
        if not d.paths or any(f == p or f.startswith(p.rstrip("/") + "/") for p in d.paths for f in files):
            conn.execute("UPDATE draft SET backed_by = ?, updated_at = ? WHERE id = ?", (commit, db.now(), d.id))
            backed.append(d.id)
    return backed


def _from_row(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        branch_id=row["branch_id"],
        kind=row["kind"],
        title=row["title"],
        statement=row["statement"],
        why=row["why"],
        scope=row["scope"],
        system_id=row["system_id"],
        paths=tuple(json.loads(row["paths"])),
        engineer=row["engineer"],
        agent=row["agent"],
        session_id=row["session_id"],
        status=row["status"],
        dropped_reason=row["dropped_reason"],
        replaced_by=row["replaced_by"],
        backed_by=row["backed_by"],
        outcome=row["outcome"],
        created_at=row["created_at"],
    )
