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
from hippomem import db, decisions, drafts, lifecycle, proposals, render, search, sessions, systems
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


# --- sessions, drafts and git events (phase 4) ---------------------------------


def _resolve(mem: Memory, actor: Actor, repo: str, branch: str, session_id: str) -> tuple[str, str, str]:
    """Fill in repo, branch and session from the session when a tool call left them out."""
    found = None
    if session_id:
        try:
            found = sessions.get(mem.conn, session_id)
        except KeyError:
            found = None
    if found is None and not (repo and branch):
        found = sessions.latest_for(mem.conn, actor, repo)
    if found is not None:
        repo = repo or found.repo
        if not branch and found.branch_id:
            branch = drafts.get_branch(mem.conn, found.branch_id).name
        session_id = session_id or found.id
    return repo, branch, session_id


def session_start(mem: Memory, actor: Actor, session_id: str, repo: str, branch: str) -> str:
    with mem.lock:
        session = sessions.start(mem.conn, actor, session_id, repo, branch)
        text = sessions.digest(mem.conn, mem.config.policy, actor, session, mem.config.max_per_session)
        record_usage(mem, actor, "session_start", repo, branch, session=session_id)
        return text


def lookup(mem: Memory, actor: Actor, session_id: str, repo: str, path: str, history: dict | None = None) -> str:
    with mem.lock:
        text = sessions.lookup(mem.conn, mem.config.policy, actor, session_id, repo, path, history)
        record_usage(mem, actor, "lookup", repo, session=session_id, path=path, hit=bool(text))
        return text


def turn_start(mem: Memory, actor: Actor, session_id: str) -> None:
    with mem.lock:
        sessions.turn_start(mem.conn, session_id)


def turn_stop(mem: Memory, actor: Actor, session_id: str, changed: list[str]) -> str:
    with mem.lock:
        reason = sessions.turn_stop(mem.conn, session_id, changed)
        record_usage(mem, actor, "turn_stop", session=session_id, changed=changed, nudged=bool(reason))
        return reason


def draft(
    mem: Memory,
    actor: Actor,
    *,
    title: str,
    why: str,
    kind: str = "decided",
    statement: str = "",
    scope: str = "code",
    system: str = "",
    paths: list[str] | None = None,
    replaces: int | None = None,
    replaced_because: str = "",
    repo: str = "",
    branch: str = "",
    session_id: str = "",
) -> str:
    with mem.lock:
        repo, branch, session_id = _resolve(mem, actor, repo, branch, session_id)
        if not repo or not branch:
            raise ValueError("pass repo and branch (they're in the session digest)")
        branch_id = drafts.ensure_branch(mem.conn, repo, branch)
        draft_id = drafts.add(
            mem.conn,
            branch_id,
            title=title,
            why=why,
            engineer=actor.id,
            agent=actor.agent,
            session_id=session_id,
            kind=kind,
            statement=statement,
            scope=scope,
            system_id=system or None,
            paths=tuple(paths or ()),
            replaces=replaces,
            replaced_because=replaced_because,
        )
        record_usage(mem, actor, "draft", repo, branch, draft=draft_id, replaces=replaces, scope=scope)
        note = f" Draft #{replaces} is now dropped." if replaces else ""
        return f"Drafted #{draft_id} on {repo}:{branch}.{note} It becomes a proposal only if the merge shows it was built."


def drop(mem: Memory, actor: Actor, draft_id: int, why: str) -> str:
    with mem.lock:
        drafts.drop(mem.conn, draft_id, why)
        record_usage(mem, actor, "drop", draft=draft_id)
        return f"Dropped draft #{draft_id}."


def trail(mem: Memory, actor: Actor, repo: str = "", branch: str = "", session_id: str = "") -> str:
    with mem.lock:
        repo, branch, session_id = _resolve(mem, actor, repo, branch, session_id)
        found = drafts.find_active(mem.conn, repo, branch)
        if found is None:
            return f"No drafts on {repo}:{branch} yet."
        lines = [sessions._trail_line(d) for d in drafts.trail(mem.conn, found.id)]
        return "\n".join(lines) or f"No drafts on {repo}:{branch} yet."


def ask_author(mem: Memory, actor: Actor, repo: str, path: str, question: str, history: dict) -> str:
    with mem.lock:
        text = sessions.ask_author(mem.conn, actor, repo, path, question, history)
        record_usage(mem, actor, "ask_author", repo, path=path)
        return text


def answer_question(mem: Memory, actor: Actor, question_id: str, title: str, why: str, alternatives: list[dict] | None = None) -> str:
    with mem.lock:
        p = sessions.answer(mem.conn, mem.config.policy, actor, question_id, title, why, alternatives)
        record_usage(mem, actor, "answer", question=question_id, proposal=p.id)
        return f"Recorded as proposal {p.id}; another engineer or a reviewer accepts it."


def git_event(mem: Memory, actor: Actor, body: dict) -> dict:
    kind = body.get("kind", "")
    repo = body.get("repo", "")
    with mem.lock:
        record_usage(mem, actor, f"event:{kind}", repo, body.get("branch", ""))
        if kind == "checkout":
            return lifecycle.on_checkout(mem.conn, repo, body.get("branch", ""))
        if kind == "commit":
            return lifecycle.on_commit(
                mem.conn,
                mem.config.policy,
                repo,
                body.get("branch", ""),
                body["commit"],
                list(body.get("files", [])),
                body.get("diff", ""),
                body.get("default_branch", ""),
            )
        if kind == "merge":
            return lifecycle.on_merge(
                mem.conn, mem.config.policy, repo, body["branch"], body.get("into", ""), body.get("diff", ""), list(body.get("commits", []))
            )
        if kind == "close":
            return lifecycle.on_close(mem.conn, repo, body["branch"])
        if kind == "close_stale":
            return {"closed": lifecycle.close_stale(mem.conn, mem.config.stale_after_days)}
        raise ValueError(f"unknown event kind {kind!r}")
