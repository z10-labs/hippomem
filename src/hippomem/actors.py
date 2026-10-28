"""Actors and their tokens.

Engineers, reviewer agents and admins each get a bearer token. Only its
SHA-256 hash is stored. A coding agent uses its engineer's token and names
itself in the `X-Hippomem-Agent` header, so drafts read `@alex via claude-code`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3

from hippomem import db
from hippomem.policy import Actor

KINDS = ("engineer", "reviewer", "admin")
TOKEN_PREFIX = "hm_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add(conn: sqlite3.Connection, actor_id: str, kind: str, name: str = "", emails: tuple[str, ...] = ()) -> str:
    """Create an actor and return its token. The token is shown once."""
    if kind not in KINDS:
        raise ValueError(f"actor kind must be one of {', '.join(KINDS)}")
    if kind in ("engineer", "admin") and not actor_id.startswith("@"):
        raise ValueError("engineer ids start with '@', e.g. @alex")
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO actor (id, kind, name, token_hash, emails, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor_id, kind, name or actor_id, _hash(token), ",".join(e.strip().lower() for e in emails), db.now()),
    )
    return token


def rotate(conn: sqlite3.Connection, actor_id: str) -> str:
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    cur = conn.execute("UPDATE actor SET token_hash = ? WHERE id = ?", (_hash(token), actor_id))
    if cur.rowcount == 0:
        raise KeyError(actor_id)
    return token


def get(conn: sqlite3.Connection, actor_id: str) -> Actor | None:
    row = conn.execute("SELECT id, kind FROM actor WHERE id = ?", (actor_id,)).fetchone()
    return Actor(row[0], row[1]) if row else None


def from_token(conn: sqlite3.Connection, token: str, agent: str = "") -> Actor | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = conn.execute("SELECT id, kind FROM actor WHERE token_hash = ?", (_hash(token),)).fetchone()
    return Actor(row[0], row[1], agent) if row else None


def by_email(conn: sqlite3.Connection, email: str) -> str | None:
    """The engineer whose git author email this is, if anyone registered it."""
    wanted = email.strip().lower()
    for actor_id, emails in conn.execute("SELECT id, emails FROM actor WHERE emails != ''"):
        if wanted in emails.split(","):
            return str(actor_id)
    return None


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, kind, name, created_at FROM actor ORDER BY kind, id").fetchall()
