"""What an agent's session sees without asking: the digest at session start,
the decisions covering a file before it's edited, the end-of-turn check, and
the proposals and questions brought to its engineer.

None of this reads transcripts (BD-09). The end-of-turn check uses only the
files changed during the turn and the drafts recorded.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from fnmatch import fnmatchcase

from hippomem import actors, db, decisions, drafts, proposals, render, systems
from hippomem.policy import Actor, Policy, refusal

MAX_DIGEST_DECISIONS = 15
MAX_OTHER_BRANCH_DRAFTS = 5
MIN_COMMITS_TO_ASK_AUTHOR = 3

# Files whose change usually means a decision was made: dependencies, schema,
# infrastructure, service wiring.
DECISION_SHAPED = (
    "package.json",
    "pyproject.toml",
    "requirements*.txt",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "build.gradle*",
    "*.lock",
    "**/migrations/**",
    "**/migration/**",
    "*schema*",
    "*.sql",
    "*.prisma",
    "docker-compose*",
    "Dockerfile*",
    "*.tf",
    "**/k8s/**",
    "**/helm/**",
    ".github/workflows/*",
    "**/config/**",
)


@dataclass(frozen=True)
class Session:
    id: str
    engineer: str
    agent: str
    repo: str
    branch_id: int | None
    turn_started_at: str | None
    stop_nudged: bool


def start(conn: sqlite3.Connection, actor: Actor, session_id: str, repo: str, branch: str) -> Session:
    branch_id = drafts.ensure_branch(conn, repo, branch) if branch and branch != "HEAD" else None
    now = db.now()
    conn.execute(
        """INSERT INTO session (id, engineer, agent, repo, branch_id, started_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET repo = excluded.repo, branch_id = excluded.branch_id""",
        (session_id, actor.id, actor.agent, repo, branch_id, now),
    )
    return get(conn, session_id)


def get(conn: sqlite3.Connection, session_id: str) -> Session:
    row = conn.execute(
        "SELECT id, engineer, agent, repo, branch_id, turn_started_at, stop_nudged FROM session WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown session {session_id}")
    return Session(row[0], row[1], row[2], row[3], row[4], row[5], bool(row[6]))


def latest_for(conn: sqlite3.Connection, actor: Actor, repo: str = "") -> Session | None:
    """The actor's most recent session, for tool calls that didn't name one."""
    sql = "SELECT id FROM session WHERE engineer = ? AND agent = ?"
    params: list[str] = [actor.id, actor.agent]
    if repo:
        sql += " AND repo = ?"
        params.append(repo)
    row = conn.execute(sql + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params).fetchone()
    return get(conn, row[0]) if row else None


# --- digest -------------------------------------------------------------------


def digest(conn: sqlite3.Connection, policy: Policy, actor: Actor, session: Session, max_proposals: int) -> str:
    branch = drafts.get_branch(conn, session.branch_id) if session.branch_id else None
    scope = systems.systems_for(conn, session.repo)
    lines = [
        f"Hippomem: this team's engineering memory. Repo {session.repo!r}"
        + (f", branch {branch.name!r}" if branch else "")
        + (f", systems: {', '.join(scope)}." if scope else ". This repo isn't mapped to a system yet; hippomem_propose_mapping can suggest one."),
        f'Pass repo="{session.repo}"' + (f' branch="{branch.name}"' if branch else "") + f' session="{session.id}" to hippomem tools.',
        "Query before non-trivial choices; draft decisions and reversals (with the reason) as you make them.",
    ]

    in_force = [d for d in decisions.all_current(conn, ("accepted",)) if d.scope == "team" or set(d.systems) & set(scope)]
    if in_force:
        lines.append("")
        lines.append(f"Decisions in force here ({len(in_force)}):")
        lines.extend(render.decision_line(d) for d in in_force[:MAX_DIGEST_DECISIONS])
        if len(in_force) > MAX_DIGEST_DECISIONS:
            lines.append(f"... and {len(in_force) - MAX_DIGEST_DECISIONS} more: hippomem_list or hippomem_query")

    if branch:
        trail = drafts.trail(conn, branch.id)
        if trail:
            lines.append("")
            lines.append("Earlier on this branch (drafts, not yet decisions):")
            lines.extend(_trail_line(d) for d in trail)

    others = _other_branch_drafts(conn, session.repo, scope, branch.id if branch else None)
    if others:
        lines.append("")
        lines.append("In progress on other branches touching the same systems (not decided):")
        lines.extend(others)

    asks = _surface(conn, policy, actor, session, max_proposals)
    questions = _open_questions(conn, actor.id)
    if asks or questions:
        lines.append("")
        lines.append("Waiting for your engineer. At a natural pause (not mid-task), ask them; then record the answer:")
        lines.extend(asks)
        lines.extend(questions)
    return "\n".join(lines)


def _trail_line(d: drafts.Draft) -> str:
    if d.status == "dropped":
        return f"#{d.id} dropped: {d.title}. Because: {d.dropped_reason}"
    why = f". Why: {d.why}" if d.why else ""
    return f"#{d.id} {d.kind}: {d.title}{why}"


def _other_branch_drafts(conn: sqlite3.Connection, repo: str, scope: list[str], own_branch: int | None) -> list[str]:
    rows = conn.execute(
        """SELECT d.id, d.title, d.status, d.dropped_reason, d.kind, d.system_id, d.engineer, b.repo, b.name
           FROM draft d JOIN branch b ON b.id = d.branch_id
           WHERE b.state = 'active' AND d.status IN ('open', 'dropped') AND b.id IS NOT ?
           ORDER BY d.id DESC""",
        (own_branch,),
    ).fetchall()
    out = []
    for r in rows:
        draft_scope = [r["system_id"]] if r["system_id"] else systems.systems_for(conn, r["repo"])
        if r["repo"] != repo and not set(draft_scope) & set(scope):
            continue
        what = f"dropped {r['title']} ({r['dropped_reason']})" if r["status"] == "dropped" else f"{r['kind']}: {r['title']}"
        out.append(f"{r['repo']}:{r['name']} ({r['engineer']}) {what}")
        if len(out) >= MAX_OTHER_BRANCH_DRAFTS:
            break
    return out


# --- surfacing proposals --------------------------------------------------------


def _surface(conn: sqlite3.Connection, policy: Policy, actor: Actor, session: Session, limit: int) -> list[str]:
    """Up to `limit` open proposals this engineer could decide, most relevant
    first: their systems, then code they wrote, then anything. Never their own,
    never one already raised in this session."""
    if limit <= 0:
        return []
    already = conn.execute("SELECT COUNT(*) FROM surfaced WHERE session_id = ?", (session.id,)).fetchone()[0]
    room = limit - already
    if room <= 0:
        return []
    owned = {r[0] for r in conn.execute("SELECT system_id FROM system_owner WHERE actor_id = ?", (actor.id,))}
    mine = set(systems.systems_for(conn, session.repo))
    candidates = []
    for p in proposals.list_by_status(conn, "open"):
        if conn.execute("SELECT 1 FROM surfaced WHERE proposal_id = ? AND session_id = ?", (p.id, session.id)).fetchone():
            continue
        engineer = Actor(actor.id, actor.kind)
        if refusal(conn, policy, engineer, kind=p.kind, weight=p.weight, payload=p.payload, proposed_by=p.proposed_by, proposed_via=p.proposed_via):
            continue
        rank = 0 if set(p.systems) & owned else 1 if set(p.systems) & mine else 2
        candidates.append((rank, p.id, p))
    candidates.sort(key=lambda t: (t[0], t[1]))
    picked = [p for _, _, p in candidates[:room]]
    for p in picked:
        conn.execute("INSERT INTO surfaced (proposal_id, engineer, session_id, at) VALUES (?, ?, ?, ?)", (p.id, actor.id, session.id, db.now()))
    return [f"- Proposal {render.proposal_full(p)}\n  (hippomem_decide proposal_id={p.id!r} accept=true|false after they answer)" for p in picked]


def urgent_proposals(conn: sqlite3.Connection, policy: Policy, actor: Actor, repo: str, path: str) -> list[proposals.Proposal]:
    """Open proposals about the file being edited: raised immediately, not at a pause."""
    out = []
    for p in proposals.list_by_status(conn, "open"):
        anchors = p.payload.get("anchors", ())
        if any(a.get("repo") == repo and systems.path_matches(path, a.get("path_glob", "")) for a in anchors):
            out.append(p)
    return out


# --- lookup before an edit --------------------------------------------------------


def lookup(conn: sqlite3.Connection, policy: Policy, actor: Actor, session_id: str, repo: str, path: str, history: dict | None = None) -> str:
    """Decisions covering a file, for the agent to read before editing it."""
    path_systems = systems.systems_for(conn, repo, path)
    anchored, system_wide = [], []
    for d in decisions.all_current(conn, ("accepted",)):
        if any(a.repo == repo and systems.path_matches(path, a.path_glob) for a in d.anchors):
            anchored.append(d)
        elif d.scope in ("system", "team") and (d.scope == "team" or set(d.systems) & set(path_systems)):
            system_wide.append(d)

    lines: list[str] = []
    if anchored:
        lines.append(f"Decisions covering {path}:")
        for d in anchored:
            lines.append(render.decision_line(d))
            lines.extend(render.decision_body(d))
    if system_wide:
        lines.append(f"Rules for {', '.join(path_systems) or 'the whole team'} that apply here:")
        lines.extend(render.decision_line(d) + f"  Why: {d.why}" for d in system_wide)

    urgent = urgent_proposals(conn, policy, actor, repo, path)
    if urgent:
        lines.append("Open proposals about this file. Raise these with your engineer now:")
        lines.extend(render.proposal_full(p) for p in urgent)

    if not anchored and history and _should_suggest_asking(conn, actor, session_id, repo, path, history):
        author = actors.by_email(conn, history["top_author_email"]) or f"{history['top_author_name']} <{history['top_author_email']}>"
        last = history.get("last", {})
        lines.append(
            f"No recorded decision covers {path}. It has {history['commits']} commits, mostly by {author}"
            + (f"; last change {last.get('date')} '{last.get('subject')}'" if last else "")
            + ". If you're about to change how it works, not just add to it, ask its author why it's built this way:"
            f' hippomem_ask_author(repo="{repo}", path="{path}", question=...).'
        )
        conn.execute(
            "INSERT INTO usage (actor, agent, action, repo, detail, at) VALUES (?, ?, 'ask_author_hint', ?, ?, ?)",
            (actor.id, actor.agent, repo, json.dumps({"session": session_id, "path": path, "history": history}), db.now()),
        )
    return "\n".join(lines)


def _should_suggest_asking(conn: sqlite3.Connection, actor: Actor, session_id: str, repo: str, path: str, history: dict) -> bool:
    if int(history.get("commits", 0)) < MIN_COMMITS_TO_ASK_AUTHOR or not history.get("top_author_email"):
        return False
    if actors.by_email(conn, history["top_author_email"]) == actor.id:
        return False
    hinted = conn.execute(
        "SELECT 1 FROM usage WHERE action = 'ask_author_hint' AND json_extract(detail, '$.session') = ? AND json_extract(detail, '$.path') = ?",
        (session_id, path),
    ).fetchone()
    return hinted is None


# --- end-of-turn check ------------------------------------------------------------


def turn_start(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE session SET turn_started_at = ?, stop_nudged = 0 WHERE id = ?", (db.now(), session_id))


def decision_shaped(files: list[str]) -> list[str]:
    return [f for f in files if any(fnmatchcase(f, pattern) or fnmatchcase(f.rsplit("/", 1)[-1], pattern) for pattern in DECISION_SHAPED)]


def turn_stop(conn: sqlite3.Connection, session_id: str, changed: list[str]) -> str:
    """A reason to keep the agent going for one more step, or '' to let it stop.

    Nudges at most once per turn, and only when decision-shaped files changed
    and nothing was drafted on the branch during the turn.
    """
    session = get(conn, session_id)
    if session.stop_nudged or not session.turn_started_at or session.branch_id is None:
        return ""
    shaped = decision_shaped(changed)
    if not shaped:
        return ""
    drafted = conn.execute(
        "SELECT COUNT(*) FROM draft WHERE branch_id = ? AND engineer = ? AND (created_at >= ? OR updated_at >= ?)",
        (session.branch_id, session.engineer, session.turn_started_at, session.turn_started_at),
    ).fetchone()[0]
    if drafted:
        return ""
    conn.execute("UPDATE session SET stop_nudged = 1 WHERE id = ?", (session_id,))
    return (
        f"Hippomem: this turn changed {', '.join(shaped[:5])}, which usually means a decision was made "
        "(a dependency, schema, infrastructure or wiring choice), but nothing was drafted. If you chose or dropped "
        "something, record it now with hippomem_draft and the reason. If not, just finish."
    )


# --- questions for authors ---------------------------------------------------------


def known_history(conn: sqlite3.Connection, repo: str, path: str) -> dict:
    """The git history the before-edit lookup saw for this file, if any."""
    row = conn.execute(
        """SELECT json_extract(detail, '$.history') FROM usage
           WHERE action = 'ask_author_hint' AND repo = ? AND json_extract(detail, '$.path') = ?
           ORDER BY id DESC LIMIT 1""",
        (repo, path),
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def ask_author(conn: sqlite3.Connection, actor: Actor, repo: str, path: str, question: str, history: dict) -> str:
    if not question.strip():
        raise ValueError("ask a question")
    history = history or known_history(conn, repo, path)
    email = str(history.get("top_author_email", ""))
    to = actors.by_email(conn, email) if email else None
    name = str(history.get("top_author_name", ""))
    to = to or (f"{name} <{email}>" if name and name != email else email or "unknown")
    with db.transaction(conn):
        qid = db.next_id(conn, "Q")
        conn.execute(
            """INSERT INTO question (id, asked_by, asked_via, to_engineer, repo, path, context, text, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (qid, actor.id, actor.agent, to, repo, path, json.dumps(history), question, db.now()),
        )
    return f"Asked {to} ({qid}). Their answer will come back as a proposal anchored to {repo}:{path}."


def _open_questions(conn: sqlite3.Connection, engineer: str) -> list[str]:
    rows = conn.execute("SELECT id, asked_by, repo, path, text FROM question WHERE to_engineer = ? AND status = 'open' ORDER BY id", (engineer,))
    return [
        f"- Question {r['id']} from {r['asked_by']} about {r['repo']}:{r['path']}: {r['text']}\n"
        f"  (hippomem_answer question_id={r['id']!r} with a title and the why, after they answer)"
        for r in rows
    ]


def answer(
    conn: sqlite3.Connection, policy: Policy, actor: Actor, question_id: str, title: str, why: str, alternatives: list[dict] | None = None
) -> proposals.Proposal:
    row = conn.execute("SELECT repo, path, status FROM question WHERE id = ?", (question_id,)).fetchone()
    if row is None:
        raise KeyError(question_id)
    if row["status"] != "open":
        raise ValueError(f"{question_id} is already {row['status']}")
    payload = {
        "title": title,
        "statement": title,
        "why": why,
        "scope": "code",
        "systems": systems.systems_for(conn, row["repo"], row["path"]),
        "anchors": [{"repo": row["repo"], "path_glob": row["path"]}],
        "alternatives": alternatives or [],
        "implemented": True,
        "evidence": [{"kind": "engineer_answer", "ref": question_id, "note": f"answered by {actor.id}"}],
    }
    p = proposals.create(conn, policy, kind="new", payload=payload, source="author_answer", proposed_by=actor.id, proposed_via=actor.agent)
    conn.execute(
        "UPDATE question SET status = 'answered', answer = ?, proposal_id = ?, answered_at = ? WHERE id = ?",
        (why, p.id, db.now(), question_id),
    )
    return p
