"""Systems, their owners, and the mapping from repo paths to systems."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from fnmatch import fnmatchcase

from hippomem import db

# A mapping row whose glob is this covers the whole repo.
WHOLE_REPO = "**"


@dataclass(frozen=True)
class Mapping:
    id: int
    repo: str
    path_glob: str
    system_id: str
    status: str


def create_system(conn: sqlite3.Connection, system_id: str, name: str, description: str = "", owners: tuple[str, ...] = ()) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO system (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (system_id, name, description, db.now()),
        )
        for owner in owners:
            conn.execute("INSERT INTO system_owner (system_id, actor_id) VALUES (?, ?)", (system_id, owner))


def system_exists(conn: sqlite3.Connection, system_id: str) -> bool:
    return conn.execute("SELECT 1 FROM system WHERE id = ?", (system_id,)).fetchone() is not None


def list_systems(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name, description FROM system ORDER BY id").fetchall()


def owners_of(conn: sqlite3.Connection, system_id: str) -> list[str]:
    rows = conn.execute("SELECT actor_id FROM system_owner WHERE system_id = ? ORDER BY actor_id", (system_id,))
    return [r[0] for r in rows]


def add_dependency(conn: sqlite3.Connection, system_id: str, depends_on: str) -> None:
    conn.execute("INSERT OR IGNORE INTO system_dependency (system_id, depends_on) VALUES (?, ?)", (system_id, depends_on))


def dependencies_of(conn: sqlite3.Connection, system_id: str) -> list[str]:
    rows = conn.execute("SELECT depends_on FROM system_dependency WHERE system_id = ? ORDER BY depends_on", (system_id,))
    return [r[0] for r in rows]


def add_mapping(conn: sqlite3.Connection, repo: str, path_glob: str, system_id: str, created_by: str, status: str = "confirmed") -> int:
    cur = conn.execute(
        """INSERT INTO repo_mapping (repo, path_glob, system_id, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (repo, path_glob, system_id) DO UPDATE SET status = excluded.status
           RETURNING id""",
        (repo, path_glob, system_id, status, created_by, db.now()),
    )
    return int(cur.fetchone()[0])


def mappings(conn: sqlite3.Connection, repo: str | None = None, status: str | None = None) -> list[Mapping]:
    sql = "SELECT id, repo, path_glob, system_id, status FROM repo_mapping WHERE 1 = 1"
    params: list[str] = []
    if repo is not None:
        sql += " AND repo = ?"
        params.append(repo)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    rows = conn.execute(sql + " ORDER BY repo, path_glob, system_id", params)
    return [Mapping(*tuple(r)) for r in rows]


def path_matches(path: str, path_glob: str) -> bool:
    """`**` covers everything; otherwise shell-style matching where `*` also
    crosses folders, so `src/billing/**` covers `src/billing/a/b.py`."""
    if path_glob == WHOLE_REPO:
        return True
    return fnmatchcase(path, path_glob) or fnmatchcase(path, path_glob.rstrip("*").rstrip("/"))


def systems_for(conn: sqlite3.Connection, repo: str, path: str | None = None) -> list[str]:
    """Systems a repo (or one file in it) belongs to, from confirmed mappings.

    With no path, every system mapped anywhere in the repo. Shared systems a
    matched system depends on are included, since their decisions apply too.
    """
    matched: list[str] = []
    for m in mappings(conn, repo=repo, status="confirmed"):
        if (path is None or path_matches(path, m.path_glob)) and m.system_id not in matched:
            matched.append(m.system_id)
    for system_id in list(matched):
        for dep in dependencies_of(conn, system_id):
            if dep not in matched:
                matched.append(dep)
    return matched


def repos_of(conn: sqlite3.Connection, system_id: str) -> list[str]:
    rows = conn.execute("SELECT DISTINCT repo FROM repo_mapping WHERE system_id = ? AND status = 'confirmed' ORDER BY repo", (system_id,))
    return [r[0] for r in rows]
