"""Search: similarity, then the decision graph.

A query is embedded and compared with every decision, explored record and
open draft. Decisions above a relevance floor are expanded one hop along their
links in both directions, so a result comes with what it depends on and what
depends on it, including whatever superseded it. Superseded decisions are
ranked lower and flagged, never hidden. Explored records and other branches'
drafts come back in their own groups so they can't be mistaken for decisions.

Scope is a ranking signal, not a filter: decisions outside the systems of the
caller's repo or file are scored lower, but still returned when they match
well. In a single team a hard filter costs more in missed matches than it
saves in noise (BD-14).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from hippomem import decisions, embedding, explored, systems

# Calibrated in the proof of concept against all-MiniLM-L6-v2: queries with a
# genuine answer scored 0.48-0.80 against it, queries without one topped out
# at 0.25.
MIN_DIRECT_SCORE = 0.30
SOFT_RELATED_THRESHOLD = 0.50
NOT_IN_FORCE_MULTIPLIER = 0.5
OUT_OF_SCOPE_MULTIPLIER = 0.8
MAX_EXPLORED = 3
MAX_DRAFTS = 3


@dataclass(frozen=True)
class DecisionHit:
    decision: decisions.Decision
    score: float
    via: str  # "direct", "related", or a link label such as "depended-on-by D-0003"
    in_scope: bool


@dataclass(frozen=True)
class ExploredHit:
    record: explored.Explored
    score: float


@dataclass(frozen=True)
class DraftHit:
    draft_id: int
    repo: str
    branch: str
    title: str
    statement: str
    why: str
    kind: str  # decided | considering | dropped
    engineer: str
    own_branch: bool
    score: float


@dataclass(frozen=True)
class Results:
    decisions: tuple[DecisionHit, ...] = ()
    explored: tuple[ExploredHit, ...] = ()
    drafts: tuple[DraftHit, ...] = ()
    scope: tuple[str, ...] = field(default=())

    @property
    def empty(self) -> bool:
        return not (self.decisions or self.explored or self.drafts)


def query(
    conn: sqlite3.Connection,
    text: str,
    *,
    repo: str | None = None,
    path: str | None = None,
    branch_id: int | None = None,
    top_n: int = 5,
) -> Results:
    q = embedding.normalized(text)
    scope = tuple(systems.systems_for(conn, repo, path)) if repo else ()
    return Results(
        decisions=tuple(_decision_hits(conn, q, repo, scope, top_n)),
        explored=tuple(_explored_hits(conn, q)),
        drafts=tuple(_draft_hits(conn, q, branch_id)),
        scope=scope,
    )


def _scores(blobs: list[bytes], q: np.ndarray) -> np.ndarray:
    if not blobs:
        return np.zeros(0, dtype=np.float32)
    matrix = np.vstack([embedding.from_blob(b) for b in blobs])
    return matrix @ q


def _in_scope(conn: sqlite3.Connection, decision_id: str, repo: str | None, scope: tuple[str, ...]) -> bool:
    if repo is None:
        return True
    row = conn.execute(
        """SELECT v.scope = 'team'
                  OR EXISTS (SELECT 1 FROM anchor a WHERE a.decision_id = d.id AND a.repo = ?)
                  OR EXISTS (SELECT 1 FROM decision_system s WHERE s.decision_id = d.id AND s.system_id IN (SELECT value FROM json_each(?)))
           FROM decision d JOIN decision_version v ON v.decision_id = d.id AND v.version = d.current_version
           WHERE d.id = ?""",
        (repo, _json_list(scope), decision_id),
    ).fetchone()
    return bool(row and row[0])


def _json_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values))


def _decision_hits(conn: sqlite3.Connection, q: np.ndarray, repo: str | None, scope: tuple[str, ...], top_n: int) -> list[DecisionHit]:
    rows = decisions.embeddings(conn)
    if not rows:
        return []
    raw = _scores([r[2] for r in rows], q)
    scored = []
    for (decision_id, status, _), raw_score in zip(rows, raw.tolist()):
        in_scope = _in_scope(conn, decision_id, repo, scope)
        effective = raw_score
        if status != "accepted":
            effective *= NOT_IN_FORCE_MULTIPLIER
        if not in_scope:
            effective *= OUT_OF_SCOPE_MULTIPLIER
        scored.append((decision_id, raw_score, effective, in_scope))
    scored.sort(key=lambda t: t[2], reverse=True)

    # The floor applies to the raw score: a superseded or out-of-scope
    # decision is only ever dropped for being irrelevant, never for its status.
    top = [t for t in scored if t[1] >= MIN_DIRECT_SCORE][:top_n]
    hits = [DecisionHit(decisions.get(conn, d), eff, "direct", in_scope) for d, _, eff, in_scope in top]
    seen = {h.decision.id for h in hits}

    for hit in list(hits):
        for link in hit.decision.links:
            if link.other in seen:
                continue
            seen.add(link.other)
            linked = decisions.get(conn, link.other)
            hits.append(DecisionHit(linked, 0.0, f"{link.type} {hit.decision.id}", _in_scope(conn, linked.id, repo, scope)))

    for decision_id, _, effective, in_scope in scored:
        if effective < SOFT_RELATED_THRESHOLD:
            break
        if decision_id in seen:
            continue
        seen.add(decision_id)
        hits.append(DecisionHit(decisions.get(conn, decision_id), effective, "related", in_scope))
    return hits


def _explored_hits(conn: sqlite3.Connection, q: np.ndarray) -> list[ExploredHit]:
    rows = explored.embeddings(conn)
    raw = _scores([r[1] for r in rows], q)
    ranked = sorted(((rid, s) for (rid, _), s in zip(rows, raw.tolist()) if s >= MIN_DIRECT_SCORE), key=lambda t: t[1], reverse=True)
    return [ExploredHit(explored.get(conn, rid), score) for rid, score in ranked[:MAX_EXPLORED]]


def _draft_hits(conn: sqlite3.Connection, q: np.ndarray, branch_id: int | None) -> list[DraftHit]:
    rows = conn.execute(
        """SELECT d.id, b.id AS branch_id, b.repo, b.name, d.title, d.statement, d.why, d.kind, d.status, d.dropped_reason, d.engineer, d.embedding
           FROM draft d JOIN branch b ON b.id = d.branch_id
           WHERE b.state = 'active' AND d.status IN ('open', 'dropped') AND d.embedding IS NOT NULL
           ORDER BY d.id"""
    ).fetchall()
    raw = _scores([r["embedding"] for r in rows], q)
    hits = [
        DraftHit(
            draft_id=r["id"],
            repo=r["repo"],
            branch=r["name"],
            title=r["title"],
            statement=r["statement"],
            why=r["dropped_reason"] if r["status"] == "dropped" else r["why"],
            kind="dropped" if r["status"] == "dropped" else r["kind"],
            engineer=r["engineer"],
            own_branch=r["branch_id"] == branch_id,
            score=s,
        )
        for r, s in zip(rows, raw.tolist())
        if s >= MIN_DIRECT_SCORE
    ]
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:MAX_DRAFTS]


def chain(conn: sqlite3.Connection, decision_id: str, max_depth: int = 5) -> dict:
    """What a decision depends on (outbound) and what would be affected if it
    changed (inbound), followed transitively."""
    root = decisions.get(conn, decision_id)

    def walk(current: str, direction: str, depth: int, seen: set[str]) -> list[dict]:
        if depth >= max_depth:
            return []
        if direction == "out":
            rows = conn.execute("SELECT type, to_id FROM link WHERE from_id = ? ORDER BY to_id", (current,)).fetchall()
        else:
            rows = conn.execute("SELECT type, from_id FROM link WHERE to_id = ? ORDER BY from_id", (current,)).fetchall()
        nodes = []
        for link_type, other in rows:
            if other in seen:
                continue
            seen.add(other)
            label = link_type if direction == "out" else decisions.INVERSE[link_type]
            d = decisions.get(conn, other)
            nodes.append({"id": other, "title": d.title, "status": d.status, "link": label, "children": walk(other, direction, depth + 1, seen)})
        return nodes

    return {
        "id": root.id,
        "title": root.title,
        "status": root.status,
        "depends_on": walk(root.id, "out", 0, {root.id}),
        "affects": walk(root.id, "in", 0, {root.id}),
    }
