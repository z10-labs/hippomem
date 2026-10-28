from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hippomem import decisions, drafts, explored, judge, lifecycle, proposals, search
from tests.helpers import ALEX, POLICY


def trail_branch(conn, branch="feat/refund-window", repo="product-api"):
    return drafts.ensure_branch(conn, repo, branch)


def add(conn, branch_id, title, why="because", **kw):
    return drafts.add(conn, branch_id, title=title, why=why, engineer="@sam", agent="claude-code", session_id="s1", **kw)


# --- drafts -----------------------------------------------------------------


def test_branch_is_reused_while_active_and_new_after_it_ends(conn):
    first = trail_branch(conn)
    assert trail_branch(conn) == first
    drafts.end_branch(conn, first, "merged")
    assert trail_branch(conn) != first


def test_replacing_a_draft_drops_it_with_the_reason(conn):
    b = trail_branch(conn)
    stored = add(conn, b, "Store the refund deadline on the order")
    computed = add(
        conn, b, "Compute the deadline from the delivery date", replaces=stored, replaced_because="goes stale when delivery dates are corrected"
    )
    old = drafts.get(conn, stored)
    assert (old.status, old.replaced_by, old.dropped_reason) == ("dropped", computed, "goes stale when delivery dates are corrected")
    assert [d.id for d in drafts.trail(conn, b)] == [stored, computed]


def test_draft_validation(conn):
    b = trail_branch(conn)
    other = trail_branch(conn, "feat/other")
    elsewhere = add(conn, other, "x")
    with pytest.raises(ValueError, match="not on this branch"):
        add(conn, b, "y", replaces=elsewhere)
    with pytest.raises(ValueError, match="kind"):
        add(conn, b, "y", kind="maybe")
    with pytest.raises(ValueError, match="unknown system"):
        add(conn, b, "y", system_id="nope")
    with pytest.raises(ValueError, match="why the draft was dropped"):
        drafts.drop(conn, elsewhere, " ")


def test_commit_backs_drafts_whose_paths_it_touched(conn):
    b = trail_branch(conn)
    touched = add(conn, b, "Refunds use the delivery date", paths=("src/billing/refunds",))
    untouched = add(conn, b, "Invoices stay immutable", paths=("src/billing/invoices.py",))
    anywhere = add(conn, b, "No paths given")
    result = lifecycle.on_commit(conn, POLICY, "product-api", "feat/refund-window", "abc123", ["src/billing/refunds/window.py"])
    assert sorted(result["backed"]) == [touched, anywhere]
    assert drafts.get(conn, untouched).backed_by is None


def test_other_branches_drafts_show_up_in_search_as_in_progress(conn):
    mine = trail_branch(conn, "feat/mine")
    theirs = trail_branch(conn, "feat/gateway-limits")
    add(conn, theirs, "Move rate limiting into the API gateway", why="one place for all limits")
    results = search.query(conn, "rate limiting in the api gateway", repo="product-api", branch_id=mine)
    assert [(h.branch, h.own_branch) for h in results.drafts] == [("feat/gateway-limits", False)]


# --- merge --------------------------------------------------------------------


def built(draft_id, **extra):
    return {"id": draft_id, "verdict": "built", "weight": "standard", "files": ["src/billing/refunds/**"], **extra}


def test_merge_turns_built_drafts_into_proposals_and_reversals_into_alternatives(conn, judge_stub):
    b = trail_branch(conn)
    lost = add(conn, b, "Store the refund deadline on the order", paths=("src/billing/refunds",))
    won = add(conn, b, "Compute the deadline from the delivery date", why="always correct", replaces=lost, replaced_because="goes stale")
    noise = add(conn, b, "Rename RefundSvc to RefundService")
    judge_stub.reply = {
        "drafts": [
            {"id": lost, "verdict": "alternative", "of": won, "reason": "goes stale when delivery dates are corrected"},
            built(won, title="Refund deadline is computed from the delivery date", statement="computed, never stored"),
            {"id": noise, "verdict": "discard"},
        ]
    }
    result = lifecycle.on_merge(conn, POLICY, "product-api", "feat/refund-window", "main", "diff --git ...", ["abc123"])
    assert result["judged"] and len(result["proposals"]) == 1
    p = proposals.get(conn, result["proposals"][0])
    assert (p.source, p.proposed_by, p.proposed_via) == ("reconciliation", "@sam", "claude-code")
    assert p.payload["alternatives"] == [
        {"option": "Store the refund deadline on the order", "reason": "goes stale when delivery dates are corrected"}
    ]
    assert p.payload["systems"] == ["billing"] and p.payload["implemented"] is True
    assert {"kind": "commit", "ref": "abc123"} in p.payload["evidence"]
    assert drafts.get(conn, lost).status == "alternative" and drafts.get(conn, noise).status == "discarded"
    assert drafts.get_branch(conn, b).state == "merged"

    decision_id = proposals.accept(conn, POLICY, p.id, ALEX)
    d = decisions.get(conn, decision_id)
    assert d.implemented and d.anchors == (decisions.Anchor("product-api", "src/billing/refunds/**"),)
    assert d.alternatives[0].option == "Store the refund deadline on the order"


