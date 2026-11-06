"""Supersession and the general graph (research R2): a new decision that
replaces one in force must take it out of force, and links say who or what
derived them."""

from __future__ import annotations

import sqlite3

from hippomem import config, db, decisions, drafts, explored, lifecycle, proposals, search, service
from hippomem.policy import Actor
from tests.helpers import ALEX, KIM, POLICY, accepted_decision

ALEX_AGENT = Actor("@alex", "engineer", "claude-code")


def branch(conn, name="feat/gateway"):
    return drafts.ensure_branch(conn, "product-api", name)


def add(conn, branch_id, title, why="because", **kw):
    return drafts.add(conn, branch_id, title=title, why=why, engineer="@sam", agent="claude-code", session_id="s1", **kw)


def merge(conn, name="feat/gateway"):
    return lifecycle.on_merge(conn, POLICY, "product-api", name, "main", "diff --git a/x b/x", ["c1"], history="--- commit c1\\nwork")


def test_a_draft_that_names_what_it_replaces_becomes_a_supersession_and_takes_it_out_of_force(conn, judge_stub):
    old = accepted_decision(conn, "Rate limit with Postgres counters", "no Redis allowed")
    b = branch(conn)
    d = add(conn, b, "Rate limit in the API gateway", "one place for every service", supersedes=old)
    drafts.mark_backed(conn, b, "c1", [])
    judge_stub.reply = {"drafts": [{"id": d, "verdict": "built", "title": "Rate limit in the gateway", "why": "one place", "files": []}]}
    pid = merge(conn)["proposals"][0]
    p = proposals.get(conn, pid)
    assert (p.kind, p.target) == ("supersede", old)
    new = proposals.accept(conn, POLICY, pid, KIM)
    replaced = decisions.get(conn, old)
    assert replaced.status == "superseded" and replaced.valid_to is not None
    link = next(lk for lk in replaced.links if lk.type == "superseded-by")
    assert (link.other, link.derived_by) == (new, "rule")
    assert old not in [x.id for x in decisions.all_current(conn, ("accepted",))]


def test_the_judge_marks_a_replacement_the_agent_did_not_name(conn, judge_stub):
    old = accepted_decision(conn, "Rate limit with Postgres counters", "no Redis allowed")
    b = branch(conn)
    d = add(conn, b, "Rate limit in the gateway instead of Postgres counters", "one place for every service")
    drafts.mark_backed(conn, b, "c1", [])
    judge_stub.reply = {"drafts": [{"id": d, "verdict": "built", "replaces": old, "why": "one place", "files": []}]}
    p = proposals.get(conn, merge(conn)["proposals"][0])
    assert (p.kind, p.target) == ("supersede", old)
    assert old in judge_stub.prompts[0]  # the judge was shown the decision in force


def test_a_draft_that_only_repeats_a_decision_becomes_evidence_not_a_second_decision(conn, judge_stub):
    old = accepted_decision(conn, "Money is integer minor units", "floats round wrongly")
    b = branch(conn, "feat/money")
    d = add(conn, b, "Store money as integer cents", "floats round wrongly")
    drafts.mark_backed(conn, b, "c1", [])
    judge_stub.reply = {"drafts": [{"id": d, "verdict": "built", "repeats": old, "files": ["src/money.py"]}]}
    p = proposals.get(conn, merge(conn, "feat/money")["proposals"][0])
    assert (p.kind, p.target, p.status) == ("evidence", old, "accepted")
    assert len(decisions.all_current(conn, ("accepted",))) == 1


def test_replaces_must_name_a_decision_in_force(conn, judge_stub):
    b = branch(conn)
    d = add(conn, b, "Rate limit in the gateway", "one place")
    drafts.mark_backed(conn, b, "c1", [])
    judge_stub.reply = {"drafts": [{"id": d, "verdict": "built", "replaces": "D-0999", "why": "one place", "files": []}]}
    assert proposals.get(conn, merge(conn)["proposals"][0]).kind == "new"


