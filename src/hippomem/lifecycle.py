"""Git events: what moves drafts along.

Sessions come and go; the branch and its git events last. A checkout makes
the branch known, a commit marks which drafts are now backed by code, a merge
reconciles the trail, and a closed or stale branch has its trail sorted.
Commits straight onto a default branch are reconciled commit by commit.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from hippomem import db, drafts, reconcile
from hippomem.policy import Policy

DEFAULT_BRANCHES = ("main", "master", "trunk", "develop")


def is_default(branch: str, default_branch: str = "") -> bool:
    return branch == default_branch if default_branch else branch in DEFAULT_BRANCHES


def on_checkout(conn: sqlite3.Connection, repo: str, branch: str) -> dict:
    if not branch or branch == "HEAD":
        return {"ignored": "detached HEAD"}
    return {"branch_id": drafts.ensure_branch(conn, repo, branch)}


def on_commit(
    conn: sqlite3.Connection, policy: Policy, repo: str, branch: str, commit: str, files: list[str], diff: str = "", default_branch: str = ""
) -> dict:
    if not branch or branch == "HEAD":
        return {"ignored": "detached HEAD"}
    branch_id = drafts.ensure_branch(conn, repo, branch)
    conn.execute("UPDATE branch SET head_commit = ? WHERE id = ?", (commit, branch_id))
    backed = drafts.mark_backed(conn, branch_id, commit, files)
    reconcile.log_event(conn, "commit", repo, branch, commit=commit, files=files, backed=backed)
    result: dict = {"branch_id": branch_id, "backed": backed}
    if is_default(branch, default_branch) and backed:
        outcome = reconcile.merge(conn, policy, branch_id, diff, [commit], keep_branch_open=True)
        result["proposals"] = list(outcome.proposals)
    return result


def on_merge(
    conn: sqlite3.Connection,
    policy: Policy,
    repo: str,
    merged_branch: str,
    into: str,
    diff: str,
    commits: list[str],
    history: str = "",
    merged_by: str = "",
) -> dict:
    branch = drafts.find_active(conn, repo, merged_branch)
    if branch is None:
        reconcile.log_event(conn, "merge_unknown_branch", repo, merged_branch, into=into)
        return {"ignored": f"no active branch {merged_branch!r} in {repo}"}
    outcome = reconcile.merge(conn, policy, branch.id, diff, commits, history=history, merged_by=merged_by)
    return {
        "proposals": list(outcome.proposals),
        "alternatives": list(outcome.alternatives),
        "explored": list(outcome.explored),
        "discarded": list(outcome.discarded),
        "judged": outcome.judged,
        "error": outcome.error,
        "notes": list(outcome.notes),
    }


def on_close(conn: sqlite3.Connection, repo: str, branch: str) -> dict:
    found = drafts.find_active(conn, repo, branch)
    if found is None:
        return {"ignored": f"no active branch {branch!r} in {repo}"}
    outcome = reconcile.close(conn, found.id)
    return {"explored": list(outcome.explored), "discarded": list(outcome.discarded), "judged": outcome.judged, "error": outcome.error}


def sort_again(conn: sqlite3.Connection, branch_id: int) -> dict:
    """Retry sorting a closed branch whose judge call failed."""
    outcome = reconcile.close(conn, branch_id)
    return {"explored": list(outcome.explored), "judged": outcome.judged, "error": outcome.error}


def close_stale(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> list[str]:
    """Close active, non-default branches with no activity for `days` days."""
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT id, repo, name FROM branch WHERE state = 'active' AND last_activity_at < ?", (cutoff,)).fetchall()
    closed = []
    for branch_id, repo, name in rows:
        if is_default(name):
            continue
        reconcile.close(conn, branch_id)
        closed.append(f"{repo}:{name}")
    if closed:
        reconcile.log_event(conn, "stale_closed", "", "", branches=closed, at=db.now())
    return closed