def test_merge_falls_back_to_the_trail_when_the_judge_fails(conn, judge_stub):
    b = trail_branch(conn)
    lost = add(conn, b, "Redis counters", paths=("src/limits",))
    won = add(conn, b, "Postgres advisory locks", paths=("src/limits",), replaces=lost, replaced_because="another service to run")
    lifecycle.on_commit(conn, POLICY, "product-api", "feat/refund-window", "c1", ["src/limits/lock.py"])
    result = lifecycle.on_merge(conn, POLICY, "product-api", "feat/refund-window", "main", "diff", ["c1"])
    assert not result["judged"] and "no judge reply" in result["error"]
    p = proposals.get(conn, result["proposals"][0])
    assert p.payload["title"] == "Postgres advisory locks"
    assert p.payload["alternatives"] == [{"option": "Redis counters", "reason": "another service to run"}]
    assert drafts.get(conn, won).status == "proposed"
    failures = conn.execute("SELECT payload FROM event WHERE kind = 'judge_failed'").fetchall()
    assert len(failures) == 1


def test_merge_keeps_explored_lessons_and_ignores_explored_without_revisit_condition(conn, judge_stub):
    b = trail_branch(conn)
    kafka = add(conn, b, "Use Kafka for refund events")
    drafts.drop(conn, kafka, "ops cost too high at our volume")
    vague = add(conn, b, "Try a cron job")
    drafts.drop(conn, vague, "didn't fit")
    judge_stub.reply = {
        "drafts": [
            {"id": kafka, "verdict": "explored", "tried": "Kafka spike", "outcome": "dropped", "why": "ops cost", "revisit_if": "volume > 100k/day"},
            {"id": vague, "verdict": "explored", "revisit_if": ""},
        ]
    }
    result = lifecycle.on_merge(conn, POLICY, "product-api", "feat/refund-window", "main", "diff", [])
    assert len(result["explored"]) == 1
    x = explored.get(conn, result["explored"][0])
    assert (x.revisit_if, x.system_id, x.repo) == ("volume > 100k/day", "billing", "product-api")
    assert drafts.get(conn, vague).status == "discarded"


def test_scope_wide_drafts_go_to_owners_without_the_judge(conn, judge_stub):
    b = trail_branch(conn)
    add(conn, b, "Billing owns all money writes", why="one place to audit", scope="system", system_id="billing")
    result = lifecycle.on_merge(conn, POLICY, "product-api", "feat/refund-window", "main", "diff", [])
    p = proposals.get(conn, result["proposals"][0])
    assert (p.source, p.payload["scope"], p.payload["systems"]) == ("scope", "system", ["billing"])
    assert judge_stub.prompts == []


def test_merge_of_unknown_branch_is_ignored_and_logged(conn):
    assert "ignored" in lifecycle.on_merge(conn, POLICY, "product-api", "feat/ghost", "main", "", [])
    assert conn.execute("SELECT COUNT(*) FROM event WHERE kind = 'merge_unknown_branch'").fetchone()[0] == 1


def test_commits_on_the_default_branch_are_reconciled_one_by_one(conn, judge_stub):
    b = trail_branch(conn, "main")
    d = add(conn, b, "Retries use exponential backoff", paths=("src/http",))
    add(conn, b, "Unrelated thought", paths=("docs",))
    judge_stub.reply = lambda prompt: {"drafts": [built(d)]}
    result = lifecycle.on_commit(conn, POLICY, "product-api", "main", "c9", ["src/http/client.py"], diff="diff")
    assert len(result["proposals"]) == 1
    assert '"Unrelated thought"' not in judge_stub.prompts[0]
    assert drafts.get_branch(conn, b).state == "active"


# --- close --------------------------------------------------------------------


