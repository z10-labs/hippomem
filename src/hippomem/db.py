"""SQLite storage: connection, schema migrations and small shared helpers.

The whole memory is one SQLite file. Migrations are numbered and applied in
order, tracked with PRAGMA user_version. Never edit a migration that has
shipped; add a new one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE actor (
        id          TEXT PRIMARY KEY,            -- '@alex', 'watchman'
        kind        TEXT NOT NULL CHECK (kind IN ('engineer', 'reviewer', 'admin')),
        name        TEXT NOT NULL,
        token_hash  TEXT UNIQUE,
        created_at  TEXT NOT NULL
    );

    CREATE TABLE system (
        id          TEXT PRIMARY KEY,            -- slug: 'billing'
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    );

    CREATE TABLE system_owner (
        system_id   TEXT NOT NULL REFERENCES system(id),
        actor_id    TEXT NOT NULL REFERENCES actor(id),
        PRIMARY KEY (system_id, actor_id)
    );

    -- A system can depend on another (an app on a shared UI kit).
    CREATE TABLE system_dependency (
        system_id   TEXT NOT NULL REFERENCES system(id),
        depends_on  TEXT NOT NULL REFERENCES system(id),
        PRIMARY KEY (system_id, depends_on)
    );

    -- repo + path glob -> system. One system can span many repos.
    CREATE TABLE repo_mapping (
        id          INTEGER PRIMARY KEY,
        repo        TEXT NOT NULL,
        path_glob   TEXT NOT NULL,
        system_id   TEXT NOT NULL REFERENCES system(id),
        status      TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed')),
        created_by  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (repo, path_glob, system_id)
    );

    CREATE TABLE branch (
        id               INTEGER PRIMARY KEY,
        repo             TEXT NOT NULL,
        name             TEXT NOT NULL,
        state            TEXT NOT NULL CHECK (state IN ('active', 'merged', 'closed')),
        created_at       TEXT NOT NULL,
        last_activity_at TEXT NOT NULL,
        ended_at         TEXT,
        head_commit      TEXT
    );
    -- A branch name can be reused once the earlier branch has ended.
    CREATE UNIQUE INDEX branch_active ON branch (repo, name) WHERE state = 'active';

    CREATE TABLE draft (
        id              INTEGER PRIMARY KEY,
        branch_id       INTEGER NOT NULL REFERENCES branch(id),
        kind            TEXT NOT NULL CHECK (kind IN ('decided', 'reversed', 'considering')),
        title           TEXT NOT NULL,
        statement       TEXT NOT NULL DEFAULT '',
        why             TEXT NOT NULL DEFAULT '',
        reverses        INTEGER REFERENCES draft(id),
        scope           TEXT NOT NULL CHECK (scope IN ('code', 'system', 'team')),
        system_id       TEXT REFERENCES system(id),
        paths           TEXT NOT NULL DEFAULT '[]',   -- JSON list of repo-relative paths
        engineer        TEXT NOT NULL,
        agent           TEXT NOT NULL DEFAULT '',
        session_id      TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL CHECK (status IN ('open', 'backed', 'proposed', 'rejected_alternative', 'dropped', 'explored')),
        backed_by       TEXT,                         -- commit sha
        created_at      TEXT NOT NULL,
        embedding       BLOB
    );

    CREATE TABLE decision (
        id              TEXT PRIMARY KEY,             -- 'D-0001'
        current_version INTEGER NOT NULL,
        status          TEXT NOT NULL CHECK (status IN ('accepted', 'superseded', 'retired')),
        created_at      TEXT NOT NULL
    );

    CREATE TABLE decision_version (
        decision_id     TEXT NOT NULL REFERENCES decision(id),
        version         INTEGER NOT NULL,
        title           TEXT NOT NULL,
        statement       TEXT NOT NULL,
        why             TEXT NOT NULL,
        trade_off       TEXT NOT NULL DEFAULT '',
        weight          TEXT NOT NULL CHECK (weight IN ('standard', 'heavy')),
        scope           TEXT NOT NULL CHECK (scope IN ('code', 'system', 'team')),
        implemented     INTEGER NOT NULL DEFAULT 0,
        valid_from      TEXT NOT NULL,
        recorded_at     TEXT NOT NULL,
        made_by         TEXT NOT NULL,                -- engineer
        made_via        TEXT NOT NULL DEFAULT '',     -- agent
        accepted_by     TEXT NOT NULL,
        accepted_by_kind TEXT NOT NULL CHECK (accepted_by_kind IN ('engineer', 'agent', 'rule')),
        proposal_id     TEXT NOT NULL,
        embedding       BLOB,
        PRIMARY KEY (decision_id, version)
    );

    CREATE TABLE decision_system (
        decision_id TEXT NOT NULL REFERENCES decision(id),
        system_id   TEXT NOT NULL REFERENCES system(id),
        PRIMARY KEY (decision_id, system_id)
    );

    CREATE TABLE anchor (
        decision_id TEXT NOT NULL REFERENCES decision(id),
        repo        TEXT NOT NULL,
        path_glob   TEXT NOT NULL,
        PRIMARY KEY (decision_id, repo, path_glob)
    );

    CREATE TABLE alternative (
        id          INTEGER PRIMARY KEY,
        decision_id TEXT NOT NULL REFERENCES decision(id),
        option      TEXT NOT NULL,
        reason      TEXT NOT NULL,
        from_draft  INTEGER REFERENCES draft(id)
    );

    CREATE TABLE constraint_ (
        id          INTEGER PRIMARY KEY,
        decision_id TEXT NOT NULL REFERENCES decision(id),
        text        TEXT NOT NULL,
        kind        TEXT NOT NULL CHECK (kind IN ('technical', 'org', 'external')),
        revisit_if  TEXT NOT NULL DEFAULT ''
    );

    -- Directed links between decisions. Inverses are computed when read.
    CREATE TABLE link (
        from_id     TEXT NOT NULL REFERENCES decision(id),
        type        TEXT NOT NULL CHECK (type IN ('depends-on', 'supersedes', 'conflicts-with')),
        to_id       TEXT NOT NULL REFERENCES decision(id),
        PRIMARY KEY (from_id, type, to_id)
    );

    CREATE TABLE explored (
        id          TEXT PRIMARY KEY,                 -- 'X-0001'
        title       TEXT NOT NULL,
        tried       TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        why         TEXT NOT NULL,
        revisit_if  TEXT NOT NULL,
        system_id   TEXT REFERENCES system(id),
        repo        TEXT NOT NULL DEFAULT '',
        branch_id   INTEGER REFERENCES branch(id),
        promoted_to TEXT REFERENCES decision(id),
        created_at  TEXT NOT NULL,
        embedding   BLOB
    );

    CREATE TABLE proposal (
        id              TEXT PRIMARY KEY,             -- 'P-0001'
        kind            TEXT NOT NULL CHECK (kind IN ('new', 'evidence', 'supersede', 'promote', 'mapping')),
        target          TEXT,                         -- decision or explored id
        payload         TEXT NOT NULL,                -- JSON
        weight          TEXT NOT NULL CHECK (weight IN ('standard', 'heavy')),
        source          TEXT NOT NULL CHECK (source IN ('reconciliation', 'scope', 'author_answer', 'agent', 'engineer')),
        status          TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'rejected', 'withdrawn')),
        proposed_by     TEXT NOT NULL,                -- engineer
        proposed_via    TEXT NOT NULL DEFAULT '',     -- agent
        session_id      TEXT NOT NULL DEFAULT '',
        branch_id       INTEGER REFERENCES branch(id),
        created_at      TEXT NOT NULL,
        decided_at      TEXT,
        decided_by      TEXT,
        decided_by_kind TEXT CHECK (decided_by_kind IN ('engineer', 'agent', 'rule')),
        note            TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE evidence (
        id          INTEGER PRIMARY KEY,
        decision_id TEXT REFERENCES decision(id),
        proposal_id TEXT REFERENCES proposal(id),
        kind        TEXT NOT NULL CHECK (kind IN ('commit', 'pr', 'branch', 'session', 'engineer_answer', 'doc')),
        ref         TEXT NOT NULL,
        note        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    );

    -- A question for the engineer who wrote some code, asked when an agent is
    -- about to change it and no decision covers it.
    CREATE TABLE question (
        id          TEXT PRIMARY KEY,                 -- 'Q-0001'
        asked_by    TEXT NOT NULL,
        asked_via   TEXT NOT NULL DEFAULT '',
        to_engineer TEXT NOT NULL,
        repo        TEXT NOT NULL,
        path        TEXT NOT NULL,
        context     TEXT NOT NULL,                    -- JSON: commits, PR, date
        text        TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('open', 'answered', 'dismissed')),
        answer      TEXT NOT NULL DEFAULT '',
        proposal_id TEXT REFERENCES proposal(id),
        created_at  TEXT NOT NULL,
        answered_at TEXT
    );

    CREATE TABLE session (
        id               TEXT PRIMARY KEY,
        engineer         TEXT NOT NULL,
        agent            TEXT NOT NULL DEFAULT '',
        repo             TEXT NOT NULL,
        branch_id        INTEGER REFERENCES branch(id),
        started_at       TEXT NOT NULL,
        turn_started_at  TEXT,
        turn_drafts      INTEGER NOT NULL DEFAULT 0,
        stop_nudged      INTEGER NOT NULL DEFAULT 0
    );

    -- Which proposals were raised with which engineer in which session, so
    -- agents respect the per-session cap and don't repeat themselves.
    CREATE TABLE surfaced (
        proposal_id TEXT NOT NULL REFERENCES proposal(id),
        engineer    TEXT NOT NULL,
        session_id  TEXT NOT NULL,
        at          TEXT NOT NULL,
        PRIMARY KEY (proposal_id, session_id)
    );

    CREATE TABLE event (
        id      INTEGER PRIMARY KEY,
        kind    TEXT NOT NULL,
        repo    TEXT NOT NULL DEFAULT '',
        branch  TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        at      TEXT NOT NULL
    );

    -- Every tool call and hook, for measuring how agents use the memory.
    CREATE TABLE usage (
        id       INTEGER PRIMARY KEY,
        actor    TEXT NOT NULL,
        agent    TEXT NOT NULL DEFAULT '',
        action   TEXT NOT NULL,
        repo     TEXT NOT NULL DEFAULT '',
        branch   TEXT NOT NULL DEFAULT '',
        detail   TEXT NOT NULL DEFAULT '{}',
        at       TEXT NOT NULL
    );

    CREATE TABLE counter (
        prefix  TEXT PRIMARY KEY,
        value   INTEGER NOT NULL
    );
    """,
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Replaced in tests to make time deterministic.
now: Callable[[], str] = utc_now


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the memory file, creating and migrating it if needed."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {number}; COMMIT;")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`with transaction(conn):` runs the block in one write transaction.

    BEGIN IMMEDIATE takes the write lock up front, so two writers never both
    read state and then race to write it. Nested use joins the outer
    transaction.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    """Human-friendly sequential ids: D-0001, P-0001, X-0001, Q-0001."""
    row = conn.execute(
        "INSERT INTO counter (prefix, value) VALUES (?, 1) ON CONFLICT(prefix) DO UPDATE SET value = value + 1 RETURNING value",
        (prefix,),
    ).fetchone()
    return f"{prefix}-{row[0]:04d}"
