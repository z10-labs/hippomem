"""Operations the server exposes, shared by the MCP tools and the JSON API.

Each function takes the open memory and the calling actor, does one thing,
records usage, and returns plain data or agent-ready text.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field

from hippomem import config as cfg
from hippomem import db, decisions, proposals, render, search, systems
from hippomem.policy import Actor


@dataclass(frozen=True)
class Memory:
    conn: sqlite3.Connection
    config: cfg.Config
    # One connection serves every request; this keeps requests from
    # interleaving inside a transaction.
    lock: threading.RLock = field(default_factory=threading.RLock)


def record_usage(mem: Memory, actor: Actor, action: str, repo: str = "", branch: str = "", **detail: object) -> None:
    mem.conn.execute(
        "INSERT INTO usage (actor, agent, action, repo, branch, detail, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (actor.id, actor.agent, action, repo, branch, json.dumps(detail, default=str), db.now()),
    )


def active_branch_id(conn: sqlite3.Connection, repo: str, branch: str) -> int | None:
    if not repo or not branch:
        return None
    row = conn.execute("SELECT id FROM branch WHERE repo = ? AND name = ? AND state = 'active'", (repo, branch)).fetchone()
    return int(row[0]) if row else None


def query(mem: Memory, actor: Actor, text: str, repo: str = "", path: str = "", branch: str = "", top_n: int = 5) -> str:
    with mem.lock:
        results = search.query(mem.conn, text, repo=repo or None, path=path or None, branch_id=active_branch_id(mem.conn, repo, branch), top_n=top_n)
        record_usage(
            mem,
            actor,
            "query",
            repo,
            branch,
            text=text,
            decisions=[h.decision.id for h in results.decisions],
            explored=[h.record.id for h in results.explored],
        )
        return render.results(results)


def get_decision(mem: Memory, actor: Actor, decision_id: str) -> str:
    with mem.lock:
        record_usage(mem, actor, "get", decision=decision_id)
        try:
            d = decisions.get(mem.conn, decision_id)
        except KeyError:
            return f"No decision {decision_id}."
        lines = [render.decision_full(d)]
        for ev in decisions.evidence_for(mem.conn, decision_id):
            lines.append(f"  Evidence: {ev['kind']} {ev['ref']} {ev['note']}".rstrip())
        return "\n".join(lines)


def chain(mem: Memory, actor: Actor, decision_id: str) -> str:
    with mem.lock:
        record_usage(mem, actor, "chain", decision=decision_id)
        try:
            return render.chain(search.chain(mem.conn, decision_id))
        except KeyError:
            return f"No decision {decision_id}."


def list_decisions(mem: Memory, actor: Actor, system: str = "", include_not_in_force: bool = False) -> str:
    with mem.lock:
        record_usage(mem, actor, "list", system=system)
        statuses = ("accepted", "superseded", "retired") if include_not_in_force else ("accepted",)
        found = [d for d in decisions.all_current(mem.conn, statuses) if not system or system in d.systems or d.scope == "team"]
        if not found:
            return "No decisions recorded" + (f" for {system}." if system else ".")
        return "\n".join(render.decision_line(d) for d in found)


def list_systems(mem: Memory, actor: Actor) -> str:
    with mem.lock:
        lines = []
        for s in systems.list_systems(mem.conn):
            owners = ", ".join(systems.owners_of(mem.conn, s["id"])) or "no owners"
            repos = ", ".join(f"{m.repo}:{m.path_glob}" for m in systems.mappings(mem.conn, status="confirmed") if m.system_id == s["id"])
            lines.append(f"{s['id']}  {s['name']}  owners: {owners}  repos: {repos or 'none mapped'}")
        return "\n".join(lines) or "No systems defined."


def propose_decision(
    mem: Memory,
    actor: Actor,
    *,
    title: str,
    why: str,
    statement: str = "",
    scope: str = "code",
    systems_: list[str] | None = None,
    alternatives: list[dict] | None = None,
    constraints: list[dict] | None = None,
    links: list[dict] | None = None,
    weight: str = "standard",
    supersedes: str = "",
    repo: str = "",
    branch: str = "",
    session_id: str = "",
    source: str = "agent",
) -> proposals.Proposal:
    """A decision proposed directly, outside merge reconciliation: typically
    one covering a module, service or team that no diff will ever confirm."""
    if scope not in ("code", "system", "team"):
        raise ValueError("scope must be code, system or team")
    if weight not in ("standard", "heavy"):
        raise ValueError("weight must be standard or heavy")
    payload = {
        "title": title,
        "statement": statement or title,
        "why": why,
        "scope": scope,
        "systems": systems_ or [],
        "alternatives": alternatives or [],
        "constraints": constraints or [],
        "links": links or [],
    }
    with mem.lock:
        if scope != "team" and not payload["systems"] and repo:
            payload["systems"] = systems.systems_for(mem.conn, repo)
        p = proposals.create(
            mem.conn,
            mem.config.policy,
            kind="supersede" if supersedes else "new",
            target=supersedes or None,
            payload=payload,
            source=source,
            proposed_by=actor.id,
            proposed_via=actor.agent,
            weight=weight,
            session_id=session_id,
            branch_id=active_branch_id(mem.conn, repo, branch),
        )
        record_usage(mem, actor, "propose", repo, branch, proposal=p.id, scope=scope)
        return p


def propose_mapping(mem: Memory, actor: Actor, repo: str, path_glob: str, system_id: str) -> proposals.Proposal:
    with mem.lock:
        p = proposals.create(
            mem.conn,
            mem.config.policy,
            kind="mapping",
            payload={"repo": repo, "path_glob": path_glob, "system_id": system_id},
            source="agent" if actor.agent else "engineer",
            proposed_by=actor.id,
            proposed_via=actor.agent,
        )
        record_usage(mem, actor, "propose_mapping", repo, proposal=p.id)
        return p


def open_proposals(mem: Memory, actor: Actor) -> list[proposals.Proposal]:
    with mem.lock:
        return proposals.list_by_status(mem.conn, "open")


def decide(mem: Memory, actor: Actor, proposal_id: str, accept: bool, note: str = "") -> str:
    """Accept or reject. Returns a sentence describing the outcome; refusals
    are returned, not raised, so an agent can relay them."""
    with mem.lock:
        try:
            if accept:
                decision_id = proposals.accept(mem.conn, mem.config.policy, proposal_id, actor, note)
                outcome = f"Accepted {proposal_id}" + (f": {decision_id} is now in force." if decision_id else ".")
            else:
                proposals.reject(mem.conn, proposal_id, actor, note)
                outcome = f"Rejected {proposal_id}."
        except proposals.Refused as refused:
            outcome = f"Not allowed: {refused}"
        except KeyError:
            outcome = f"No proposal {proposal_id}."
        record_usage(mem, actor, "decide", proposal=proposal_id, accept=accept, outcome=outcome)
        return outcome
