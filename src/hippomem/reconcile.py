"""Turning a branch's draft trail into proposals, rejected alternatives and
explored records.

At merge, the diff shows which drafts were actually built. Built drafts
become proposals, with the commits as evidence. Drafts dropped in favour of a
built one become its rejected alternatives. Lasting dead ends become explored
records. The rest is discarded. When a branch closes without merging there is
no diff: the trail is only sorted into explored records and noise.

The judgement calls go to `judge.run`. If the judge fails, merge falls back to
the trail's own structure (backed drafts are built, replaced drafts are their
alternatives) and records the failure as an event, so nothing is lost
silently. A failed close leaves the trail untouched to be sorted again.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from hippomem import db, drafts, explored, judge, proposals, systems
from hippomem.policy import Policy

MAX_DIFF_CHARS = 60_000
MAX_HISTORY_CHARS = 30_000


@dataclass(frozen=True)
class Outcome:
    proposals: tuple[str, ...] = ()
    alternatives: tuple[int, ...] = ()
    explored: tuple[str, ...] = ()
    discarded: tuple[int, ...] = ()
    judged: bool = True
    error: str = ""
    notes: tuple[str, ...] = field(default=())


def log_event(conn: sqlite3.Connection, kind: str, repo: str, branch: str, **payload: object) -> None:
    conn.execute(
        "INSERT INTO event (kind, repo, branch, payload, at) VALUES (?, ?, ?, ?, ?)",
        (kind, repo, branch, json.dumps(payload, default=str), db.now()),
    )


def _draft_json(d: drafts.Draft) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "status": d.status,
        "title": d.title,
        "statement": d.statement,
        "why": d.why,
        "dropped_reason": d.dropped_reason,
        "replaced_by": d.replaced_by,
        "paths": list(d.paths),
        "backed_by_commit": d.backed_by,
    }


MERGE_PROMPT = """You are reconciling an engineering team's draft decisions with what a merged branch actually built.
Drafts were written while agents and engineers worked; some were reversed along the way.

Repo: {repo}
Branch: {branch}

Drafts:
{drafts}

Commit history of the branch, oldest first (may be truncated). Use it to spot options that were built and then
reversed even if nobody drafted them:
{history}

Diff of the merge (may be truncated):
{diff}

Give exactly one verdict per draft id:
- "built": the diff implements this decision. Include "title" (short), "statement" (one sentence, what is now true),
  "why" (the reason, from the draft), "weight" ("heavy" for architecture, data model, security or cross-service
  choices, else "standard"), "files" (repo paths or globs the decision governs), and "alternatives": options the
  commit history shows were tried and reversed for this decision, as [{{"option": ..., "reason": ...}}] (empty if none).
- "alternative": a dropped option that lost to a built draft. Include "of" (the built draft id) and "reason" (why it lost).
- "explored": a dropped dead end with a lasting lesson that is not just an alternative of a built draft. Include "tried",
  "outcome", "why", and "revisit_if" (what would make it worth trying again).
- "discard": noise, renaming, trivia, or not built.

Then list "undrafted": real engineering decisions the history and diff show were made but no draft covers (an approach
chosen, often after reversing another). Leave out trivia. Each: "title", "statement", "why" (from commit messages and
code; say "not recorded" rather than invent), "weight", "files", "alternatives" as above.

Reply with only JSON: {{"drafts": [{{"id": 1, "verdict": "built", ...}}, ...], "undrafted": [...]}}
"""

CLOSE_PROMPT = """A branch was closed without merging. Sort its draft trail: keep only dead ends that taught something lasting.

Repo: {repo}
Branch: {branch}

Drafts:
{drafts}

Give exactly one verdict per draft id:
- "explored": a lasting lesson. Include "title", "tried", "outcome", "why", and "revisit_if" (what would make it worth trying again).
- "discard": noise, or nothing anyone would need before trying the same thing.

