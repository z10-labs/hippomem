"""Scenarios about the decision lifecycle: drafts across sessions, merge
reconciliation, closed branches, other branches, nudges, authors, review."""

from __future__ import annotations

import json

from scenarios.harness import Team, sh
from scenarios.scenarios import Outcome, branch, grep, tool, used_memory


def _branch_id(team: Team, repo: str, name: str) -> int:
    rows = team.rows("SELECT id FROM branch WHERE repo = ? AND name = ? ORDER BY id DESC LIMIT 1", repo, name)
    return int(rows[0]["id"]) if rows else 0


# --- 4. a decision reversed across sessions, then merged -------------------------------------


def s4_reversal_and_merge(team: Team) -> Outcome:
    out = Outcome("S4", "A decision reversed in a later session, continued in a third, then merged and reconciled")
    repo = team.repo("ledger-api")
    branch(team, repo, "s4/refund-window")
    team.run_agent(
        "S4",
        "session 1: store the deadline",
        repo,
        "Add a refund window: a refund can only be requested within 14 days of delivery. Store a refund_deadline on the Order "
        "when it's marked delivered (src/ledger_api/orders.py), and check it in request_refund. Commit when done.",
    )
    team.run_agent(
        "S4",
        "session 2: reverse it",
        repo,
        "Change of plan on the refund window: a stored refund_deadline goes stale when a delivery date gets corrected. "
        "Compute the deadline from delivered_at instead, and remove the stored field. Commit when done.",
    )
    since = team.last_usage_id()
    third = team.run_agent(
        "S4",
        "session 3: continue",
        repo,
        "Continue the refund window work: add refund_deadline_for(order) to src/ledger_api/refunds.py so the API can show customers "
        "their deadline. Commit when done. In your final answer, say how the deadline is determined and why.",
    )
    b = _branch_id(team, "ledger-api", "s4/refund-window")
    trail = team.rows("SELECT id, title, status, dropped_reason, replaced_by FROM draft WHERE branch_id = ? ORDER BY id", b)
    out.notes.append("trail: " + "; ".join(f"#{r['id']} {r['status']}: {r['title']}" for r in trail))
    out.check("drafts were recorded on the branch", len(trail) >= 2, f"{len(trail)} drafts")
    out.check("the reversal was recorded with a reason", any(r["status"] == "dropped" and r["dropped_reason"] for r in trail), "")
    out.check("session 3 started with the branch digest", len(team.usage("session_start", since)) >= 1, "")
    stored_field = grep(repo, "refund_deadline:", "src/ledger_api/orders.py")
    out.check("session 3 did not bring the stored field back", not stored_field, f"{stored_field}")
    out.check("session 3 explains the computed deadline", "deliver" in third.result.lower(), third.result[-400:])

    sh(repo, "git", "checkout", "-q", "main")
    sh(repo, "git", "merge", "-q", "--no-ff", "-m", "Merge refund window", "s4/refund-window", env={"HIPPOMEM_GIT_SYNC": "1"})
    merged = team.rows("SELECT payload FROM event WHERE kind = 'merged' AND branch = 's4/refund-window'")
    out.check("the merge reached Hippomem", bool(merged), "")
    judged = bool(merged) and json.loads(merged[-1]["payload"]).get("judged")
    out.check("the judge reconciled the trail (no fallback)", bool(judged), merged[-1]["payload"] if merged else "")
    props = team.rows("SELECT id, payload FROM proposal WHERE source = 'reconciliation' AND branch_id = ?", b)
    out.check("reconciliation produced a proposal", bool(props), f"{len(props)} proposals")
    alternatives = [a for p in props for a in json.loads(p["payload"]).get("alternatives", [])]
    out.check(
        "the stored-deadline option became a rejected alternative", any("stor" in a["option"].lower() for a in alternatives), json.dumps(alternatives)
    )
    if props:
        outcome = tool(team, team.tokens["@sam"], "hippomem_decide", proposal_id=props[0]["id"], accept=True)
        out.check("another engineer accepted it", "now in force" in outcome, outcome)
        out.notes.append("proposal: " + json.dumps(json.loads(props[0]["payload"]), indent=None)[:600])
    return out


# --- 5. another branch moving the same thing the other way ---------------------------------------


def s5_other_branch(team: Team) -> Outcome:
    out = Outcome("S5", "Another engineer's branch is moving rate limiting into the gateway; this agent touches rate limiting")
    repo = team.repo("ledger-api")
    branch(team, repo, "s5/gateway-limits")
    tool(
        team,
        team.tokens["@sam"],
        "hippomem_draft",
        title="Move refund rate limiting into the API gateway",
        why="one place for every limit, and the gateway already knows the customer",
        repo="ledger-api",
        branch="s5/gateway-limits",
    )
    branch(team, repo, "s5/tune-limits")
    run = team.run_agent(
        "S5",
        "with Hippomem",
        repo,
        "We want refund rate limiting in request_refund to allow 10 requests per customer per hour. Add or adjust it in "
        "src/ledger_api/refunds.py and commit. In your final answer, mention anything the team should know before merging.",
    )
    out.check("the agent noticed the other branch's plan", "gateway" in run.result.lower(), run.result[-500:])
    sh(repo, "git", "checkout", "-q", "main")
    return out


# --- 6. an abandoned spike becomes an explored record ---------------------------------------------


