"""Instruments the research harness relies on: a settable clock and usage
records that say exactly what each response showed the agent."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from hippomem import config, db, drafts, lifecycle, service
from hippomem.policy import Actor
from hippomem.decisions import Link
from tests.helpers import accepted_decision

ALEX_AGENT = Actor("@alex", "engineer", "claude-code")


def last_usage(conn, action: str) -> dict:
    row = conn.execute("SELECT detail FROM usage WHERE action = ? ORDER BY id DESC LIMIT 1", (action,)).fetchone()
    return json.loads(row[0])


def test_clock_file_sets_the_time_when_the_env_var_is_set(tmp_path, monkeypatch):
    clock = tmp_path / "clock"
    clock.write_text("2026-03-02T09:00:00Z\n")
    monkeypatch.setenv(db.CLOCK_FILE_ENV, str(clock))
    assert db.utc_now() == "2026-03-02T09:00:00Z"
    clock.write_text("2026-04-13T17:30:00Z")
    assert db.utc_now() == "2026-04-13T17:30:00Z"


def test_system_clock_is_used_without_the_env_var(monkeypatch):
    monkeypatch.delenv(db.CLOCK_FILE_ENV, raising=False)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    assert db.parse_time(db.utc_now()) >= before


def test_close_stale_measures_idleness_against_the_memory_clock(conn, monkeypatch):
    monkeypatch.setattr(db, "now", lambda: "2026-03-01T00:00:00Z")
    drafts.ensure_branch(conn, "product-api", "feat/old")
    monkeypatch.setattr(db, "now", lambda: "2026-03-20T00:00:00Z")
    assert lifecycle.close_stale(conn, 30) == []
    monkeypatch.setattr(db, "now", lambda: "2026-04-15T00:00:00Z")
    assert lifecycle.close_stale(conn, 30) == ["product-api:feat/old"]


def test_shown_ids_lists_each_record_once_in_order():
    text = "D-0002  Use Postgres\n  ↳ depends-on D-0001\nX-0001 EXPLORED\nP-0010 from @sam\nD-0002 again, Q-0003"
    assert service.shown_ids(text) == ["D-0002", "D-0001", "X-0001", "P-0010", "Q-0003"]


def test_query_usage_records_scores_and_how_each_hit_was_reached(conn):
    mem = service.Memory(conn=conn, config=config.Config())
    base = accepted_decision(conn, "Use Postgres for rate limit counters", "no new infrastructure")
    accepted_decision(conn, "Rate limit windows are per customer", "fairness", links=(Link("depends-on", base),))
    service.query(mem, ALEX_AGENT, "rate limit counters", repo="product-api", path="src/billing/limits.py")
    detail = last_usage(conn, "query")
    assert detail["path"] == "src/billing/limits.py"
    hits = {h["id"]: h for h in detail["decisions"]}
    assert base in hits
    assert all(isinstance(h["score"], float) and "via" in h and "in_scope" in h for h in hits.values())


def test_session_start_and_lookup_record_what_the_agent_was_shown(conn):
    mem = service.Memory(conn=conn, config=config.Config())
    decision_id = accepted_decision(conn, "Billing owns all money writes", "one place to audit", scope="system")
    service.session_start(mem, ALEX_AGENT, "s1", "product-api", "feat/refunds")
    assert decision_id in last_usage(conn, "session_start")["shown"]
    service.lookup(mem, ALEX_AGENT, "s1", "product-api", "src/billing/refunds.py")
    assert decision_id in last_usage(conn, "lookup")["shown"]