def test_closed_branch_keeps_only_lasting_dead_ends(conn, judge_stub):
    b = trail_branch(conn, "feat/kafka-queue")
    spike = add(conn, b, "Kafka for the job queue")
    noise = add(conn, b, "Named the topic jobs.v1")
    judge_stub.reply = {
        "drafts": [
            {
                "id": spike,
                "verdict": "explored",
                "title": "Kafka for the job queue",
                "tried": "2-day spike",
                "outcome": "dropped",
                "why": "ops cost at ~1k jobs/day",
                "revisit_if": "volume > 100k/day",
            },
            {"id": noise, "verdict": "discard"},
        ]
    }
    result = lifecycle.on_close(conn, "product-api", "feat/kafka-queue")
    assert len(result["explored"]) == 1 and result["discarded"] == [noise]
    found = search.query(conn, "use kafka for the job queue")
    assert found.explored[0].record.title == "Kafka for the job queue"


def test_failed_close_leaves_the_trail_to_sort_again(conn, judge_stub):
    b = trail_branch(conn, "feat/kafka-queue")
    d = add(conn, b, "Kafka for the job queue")
    result = lifecycle.on_close(conn, "product-api", "feat/kafka-queue")
    assert not result["judged"]
    assert drafts.get(conn, d).status == "open" and drafts.get_branch(conn, b).state == "closed"
    judge_stub.reply = {
        "drafts": [{"id": d, "verdict": "explored", "tried": "spike", "outcome": "dropped", "why": "ops", "revisit_if": "volume grows"}]
    }
    assert len(lifecycle.sort_again(conn, b)["explored"]) == 1


def test_stale_branches_are_closed_but_default_branches_never(conn, judge_stub):
    old = trail_branch(conn, "feat/forgotten")
    trail_branch(conn, "main")
    conn.execute("UPDATE branch SET last_activity_at = '2026-01-01T00:00:00Z'")
    closed = lifecycle.close_stale(conn, 30, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert closed == ["product-api:feat/forgotten"]
    assert drafts.get_branch(conn, old).state == "closed"


def test_checkout_ignores_detached_head(conn):
    assert "ignored" in lifecycle.on_checkout(conn, "product-api", "HEAD")
    assert "branch_id" in lifecycle.on_checkout(conn, "product-api", "feat/x")


def test_extract_json_handles_fenced_replies():
    assert judge.extract_json('Here you go:\n```json\n{"drafts": []}\n```') == {"drafts": []}
    with pytest.raises(judge.JudgeError):
        judge.extract_json("no json here")
    with pytest.raises(judge.JudgeError):
        judge.extract_json("{not: json}")


def test_merge_records_decisions_only_the_history_shows(conn, judge_stub):
    trail_branch(conn)
    judge_stub.reply = {
        "drafts": [],
        "undrafted": [
            {
                "title": "Refund deadline is computed from delivered_at",
                "why": "a stored deadline went stale when delivery dates were corrected",
                "files": ["src/billing/orders.py"],
                "alternatives": [{"option": "Store refund_deadline on the order", "reason": "goes stale"}],
            },
            {"title": " "},
        ],
    }
    result = lifecycle.on_merge(
        conn,
        POLICY,
        "product-api",
        "feat/refund-window",
        "main",
        "diff",
        ["c1"],
        history="--- commit a store\n--- commit b compute",
        merged_by="@alex",
    )
    assert len(result["proposals"]) == 1
    p = proposals.get(conn, result["proposals"][0])
    assert (p.proposed_by, p.proposed_via, p.payload["inferred"]) == ("@alex", "git", True)
    assert p.payload["alternatives"] == [{"option": "Store refund_deadline on the order", "reason": "goes stale"}]
    assert "--- commit b compute" in judge_stub.prompts[0]


def test_judge_alternatives_join_the_trail_alternatives_without_duplicates(conn, judge_stub):
    b = trail_branch(conn)
    lost = add(conn, b, "Redis counters")
    won = add(conn, b, "Postgres locks", replaces=lost, replaced_because="another service")
    judge_stub.reply = {
        "drafts": [built(won, alternatives=[{"option": "redis counters", "reason": "dup"}, {"option": "In-memory", "reason": "replicas"}])]
    }
    result = lifecycle.on_merge(conn, POLICY, "product-api", "feat/refund-window", "main", "diff", [])
    options = [a["option"] for a in proposals.get(conn, result["proposals"][0]).payload["alternatives"]]
    assert options == ["Redis counters", "In-memory"]