def test_undrafted_decisions_need_a_written_reason_and_are_capped(conn, judge_stub):
    judge_stub.reply = {
        "drafts": [],
        "undrafted": [
            {"title": "Hash request bodies with SHA-256", "why": "not recorded"},
            {"title": "Use an outbox table", "why": "commit says: publish in the same transaction as the refund"},
            {"title": "Retry three times", "why": "commit says: most failures are transient"},
            {"title": "Poll every 5 seconds", "why": "commit says: low latency is enough"},
        ],
    }
    branch(conn, "feat/events")
    found = merge(conn, "feat/events")["proposals"]
    titles = [proposals.get(conn, pid).title for pid in found]
    assert titles == ["Use an outbox table", "Retry three times"]


def test_a_draft_restated_only_to_mark_what_it_replaces_is_not_its_own_alternative(conn, judge_stub):
    old = accepted_decision(conn, "Rate limit with Postgres counters", "no Redis allowed")
    b = branch(conn)
    first = add(conn, b, "Rate limit in the gateway", "one place")
    second = add(conn, b, "Rate limit in the gateway", "one place", replaces=first, replaced_because="restated", supersedes=old)
    drafts.mark_backed(conn, b, "c1", [])
    judge_stub.reply = {"drafts": [{"id": first, "verdict": "alternative", "of": second}, {"id": second, "verdict": "built", "files": []}]}
    p = proposals.get(conn, merge(conn)["proposals"][0])
    assert p.payload["alternatives"] == [] and p.kind == "supersede"


def test_a_team_wide_draft_can_supersede_a_team_rule(conn, judge_stub):
    old = accepted_decision(conn, "Postgres is the only datastore", "platform is one person", scope="team", systems_=())
    b = branch(conn, "feat/redis-cache")
    d = add(conn, b, "Redis allowed as a cache only", "platform is three people now", scope="team")
    judge_stub.reply = {"drafts": [], "scope": [{"id": d, "replaces": old}]}
    p = proposals.get(conn, merge(conn, "feat/redis-cache")["proposals"][0])
    assert (p.kind, p.target, p.source) == ("supersede", old, "scope")


def test_drafting_names_related_decisions_in_force(conn):
    mem = service.Memory(conn=conn, config=config.Config())
    old = accepted_decision(conn, "Rate limit POST endpoints with Postgres counters", "no Redis allowed")
    statement = "Rate limit POST endpoints in the gateway rather than with Postgres counters"
    text = service.draft(
        mem, ALEX_AGENT, title="Rate limit POST endpoints in the gateway", statement=statement, why="one place", repo="product-api", branch="feat/gw"
    )
    assert f"{old} Rate limit POST endpoints with Postgres counters" in text and "supersedes=" in text
    marked = service.draft(mem, ALEX_AGENT, title="Rate limit in the gateway", why="one place", supersedes=old, repo="product-api", branch="feat/gw")
    assert f"Marked as replacing {old}" in marked and "look related" not in marked


def test_draft_supersedes_must_be_in_force(conn):
    b = branch(conn)
    try:
        add(conn, b, "Something", supersedes="D-0404")
    except ValueError as err:
        assert "decision in force" in str(err)
    else:
        raise AssertionError("expected a ValueError")


def test_a_decision_can_be_informed_by_an_explored_record_and_the_chain_shows_it(conn):
    spike = explored.create(conn, title="NATS for events", tried="NATS", outcome="abandoned", why="one more service", revisit_if="platform grows")
    informed = decisions.Link("informed-by", spike, "judge", 0.8)
    d = accepted_decision(conn, "Refund events go through an outbox", "no new infrastructure", links=(informed,))
    link = decisions.get(conn, d).links[0]
    assert (link.type, link.other, link.derived_by, link.confidence) == ("informed-by", spike, "judge", 0.8)
    tree = search.chain(conn, d)
    assert tree["depends_on"][0]["id"] == spike and tree["depends_on"][0]["status"] == "explored"
    assert search.query(conn, "refund events outbox").decisions  # expansion skips the explored record without failing


