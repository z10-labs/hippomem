"""Run Meridian's 12 weeks: build the team, play every step through real
agents, and review proposals the way the team would (E1, E2, E6).

Review happens at the end of each simulated week. The reviewer agent goes
first and decides what it's allowed to (standard weight). Then each
remaining proposal is graded against the answer key, and the simulated
engineers accept it if it says what the team decided for the right reason,
or reject it. Every grade is kept: that is the "before review" measure of
the capture pipeline. Questions to authors are answered from the answer key's
legacy reasons.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from hippomem import db, sessions
from hippomem.policy import Actor, Policy
from research import grader as grading
from research.meridian import world
from research.team import Engineer, Sandbox

COMMIT_LINE = "\n\nWhen you're done, commit your work on the current branch with a clear message."
REVIEWER = "watchman"
REVIEWER_PROMPT = """You are Watchman, the team's reviewer agent for Hippomem, the team's engineering memory.

Review the open proposals (hippomem_review). For each one you are allowed to decide (standard weight, not scope-wide, not your own):
- accept it if it states one clear decision, gives a reason, and doesn't conflict with or duplicate an accepted decision (check with hippomem_query and hippomem_get);
- reject it, with a short note saying why, if it is vague, isn't really a decision, duplicates an accepted one, or its reason isn't supported.
Leave heavy and scope-wide proposals for the engineers. Finish with a one-line summary of what you decided."""


def at(start: date, week: int, day: int, hour: int, minute: int = 0) -> str:
    moment = datetime(start.year, start.month, start.day, hour, minute, tzinfo=timezone.utc) + timedelta(weeks=week - 1, days=day - 1)
    return moment.strftime(db.TIME_FORMAT)


def build(box: Sandbox, timeline: dict) -> None:
    """Create the team, the repos with their pre-memory history, and the memory's git hooks."""
    engineers = [Engineer(e["id"], e["name"], e["email"], e["kind"]) for e in timeline["engineers"]]
    engineers.sort(key=lambda e: e.kind != "admin")  # the admin creates the memory
    box.now = "2025-05-01T09:00:00Z"  # create() writes the clock once the sandbox exists
    box.create(world.TEAM_CONFIG, engineers, reviewer=REVIEWER)
    for name, (author, files) in world.REPOS.items():
        box.add_repo(name, files, author=author)
    for repo, author, when, message, files in world.LEGACY_COMMITS:
        box.set_time(when)
        box.commit(repo, author, message, files)
    for name in world.REPOS:
        box.install_hooks(name)
    box.set_time(at(date.fromisoformat(timeline["world"]["start"]), 1, 1, 8))
    box.save()


# --- steps ---------------------------------------------------------------------------------


def run_step(box: Sandbox, step: dict, start: date, log: Path) -> None:
    engineer, repo, branch = step["engineer"], step["repo"], step.get("branch", "")
    box.set_time(at(start, step["week"], step["day"], 9))
    if branch:
        box.checkout(repo, branch, engineer)
    else:
        box.git(repo, "checkout", "-q", "main", as_=engineer)
    record: dict = {"step": step["id"], "runs": [], "anomalies": []}
    for i, prompt in enumerate(step["sessions"]):
        box.set_time(at(start, step["week"], step["day"], 9 + 2 * i))
        text = prompt + (COMMIT_LINE if step["end"] != "none" else "")
        run = box.run_agent(engineer, box.repo(repo), text, label=f"{step['id']}-s{i + 1}")
        record["runs"].append({"run_id": run.run_id, "cost": run.cost_usd, "turns": run.turns, "error": run.error})
        if branch and box.git(repo, "rev-parse", "--abbrev-ref", "HEAD") != branch:
            record["anomalies"].append(f"session {i + 1} left the repo on another branch")
            box.git(repo, "checkout", "-q", branch, as_=engineer)
        if box.has_changes(repo) and step["end"] != "none":
            record["anomalies"].append(f"session {i + 1} left uncommitted changes; the harness committed them")
            box.git(repo, "add", "-A", as_=engineer)
            box.git(repo, "commit", "-q", "-m", "wip: changes the agent left uncommitted", as_=engineer)
    box.set_time(at(start, step["week"], step["day"], 16))
    if step["end"] == "merge":
        box.merge(repo, branch, engineer)
    elif step["end"] == "close":
        box.git(repo, "checkout", "-q", "main", as_=engineer)
        box.hm(engineer, "branch", "close", branch, "--repo", repo)
    with log.open("a") as f:
        f.write(json.dumps(record) + "\n")


