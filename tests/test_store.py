from __future__ import annotations

import pytest

from hippomem import db, decisions, explored, systems


def test_migrations_apply_once_and_set_user_version(tmp_path):
    path = tmp_path / "memory.db"
    first = db.connect(path)
    assert first.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    first.close()
    again = db.connect(path)  # must not try to re-create tables
    assert again.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    again.close()


def test_next_id_is_sequential_per_prefix(conn):
    assert db.next_id(conn, "D") == "D-0001"
    assert db.next_id(conn, "D") == "D-0002"
    assert db.next_id(conn, "P") == "P-0001"


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            systems.create_system(conn, "queue", "Queue")
            raise RuntimeError("boom")
    assert not systems.system_exists(conn, "queue")


def test_one_system_spans_many_repos(conn):
    assert systems.systems_for(conn, "product-worker") == ["billing"]
    assert systems.systems_for(conn, "product-api", "src/billing/refunds.py") == ["billing"]
    assert systems.repos_of(conn, "billing") == ["product-api", "product-worker"]


def test_unmapped_path_belongs_to_no_system(conn):
    assert systems.systems_for(conn, "product-api", "src/sessions/booking.py") == []
    assert systems.systems_for(conn, "unknown-repo") == []


def test_proposed_mappings_do_not_count_until_confirmed(conn):
    systems.add_mapping(conn, "product-api", "src/sessions/**", "web", created_by="@sam", status="proposed")
    assert systems.systems_for(conn, "product-api", "src/sessions/x.py") == []
    systems.add_mapping(conn, "product-api", "src/sessions/**", "web", created_by="@alex", status="confirmed")
    assert systems.systems_for(conn, "product-api", "src/sessions/x.py") == ["web"]


def test_shared_system_dependencies_are_included(conn):
    systems.create_system(conn, "ui-kit", "UI kit")
    systems.add_dependency(conn, "web", "ui-kit")
    assert systems.systems_for(conn, "product-web", "app/page.tsx") == ["web", "ui-kit"]


def test_path_matching():
    assert systems.path_matches("src/billing/a/b.py", "src/billing/**")
    assert systems.path_matches("src/billing", "src/billing/**")
    assert systems.path_matches("anything.py", "**")
    assert not systems.path_matches("src/billingx/a.py", "src/billing/**")


def test_decision_versions_keep_history(conn):
    content = decisions.Content(title="Use advisory locks", statement="s", why="w", systems=("billing",))
    decision_id = decisions.create(
        conn, content, made_by="@sam", made_via="claude-code", accepted_by="@alex", accepted_by_kind="engineer", proposal_id="P-x"
    )
    v2 = decisions.Content(title="Use advisory locks with a timeout", statement="s2", why="w2", systems=("billing", "web"))
    decisions.add_version(conn, decision_id, v2, made_by="@sam", made_via="", accepted_by="@alex", accepted_by_kind="engineer", proposal_id="P-y")
    current = decisions.get(conn, decision_id)
    assert current.version == 2
    assert current.title == "Use advisory locks with a timeout"
    assert current.systems == ("billing", "web")
    old_title = conn.execute("SELECT title FROM decision_version WHERE decision_id = ? AND version = 1", (decision_id,)).fetchone()[0]
    assert old_title == "Use advisory locks"


def test_links_are_readable_from_both_ends(conn):
    c = decisions.Content(title="a", statement="", why="w")
    a = decisions.create(conn, c, made_by="@sam", made_via="", accepted_by="@alex", accepted_by_kind="engineer", proposal_id="P-1")
    b = decisions.create(conn, c, made_by="@sam", made_via="", accepted_by="@alex", accepted_by_kind="engineer", proposal_id="P-2")
    decisions.add_link(conn, b, "depends-on", a)
    assert decisions.get(conn, b).links == (decisions.Link("depends-on", a),)
    assert decisions.get(conn, a).links == (decisions.Link("depended-on-by", b),)


def test_unknown_link_type_is_rejected(conn):
    c = decisions.Content(title="a", statement="", why="w")
    a = decisions.create(conn, c, made_by="@sam", made_via="", accepted_by="@alex", accepted_by_kind="engineer", proposal_id="P-1")
    with pytest.raises(ValueError):
        decisions.add_link(conn, a, "likes", a)


def test_explored_record_requires_revisit_condition(conn):
    with pytest.raises(ValueError, match="revisit_if"):
        explored.create(conn, title="Kafka", tried="2-day spike", outcome="dropped", why="ops cost", revisit_if=" ")
    rid = explored.create(conn, title="Kafka", tried="2-day spike", outcome="dropped", why="ops cost", revisit_if="volume > 100k/day")
    assert explored.get(conn, rid).revisit_if == "volume > 100k/day"