def test_migration_2_keeps_links_and_dates_replaced_versions(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.executescript("BEGIN;" + db.MIGRATIONS[0] + "PRAGMA user_version = 1; COMMIT;")
    conn.executescript(
        """
        INSERT INTO decision VALUES ('D-0001', 1, 'superseded', '2026-01-01T00:00:00Z'), ('D-0002', 1, 'accepted', '2026-02-01T00:00:00Z');
        INSERT INTO decision_version (decision_id, version, title, statement, why, weight, scope, valid_from, recorded_at, made_by,
            accepted_by, accepted_by_kind, proposal_id)
        VALUES ('D-0001', 1, 'old', 'old', 'w', 'standard', 'code', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '@a', '@b', 'engineer', 'P-1'),
               ('D-0002', 1, 'new', 'new', 'w', 'standard', 'code', '2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z', '@a', '@b', 'engineer', 'P-2');
        INSERT INTO link VALUES ('D-0002', 'supersedes', 'D-0001');
        """
    )
    conn.close()
    migrated = db.connect(path)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    row = migrated.execute("SELECT from_id, type, to_id, derived_by FROM link").fetchone()
    assert tuple(row) == ("D-0002", "supersedes", "D-0001", "engineer")
    assert decisions.get(migrated, "D-0001").valid_to == "2026-02-01T00:00:00Z"
    assert decisions.get(migrated, "D-0002").valid_to is None


def test_accepting_without_reconciliation_still_records_supersession(conn):
    old = accepted_decision(conn, "Batches of 100", "5 requests a second")
    p = proposals.create(
        conn,
        POLICY,
        kind="supersede",
        target=old,
        payload={"title": "Send payouts one by one", "why": "limit raised to 50/s"},
        source="agent",
        proposed_by="@sam",
        proposed_via="claude-code",
    )
    proposals.accept(conn, POLICY, p.id, ALEX)
    assert decisions.get(conn, old).status == "superseded"


def test_get_shows_the_lineage_with_dates(conn, monkeypatch):
    mem = service.Memory(conn=conn, config=config.Config())
    monkeypatch.setattr(db, "now", lambda: "2026-01-20T09:00:00Z")
    first = accepted_decision(conn, "Batches of 100", "5 requests a second")
    monkeypatch.setattr(db, "now", lambda: "2026-02-23T09:00:00Z")
    payload = {"title": "One by one, 20 at a time", "why": "limit raised to 50/s"}
    p = proposals.create(conn, POLICY, kind="supersede", target=first, payload=payload, source="agent", proposed_by="@sam")
    second = proposals.accept(conn, POLICY, p.id, ALEX)
    text = service.get_decision(mem, ALEX_AGENT, first)
    assert f"→ {first}  Batches of 100: from 2026-01-20 until 2026-02-23" in text
    assert f"{second}  One by one, 20 at a time: from 2026-02-23 (in force)" in text
    assert [d.id for d in search.lineage(conn, second)] == [first, second]


def test_a_decision_that_stays_true_is_amended_not_superseded(conn, judge_stub):
    keys = accepted_decision(conn, "Payout requests carry idempotency keys for bulk payouts", "duplicate payouts incident")
    b = branch(conn, "feat/individual")
    d = add(conn, b, "Send payouts one by one, 20 at a time", "limit raised to 50/s")
    drafts.mark_backed(conn, b, "c1", [])
    judge_stub.reply = {"drafts": [{"id": d, "verdict": "built", "amends": keys, "why": "limit raised", "files": []}]}
    p = proposals.get(conn, merge(conn, "feat/individual")["proposals"][0])
    assert p.kind == "new" and {"type": "informed-by", "to": keys, "derived_by": "judge"} in p.payload["links"]
    new = proposals.accept(conn, POLICY, p.id, ALEX)
    assert decisions.get(conn, keys).status == "accepted"
    assert decisions.Link("informed-by", keys) in decisions.get(conn, new).links


def test_a_supersession_tells_the_reviewer_what_leaves_force(conn):
    from hippomem import render

    old = accepted_decision(conn, "Batches of 100", "5 requests a second")
    p = proposals.create(
        conn, POLICY, kind="supersede", target=old, payload={"title": "One by one", "why": "limit raised"}, source="agent", proposed_by="@sam"
    )
    assert f"takes {old} (Batches of 100) OUT OF FORCE" in render.proposal_full(p)