# --- review --------------------------------------------------------------------------------


def open_proposals(box: Sandbox) -> list[sqlite3.Row]:
    return box.rows("SELECT * FROM proposal WHERE status = 'open' ORDER BY id")


def proposal_text(p: sqlite3.Row) -> str:
    payload = json.loads(p["payload"])
    lines = [f"Kind: {p['kind']}" + (f" (target {p['target']})" if p["target"] else ""), f"Title: {payload.get('title', '')}"]
    for key in ("statement", "why", "trade_off"):
        if payload.get(key):
            lines.append(f"{key.replace('_', '-').capitalize()}: {payload[key]}")
    for alt in payload.get("alternatives", ()):
        lines.append(f"Rejected: {alt['option']}: {alt['reason']}")
    for con in payload.get("constraints", ()):
        lines.append(f"Constraint: {con.get('text', '')} (revisit if {con.get('revisit_if', '')})")
    if payload.get("inferred"):
        lines.append("(inferred from branch history; nobody drafted it)")
    return "\n".join(lines)


def known_records(timeline: dict, week: int) -> list[dict]:
    return [r for r in timeline["decisions"] + timeline["explored"] if r["week"] <= week]


def deciders(timeline: dict, proposed_by: str, systems: list[str]) -> list[str]:
    """Who to ask, in order: owners of the proposal's systems, the admin, everyone else; never the proposer."""
    owners = [e["id"] for e in timeline["engineers"] if set(e["owns"]) & set(systems)]
    admins = [e["id"] for e in timeline["engineers"] if e["kind"] == "admin"]
    rest = [e["id"] for e in timeline["engineers"]]
    order = list(dict.fromkeys(owners + admins + rest))
    return [a for a in order if a != proposed_by]


def decide(box: Sandbox, actors: list[str], proposal_id: str, accept: bool, note: str) -> tuple[str, str]:
    """Try each actor until one is allowed; return (actor, outcome)."""
    outcome = ""
    for actor in actors:
        outcome = box.hm(actor, "accept" if accept else "reject", proposal_id, "--note", note)
        if not outcome.startswith("Not allowed"):
            return actor, outcome
    return "", outcome


def is_good(grade: dict) -> bool:
    return bool(grade.get("match")) and bool(grade.get("statement_correct")) and grade.get("why") in ("correct", "partial")


def review_week(box: Sandbox, timeline: dict, week: int, grader: grading.Grader, matched: dict[str, str], log: Path) -> None:
    """Grade every open proposal (the before-review measure), let the reviewer
    agent decide what it may, then the simulated engineers decide the rest."""
    start = date.fromisoformat(timeline["world"]["start"])
    box.set_time(at(start, week, 5, 17))
    candidates = known_records(timeline, week)
    grades: dict[str, dict] = {}
    for p in open_proposals(box):
        if p["kind"] == "mapping":
            continue
        try:
            grades[p["id"]] = grading.match_proposal(grader, proposal_text(p), candidates, label=f"match-{p['id']}")
        except grading.GraderError as err:
            _append(log, {"week": week, "proposal": p["id"], "error": str(err)})
    if open_proposals(box):
        workspace = box.root / "workspace-reviewer"
        workspace.mkdir(exist_ok=True)
        box.run_agent(REVIEWER, workspace, REVIEWER_PROMPT, with_code=False, label=f"w{week}-review")
    for row in box.rows(f"SELECT * FROM proposal WHERE id IN ({','.join('?' * len(grades))})", *grades) if grades else []:
        if row["status"] != "open":  # the reviewer agent decided it
            grade = grades[row["id"]]
            if row["status"] == "accepted" and is_good(grade) and str(grade["match"]) not in matched:
                matched[str(grade["match"])] = row["id"]
            _append(log, _entry(week, row, grade, decision=row["status"], by=row["decided_by"], outcome="by reviewer agent"))
    box.set_time(at(start, week, 5, 18))
    for p in open_proposals(box):
        if p["kind"] == "mapping":
            actor, outcome = decide(box, deciders(timeline, p["proposed_by"], []), p["id"], True, "mapping looks right")
            _append(log, _entry(week, p, None, decision="accept", by=actor, outcome=outcome))
            continue
        if p["id"] not in grades:
            continue
        grade = grades[p["id"]]
        match = str(grade.get("match"))
        duplicate = is_good(grade) and match in matched
        systems = json.loads(p["payload"]).get("systems", [])
        if is_good(grade) and not duplicate:
            actor, outcome = decide(box, deciders(timeline, p["proposed_by"], systems), p["id"], True, "matches what we decided")
            if outcome.startswith("Accepted"):
                matched[match] = p["id"]
            _append(log, _entry(week, p, grade, decision="accept", by=actor, outcome=outcome))
        else:
            note = f"already recorded ({matched[match]})" if duplicate else "doesn't say what we decided, or why"
            actor, outcome = decide(box, deciders(timeline, p["proposed_by"], systems), p["id"], False, note)
            _append(log, _entry(week, p, grade, decision="reject", by=actor, outcome=outcome))
    answer_questions(box, timeline, log)


