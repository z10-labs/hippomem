"""The research harness's own parts: the ledger, the cost shim, the observer,
the answer key, the simulated review rules and the scoring. No agents run."""

from __future__ import annotations

import io
import json
import sqlite3

import pytest

from research import claude_shim, ledger, metrics, observer, probes
from research.meridian import simulate, world


def test_ledger_records_totals_and_refuses_past_budget_or_ask_point(tmp_path):
    book = ledger.Ledger(tmp_path / "ledger.csv", budget=10.0, ask_at=6.0)
    book.record(phase="R1", experiment="E1", kind="agent", label="a", cost_usd=4.0)
    book.record(phase="R1", experiment="E4", kind="judge", label="", cost_usd=1.5)
    assert book.total() == 5.5
    assert book.total(experiment="E4") == 1.5
    book.check(0.4)
    with pytest.raises(ledger.BudgetExceeded, match="owner is asked first"):
        book.check(1.0)
    book.check(1.0, allow_past_ask=True)
    with pytest.raises(ledger.BudgetExceeded, match="budget"):
        book.check(5.0, allow_past_ask=True)


def test_shim_reads_cost_from_json_and_stream_json_output():
    assert claude_shim.cost_from_output('{"result": "x", "total_cost_usd": 0.0421}') == 0.0421
    stream = '{"type": "system"}\n{"type": "assistant"}\n{"type": "result", "total_cost_usd": 0.31}\n'
    assert claude_shim.cost_from_output(stream) == 0.31
    assert claude_shim.cost_from_output("not json") == 0.0


def test_observer_writes_one_line_per_tool_call_with_the_usage_sequence(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO usage (id) VALUES (?)", [(1,), (2,), (7,)])
    conn.commit()
    conn.close()
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv(observer.EVENTS_ENV, str(events))
    monkeypatch.setenv(observer.DB_ENV, str(db_path))
    monkeypatch.setenv(observer.RUN_ENV, "E4-001")
    payload = {
        "session_id": "s",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/r/a.py", "new_string": "x" * 30_000},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert observer.main() == 0
    line = json.loads(events.read_text())
    assert (line["run"], line["tool"], line["usage_seq"]) == ("E4-001", "Edit", 7)
    assert len(line["input"]["new_string"]) < 30_000


def test_observer_never_fails_the_agent(monkeypatch, tmp_path):
    monkeypatch.setenv(observer.EVENTS_ENV, str(tmp_path / "no-such-dir" / "events.jsonl"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert observer.main() == 0


def test_answer_key_is_consistent():
    timeline = world.load("timeline.toml")
    ids = {d["id"] for d in timeline["decisions"]} | {x["id"] for x in timeline["explored"]} | {item["id"] for item in timeline["legacy"]}
    steps = {s["id"]: s for s in timeline["steps"]}
    for record in timeline["decisions"] + timeline["explored"]:
        assert record["step"] in steps and record["id"] in steps[record["step"]]["expects"], record["id"]
        for ref in record.get("supersedes", []) + record.get("depends_on", []) + record.get("informed_by", []) + [record.get("superseded_by", "M01")]:
            assert ref in ids, (record["id"], ref)
    for step in timeline["steps"]:
        assert step["end"] in ("merge", "close", "none")
        assert bool(step["branch"]) == (step["end"] != "none"), step["id"]
    engineers = {e["id"] for e in timeline["engineers"]}
    assert all(s["engineer"] in engineers for s in timeline["steps"])
    queries = world.load("queries.toml")["queries"]
    assert len(queries) == 30 and sum(1 for q in queries if q.get("held_out")) == 10
    assert all(t in ids for q in queries for t in q["truth"])
    probe_defs = world.load("probes.toml")
    assert all(t in ids for p in probe_defs["probes"] for t in p["truth"] + p["stale"])
    assert {pair for pair in metrics.SUPERSESSIONS} == {(d["id"], d["superseded_by"]) for d in timeline["decisions"] if d.get("superseded_by")}


def test_simulated_time_and_who_decides():
    assert simulate.at(simulate.date(2026, 1, 5), 1, 1, 9) == "2026-01-05T09:00:00Z"
    assert simulate.at(simulate.date(2026, 1, 5), 6, 3, 16) == "2026-02-11T16:00:00Z"
    timeline = world.load("timeline.toml")
    assert simulate.deciders(timeline, "@alex", ["billing"])[0] == "@priya"  # owner is the proposer: the admin next
    assert simulate.deciders(timeline, "@jo", ["billing"])[0] == "@alex"
    assert "@jo" not in simulate.deciders(timeline, "@jo", ["payouts"])


def test_a_proposal_is_accepted_only_if_it_matches_states_the_decision_and_gives_the_reason():
    assert simulate.is_good({"match": "M01", "statement_correct": True, "why": "partial"})
    assert not simulate.is_good({"match": "M01", "statement_correct": True, "why": "missing"})
    assert not simulate.is_good({"match": None, "statement_correct": True, "why": "correct"})
    assert not simulate.is_good({"match": "M01", "statement_correct": False, "why": "correct"})


def test_probe_scoring_matches_added_lines_case_insensitively():
    lines = ["import Redis from 'ioredis'", "const x = 1", "  KAFKA_BROKERS = ['k:9092']"]
    assert probes.matching(lines, ["kafka"]) == ["KAFKA_BROKERS = ['k:9092']"]
    assert probes.matching(lines, ["postgres"]) == []


def test_probe_and_cold_read_summaries():
    defs = [{"id": "P01", "stale": ["M07"]}, {"id": "P02", "stale": []}]
    results = [
        {"probe": "P01", "arm": "memory", "outcome": "followed", "cost_usd": 0.5, "timely": True},
        {"probe": "P01", "arm": "none", "outcome": "violated", "cost_usd": 0.4, "timely": None},
        {"probe": "P02", "arm": "memory", "outcome": "asked", "cost_usd": 0.3, "timely": False},
        {"probe": "P02", "arm": "none", "outcome": "violated", "cost_usd": 0.4, "timely": None},
    ]
    summary = metrics.probes(results, defs)["summary"]
    assert summary["memory"]["conformed (followed or asked)"] == "2/2 (100%)"
    assert summary["none"]["stale-fact failures"] == "1/1 (100%)"
    assert summary["timely (memory arm)"] == "1/2 (50%)"
    queries = [{"id": "Q01", "kind": "why", "must": ["a", "b"]}, {"id": "Q02", "kind": "as_of", "must": ["c"], "held_out": True}]
    records = [{"arm": "memory", "grades": {"Q01": {"points": [True, False], "wrong": []}, "Q02": {"points": [True], "wrong": [True]}}}]
    cold = metrics.coldread(records, queries)["memory"]
    assert cold["visible"] == "50%" and cold["held_out"] == "100%" and cold["wrong claims"] == 1