Reply with only JSON: {{"drafts": [{{"id": 1, "verdict": "explored", ...}}, ...]}}
"""


def merge(
    conn: sqlite3.Connection,
    policy: Policy,
    branch_id: int,
    diff: str,
    commits: list[str],
    keep_branch_open: bool = False,
    history: str = "",
    merged_by: str = "",
) -> Outcome:
    """Reconcile a branch's trail with the merged diff and commit history.

    With `keep_branch_open` (commits straight onto a default branch) only
    backed drafts are judged and the branch stays active. Decisions the
    history shows but nobody drafted become proposals from `merged_by`.
    """
    branch = drafts.get_branch(conn, branch_id)
    trail = drafts.trail(conn, branch_id)
    if keep_branch_open:
        backed = {d.id for d in trail if d.backed_by}
        trail = [d for d in trail if d.id in backed or (d.status == "dropped" and d.replaced_by in backed)]

    scope_drafts = [d for d in trail if d.scope != "code" and d.status == "open"]
    code_trail = [d for d in trail if d not in scope_drafts]
    notes: list[str] = []
    created: list[str] = [_scope_proposal(conn, policy, branch, d) for d in scope_drafts]
    if scope_drafts:
        notes.append(f"{len(scope_drafts)} module/service/team-wide drafts sent for an owner to confirm")

    verdicts: dict[int, dict] = {}
    undrafted: list[dict] = []
    error = ""
    if code_trail or (history and not keep_branch_open):
        try:
            reply = judge.run(
                MERGE_PROMPT.format(
                    repo=branch.repo,
                    branch=branch.name,
                    drafts=json.dumps([_draft_json(d) for d in code_trail], indent=1) if code_trail else "(none)",
                    history=history[:MAX_HISTORY_CHARS] or "(not available)",
                    diff=diff[:MAX_DIFF_CHARS],
                )
            )
            verdicts = {int(v["id"]): v for v in reply.get("drafts", []) if "id" in v}
            undrafted = [u for u in reply.get("undrafted", []) if isinstance(u, dict) and str(u.get("title", "")).strip()]
        except (judge.JudgeError, KeyError, ValueError, TypeError) as err:
            error = str(err)
            log_event(conn, "judge_failed", branch.repo, branch.name, stage="merge", error=error)

    outcome = _apply_merge(conn, policy, branch, code_trail, verdicts, commits)
    found = [_undrafted_proposal(conn, policy, branch, u, commits, merged_by) for u in undrafted]
    if found:
        notes.append(f"{len(found)} decisions found in the history that nobody drafted")
        outcome = Outcome(
            proposals=outcome.proposals + tuple(found), alternatives=outcome.alternatives, explored=outcome.explored, discarded=outcome.discarded
        )
    if not keep_branch_open:
        drafts.end_branch(conn, branch_id, "merged")
    log_event(conn, "merged", branch.repo, branch.name, proposals=created + list(outcome.proposals), commits=commits, judged=not error)
    return Outcome(
        proposals=tuple(created) + outcome.proposals,
        alternatives=outcome.alternatives,
        explored=outcome.explored,
        discarded=outcome.discarded,
        judged=not error,
        error=error,
        notes=tuple(notes),
    )


def _fallback_verdict(d: drafts.Draft, trail_by_id: dict[int, drafts.Draft]) -> dict:
    """Without a judge, trust the trail's own structure."""
    if d.status == "open" and d.backed_by:
        return {"verdict": "built", "files": list(d.paths)}
    if d.status == "dropped" and d.replaced_by in trail_by_id and trail_by_id[d.replaced_by].backed_by:
        return {"verdict": "alternative", "of": d.replaced_by, "reason": d.dropped_reason}
    return {"verdict": "discard"}