def _entry(week: int, p: sqlite3.Row, grade: dict | None, **decision: str) -> dict:
    payload = json.loads(p["payload"])
    return {
        "week": week,
        "proposal": p["id"],
        "kind": p["kind"],
        "weight": p["weight"],
        "source": p["source"],
        "inferred": bool(payload.get("inferred")),
        "proposed_by": p["proposed_by"],
        "proposed_via": p["proposed_via"],
        "grade": grade,
        **decision,
    }


def answer_questions(box: Sandbox, timeline: dict, log: Path) -> None:
    legacy = {(item["repo"], item["path"]): item for item in timeline["legacy"]}
    for q in box.rows("SELECT id, to_engineer, repo, path FROM question WHERE status = 'open'"):
        item = legacy.get((q["repo"], q["path"]))
        conn = db.connect(box.db_path)
        try:
            if item and item["author"] == q["to_engineer"]:
                actor = Actor(q["to_engineer"], "engineer")
                p = sessions.answer(conn, Policy(), actor, q["id"], title=f"Why {q['path']} is built this way", why=item["reason"])
                _append(log, {"question": q["id"], "answered_by": q["to_engineer"], "proposal": p.id, "legacy": item["id"]})
            else:
                with db.transaction(conn):
                    conn.execute("UPDATE question SET status = 'dismissed', answered_at = ? WHERE id = ?", (db.now(), q["id"]))
                _append(log, {"question": q["id"], "dismissed": True, "reason": "no known reason"})
        finally:
            conn.close()


def _append(log: Path, entry: dict) -> None:
    with log.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# --- the whole run -------------------------------------------------------------------------


def run(box: Sandbox, timeline: dict, grader: grading.Grader, only_weeks: int = 0) -> None:
    """Play the timeline (or its first `only_weeks` weeks) into an already built sandbox."""
    start = date.fromisoformat(timeline["world"]["start"])
    out = box.root / "e1"
    out.mkdir(exist_ok=True)
    matched_file = out / "matched.json"
    matched: dict[str, str] = json.loads(matched_file.read_text()) if matched_file.exists() else {}
    last_week = only_weeks or timeline["world"]["weeks"]
    box.start_server()
    try:
        for week in range(1, last_week + 1):
            for step in (s for s in timeline["steps"] if s["week"] == week):
                run_step(box, step, start, out / "steps.jsonl")
            review_week(box, timeline, week, grader, matched, out / "review.jsonl")
            matched_file.write_text(json.dumps(matched, indent=2))
            box.save()
    finally:
        box.stop_server()
    if last_week == timeline["world"]["weeks"]:
        grade_explored(box, timeline, grader, out / "explored.jsonl")


def grade_explored(box: Sandbox, timeline: dict, grader: grading.Grader, log: Path) -> None:
    """Explored records skip review, so grade each one once, at the end."""
    candidates = timeline["explored"] + timeline["decisions"]
    for x in box.rows("SELECT * FROM explored ORDER BY id"):
        text = f"Explored record: {x['title']}\nTried: {x['tried']}\nOutcome: {x['outcome']}\nWhy: {x['why']}\nRevisit if: {x['revisit_if']}"
        try:
            grade = grading.match_proposal(grader, text, candidates, label=f"match-{x['id']}")
        except grading.GraderError as err:
            grade = {"error": str(err)}
        _append(log, {"explored": x["id"], "grade": grade})