def s6_closed_branch(team: Team) -> Outcome:
    out = Outcome("S6", "An abandoned spike branch is closed; its lesson becomes an explored record")
    before = len(team.rows("SELECT id FROM explored"))
    for title, why in (("Use a cron container for payout batches", "simplest way to run the batch"),):
        tool(team, team.tokens["@sam"], "hippomem_draft", title=title, why=why, repo="ledger-api", branch="s6/payout-cron")
    tool(
        team,
        team.tokens["@sam"],
        "hippomem_draft",
        title="Run payout batches from ledger-worker's job loop instead",
        why="no extra container",
        replaces=_latest_draft(team, "s6/payout-cron"),
        replaced_because="a separate cron container needs its own deploy and secrets, and missed runs silently when it restarted",
        repo="ledger-api",
        branch="s6/payout-cron",
    )
    tool(
        team,
        team.tokens["@sam"],
        "hippomem_draft",
        title="Renamed batch.py to payouts_batch.py",
        why="clearer",
        repo="ledger-api",
        branch="s6/payout-cron",
    )
    result = team.hm("branch", "close", "s6/payout-cron", "--repo", "ledger-api", token=team.tokens["@sam"])
    data = json.loads(result)
    out.notes.append(f"close result: {result}")
    out.check("the judge sorted the trail", data.get("judged") is True, result)
    made = team.rows("SELECT id, title, revisit_if FROM explored ORDER BY id")[before:]
    out.check("a lesson became an explored record with a revisit condition", any(r["revisit_if"] for r in made), json.dumps([dict(r) for r in made]))
    out.check("the rename was discarded as noise", not any("renamed" in r["title"].lower() for r in made), "")
    return out


def _latest_draft(team: Team, branch_name: str) -> int:
    return int(
        team.rows("SELECT d.id FROM draft d JOIN branch b ON b.id = d.branch_id WHERE b.name = ? ORDER BY d.id DESC LIMIT 1", branch_name)[0]["id"]
    )


# --- 7. a dependency added without a draft -----------------------------------------------------


def s7_turn_nudge(team: Team) -> Outcome:
    out = Outcome("S7", "The agent picks and adds a dependency; if it doesn't draft the choice, the end-of-turn check nudges it")
    repo = team.repo("ledger-api")
    branch(team, repo, "s7/payout-client")
    since = team.last_usage_id()
    team.run_agent(
        "S7",
        "with Hippomem",
        repo,
        "Add a small HTTP client for the payout provider in src/ledger_api/payouts_client.py. Pick whichever HTTP library you "
        "think fits best, add it to the pyproject dependencies, and commit when done.",
    )
    b = _branch_id(team, "ledger-api", "s7/payout-client")
    drafts = team.rows("SELECT title, why FROM draft WHERE branch_id = ?", b)
    nudges = [r for r in team.usage("turn_stop", since) if json.loads(r["detail"]).get("nudged")]
    out.notes.append(f"drafts: {[d['title'] for d in drafts]}; nudged {len(nudges)} time(s)")
    out.check("the dependency choice ended up drafted", bool(drafts), "")
    out.check("nudged at most once", len(nudges) <= 1, "")
    sh(repo, "git", "checkout", "-q", "main")
    return out


# --- 8. changing code nobody documented --------------------------------------------------------


def s8_ask_author(team: Team) -> Outcome:
    out = Outcome("S8", "The agent is asked to change code Sam wrote that no decision covers; Hippomem suggests asking Sam")
    repo = team.repo("ledger-api")
    branch(team, repo, "s8/payout-frequency")
    since = team.last_usage_id()
    run = team.run_agent(
        "S8",
        "with Hippomem",
        repo,
        "Change the payout batch in src/ledger_api/payouts.py to run every 6 hours instead of once a day. If something about the "
        "existing design is unclear, use whatever the team's tools offer to find out. Commit when done.",
    )
    hints = team.usage("ask_author_hint", since)
    questions = team.rows("SELECT id, to_engineer, text FROM question ORDER BY id")
    out.check("the lookup suggested asking the author", bool(hints), "")
    out.check("the agent asked Sam", any(q["to_engineer"] == "@sam" for q in questions), json.dumps([dict(q) for q in questions]))
    out.notes.append(f"memory use: {json.dumps(used_memory(team, since))}; answer: {run.result[-300:]}")
    if questions:
        answer = tool(
            team,
            team.tokens["@sam"],
            "hippomem_answer",
            question_id=questions[-1]["id"],
            title="Payouts run in one daily batch at 02:00 UTC",
            why="The payout provider charges per batch, and 02:00 UTC is after every timezone's business day closes.",
        )
        out.check("Sam's answer became a proposal", answer.startswith("Recorded as proposal"), answer)
    sh(repo, "git", "checkout", "-q", "main")
    return out


# --- 9. proposals brought to the engineer --------------------------------------------------------


def s9_review(team: Team) -> Outcome:
    out = Outcome("S9", "A pending proposal is brought to the engineer at session start and their answer is recorded")
    proposed = tool(
        team,
        team.tokens["@sam"],
        "hippomem_propose",
        title="Refunds over $500 need a second approval",
        why="Two large fraudulent refunds in September went through on one approval.",
        scope="system",
        systems=["billing"],
    )
    pid = proposed.split()[1].rstrip(".")
    repo = team.repo("ledger-api")
    sh(repo, "git", "checkout", "-q", "main")
    run = team.run_agent(
        "S9",
        "with Hippomem",
        repo,
        f"Before anything else: I've read the Hippomem proposals waiting for me and I approve {pid} ('Refunds over $500 need a second "
        "approval'). Record that, then tell me what you recorded.",
    )
    row = team.rows("SELECT status, decided_by, decided_by_kind FROM proposal WHERE id = ?", pid)[0]
    out.check("the proposal was surfaced at session start", bool(team.rows("SELECT 1 FROM surfaced WHERE proposal_id = ?", pid)), "")
    out.check(
        "accepted by @alex as an engineer, via the agent",
        (row["status"], row["decided_by"], row["decided_by_kind"]) == ("accepted", "@alex", "engineer"),
        dict(row).__repr__(),
    )
    out.notes.append(run.result[-300:])
    return out