def _apply_merge(
    conn: sqlite3.Connection, policy: Policy, branch: drafts.Branch, trail: list[drafts.Draft], verdicts: dict[int, dict], commits: list[str]
) -> Outcome:
    by_id = {d.id: d for d in trail}
    resolved = {d.id: verdicts.get(d.id) or _fallback_verdict(d, by_id) for d in trail}

    alternatives_of: dict[int, list[dict]] = {}
    for d in trail:
        v = resolved[d.id]
        if v.get("verdict") == "alternative":
            winner = int(v.get("of") or d.replaced_by or 0)
            if winner in by_id and resolved[winner].get("verdict") == "built":
                alternatives_of.setdefault(winner, []).append(
                    {"option": d.title, "reason": v.get("reason") or d.dropped_reason or "dropped during work"}
                )
            else:
                resolved[d.id] = {"verdict": "discard"}
    for d in trail:
        if d.status == "dropped" and d.replaced_by in by_id and resolved[d.id].get("verdict") == "discard":
            if resolved[d.replaced_by].get("verdict") == "built":
                resolved[d.id] = {"verdict": "alternative", "of": d.replaced_by, "reason": d.dropped_reason}
                alternatives_of.setdefault(d.replaced_by, []).append({"option": d.title, "reason": d.dropped_reason})

    made_proposals: list[str] = []
    made_explored: list[str] = []
    alternatives: list[int] = []
    discarded: list[int] = []
    proposal_of: dict[int, str] = {}
    for d in trail:
        v = resolved[d.id]
        if v.get("verdict") == "built":
            pid = _built_proposal(conn, policy, branch, d, v, _merge_alternatives(alternatives_of.get(d.id, []), v.get("alternatives")), commits)
            proposal_of[d.id] = pid
            made_proposals.append(pid)
            drafts.set_outcome(conn, d.id, "proposed", pid)
    for d in trail:
        v = resolved[d.id]
        verdict = v.get("verdict")
        if verdict == "built":
            continue
        if verdict == "alternative":
            drafts.set_outcome(conn, d.id, "alternative", proposal_of.get(int(v.get("of") or d.replaced_by or 0)))
            alternatives.append(d.id)
        elif verdict == "explored" and str(v.get("revisit_if", "")).strip():
            xid = _explored(conn, branch, d, v)
            drafts.set_outcome(conn, d.id, "explored", xid)
            made_explored.append(xid)
        else:
            drafts.set_outcome(conn, d.id, "discarded")
            discarded.append(d.id)
    return Outcome(proposals=tuple(made_proposals), alternatives=tuple(alternatives), explored=tuple(made_explored), discarded=tuple(discarded))


def _merge_alternatives(from_trail: list[dict], from_judge: object) -> list[dict]:
    """Alternatives from reversed drafts, plus any the judge found in the history, without duplicates."""
    merged = list(from_trail)
    seen = {a["option"].strip().lower() for a in merged}
    for alt in from_judge if isinstance(from_judge, list) else []:
        if isinstance(alt, dict) and str(alt.get("option", "")).strip() and alt["option"].strip().lower() not in seen:
            merged.append({"option": str(alt["option"]), "reason": str(alt.get("reason") or "reversed during work")})
            seen.add(alt["option"].strip().lower())
    return merged


def _undrafted_proposal(conn: sqlite3.Connection, policy: Policy, branch: drafts.Branch, u: dict, commits: list[str], merged_by: str) -> str:
    """A decision the history shows but nobody drafted. Proposed by whoever
    merged, marked so a reviewer knows it was inferred, not stated."""
    files = [str(f) for f in u.get("files", []) if str(f).strip()]
    payload = {
        "title": str(u["title"]),
        "statement": str(u.get("statement") or u["title"]),
        "why": str(u.get("why") or "not recorded"),
        "scope": "code",
        "systems": systems.systems_for(conn, branch.repo, files[0] if files else None),
        "anchors": [{"repo": branch.repo, "path_glob": f} for f in files],
        "alternatives": _merge_alternatives([], u.get("alternatives")),
        "implemented": True,
        "inferred": True,
        "evidence": [{"kind": "commit", "ref": c} for c in commits]
        + [{"kind": "branch", "ref": f"{branch.repo}:{branch.name}", "note": "inferred from history, not drafted"}],
    }
    weight = str(u["weight"]) if u.get("weight") in ("standard", "heavy") else "standard"
    p = proposals.create(
        conn,
        policy,
        kind="new",
        payload=payload,
        source="reconciliation",
        proposed_by=merged_by or "unknown",
        proposed_via="git",
        weight=weight,
        branch_id=branch.id,
    )
    return p.id


def _systems_for_draft(conn: sqlite3.Connection, branch: drafts.Branch, d: drafts.Draft, files: list[str]) -> list[str]:
    found: list[str] = [d.system_id] if d.system_id else []
    for path in files or list(d.paths) or [None]:  # type: ignore[list-item]
        for s in systems.systems_for(conn, branch.repo, path):
            if s not in found:
                found.append(s)
    return found


def _built_proposal(
    conn: sqlite3.Connection, policy: Policy, branch: drafts.Branch, d: drafts.Draft, v: dict, alternatives: list[dict], commits: list[str]
) -> str:
    files = [str(f) for f in v.get("files", []) if str(f).strip()] or list(d.paths)
    weight = str(v["weight"]) if v.get("weight") in ("standard", "heavy") else "standard"
    payload = {
        "title": v.get("title") or d.title,
        "statement": v.get("statement") or d.statement or d.title,
        "why": v.get("why") or d.why or "(no reason recorded in the draft)",
        "scope": "code",
        "systems": _systems_for_draft(conn, branch, d, files),
        "anchors": [{"repo": branch.repo, "path_glob": f} for f in files],
        "alternatives": alternatives,
        "implemented": True,
        "evidence": [{"kind": "commit", "ref": c} for c in commits]
        + [{"kind": "branch", "ref": f"{branch.repo}:{branch.name}", "note": f"draft #{d.id}"}],
        "drafts": [d.id],
    }
    p = proposals.create(
        conn,
        policy,
        kind="new",
        payload=payload,
        source="reconciliation",
        proposed_by=d.engineer,
        proposed_via=d.agent,
        weight=weight,
        session_id=d.session_id,
        branch_id=branch.id,
    )
    return p.id


def _scope_proposal(conn: sqlite3.Connection, policy: Policy, branch: drafts.Branch, d: drafts.Draft) -> str:
    payload = {
        "title": d.title,
        "statement": d.statement or d.title,
        "why": d.why or "(no reason recorded in the draft)",
        "scope": d.scope,
        "systems": _systems_for_draft(conn, branch, d, []) if d.scope == "system" else [],
        "evidence": [{"kind": "branch", "ref": f"{branch.repo}:{branch.name}", "note": f"draft #{d.id}"}],
        "drafts": [d.id],
    }
    p = proposals.create(
        conn,
        policy,
        kind="new",
        payload=payload,
        source="scope",
        proposed_by=d.engineer,
        proposed_via=d.agent,
        session_id=d.session_id,
        branch_id=branch.id,
    )
    drafts.set_outcome(conn, d.id, "proposed", p.id)
    return p.id


def _explored(conn: sqlite3.Connection, branch: drafts.Branch, d: drafts.Draft, v: dict) -> str:
    repo_systems = systems.systems_for(conn, branch.repo)
    return explored.create(
        conn,
        title=v.get("title") or d.title,
        tried=v.get("tried") or d.title,
        outcome=v.get("outcome") or "dropped",
        why=v.get("why") or d.dropped_reason or d.why,
        revisit_if=str(v["revisit_if"]),
        system_id=d.system_id or (repo_systems[0] if repo_systems else None),
        repo=branch.repo,
        branch_id=branch.id,
    )


def close(conn: sqlite3.Connection, branch_id: int) -> Outcome:
    """Sort the trail of a branch that ended without merging."""
    branch = drafts.get_branch(conn, branch_id)
    if branch.state == "active":
        drafts.end_branch(conn, branch_id, "closed")
    trail = drafts.trail(conn, branch_id)
    if not trail:
        log_event(conn, "closed", branch.repo, branch.name, explored=[])
        return Outcome()
    try:
        reply = judge.run(CLOSE_PROMPT.format(repo=branch.repo, branch=branch.name, drafts=json.dumps([_draft_json(d) for d in trail], indent=1)))
        verdicts = {int(v["id"]): v for v in reply.get("drafts", []) if "id" in v}
    except (judge.JudgeError, KeyError, ValueError, TypeError) as err:
        log_event(conn, "judge_failed", branch.repo, branch.name, stage="close", error=str(err))
        return Outcome(judged=False, error=str(err), notes=("trail left as is; sort it again with `hippomem branch sort`",))

    made: list[str] = []
    discarded: list[int] = []
    for d in trail:
        v = verdicts.get(d.id, {"verdict": "discard"})
        if v.get("verdict") == "explored" and str(v.get("revisit_if", "")).strip():
            xid = _explored(conn, branch, d, v)
            drafts.set_outcome(conn, d.id, "explored", xid)
            made.append(xid)
        else:
            drafts.set_outcome(conn, d.id, "discarded")
            discarded.append(d.id)
    log_event(conn, "closed", branch.repo, branch.name, explored=made)
    return Outcome(explored=tuple(made), discarded=tuple(discarded))
