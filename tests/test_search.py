from __future__ import annotations

from hippomem import decisions, explored, search
from tests.helpers import accepted_decision


def test_direct_match_comes_back_with_its_rejected_options(conn):
    d = accepted_decision(
        conn,
        "Rate limiting uses Postgres advisory locks",
        "no new infrastructure",
        alternatives=(decisions.Alternative("Redis counters", "another service to run"),),
    )
    results = search.query(conn, "rate limiting with redis counters")
    assert results.decisions[0].decision.id == d
    assert results.decisions[0].via == "direct"


def test_irrelevant_query_returns_nothing(conn):
    accepted_decision(conn, "Rate limiting uses Postgres advisory locks")
    assert search.query(conn, "font sizes in the marketing site").empty


def test_graph_expansion_returns_what_depends_on_a_match(conn):
    base = accepted_decision(conn, "Postgres is the only datastore")
    dependent = accepted_decision(conn, "Background jobs claim work with SKIP LOCKED", links=(decisions.Link("depends-on", base),))
    results = search.query(conn, "postgres only datastore")
    by_id = {h.decision.id: h for h in results.decisions}
    assert by_id[base].via == "direct"
    assert by_id[dependent].via == f"depended-on-by {base}"


def test_superseded_decision_is_demoted_but_not_hidden(conn):
    from hippomem import proposals
    from tests.helpers import ALEX, POLICY

    old = accepted_decision(conn, "Rate limiting uses redis counters")
    p = proposals.create(
        conn,
        POLICY,
        kind="supersede",
        target=old,
        payload={"title": "Rate limiting uses postgres advisory locks", "why": "drop redis", "systems": ["billing"]},
        source="agent",
        proposed_by="@sam",
    )
    new = proposals.accept(conn, POLICY, p.id, ALEX)
    results = search.query(conn, "rate limiting uses redis counters")
    ids = [h.decision.id for h in results.decisions]
    assert old in ids and new in ids
    old_hit = next(h for h in results.decisions if h.decision.id == old)
    assert old_hit.decision.status == "superseded"


def test_out_of_scope_decisions_rank_below_in_scope_ones(conn):
    web = accepted_decision(conn, "Cache pages for five minutes", systems_=("web",))
    billing = accepted_decision(conn, "Cache invoices for five minutes", systems_=("billing",))
    results = search.query(conn, "cache for five minutes", repo="product-worker")
    order = [h.decision.id for h in results.decisions if h.via == "direct"]
    assert order.index(billing) < order.index(web)
    assert next(h for h in results.decisions if h.decision.id == web).in_scope is False


def test_team_wide_decisions_are_in_scope_everywhere(conn):
    team = accepted_decision(conn, "Services never read each other's databases", systems_=(), scope="team")
    results = search.query(conn, "can services read each other's databases", repo="product-web")
    assert next(h for h in results.decisions if h.decision.id == team).in_scope


def test_explored_records_come_back_separately(conn):
    x = explored.create(conn, title="Kafka for the job queue", tried="2-day spike", outcome="dropped", why="ops cost", revisit_if="volume > 100k/day")
    results = search.query(conn, "use kafka for the job queue")
    assert [h.record.id for h in results.explored] == [x]
    assert results.decisions == ()


def test_chain_follows_links_both_ways(conn):
    a = accepted_decision(conn, "a datastore")
    b = accepted_decision(conn, "b jobs", links=(decisions.Link("depends-on", a),))
    c = accepted_decision(conn, "c retries", links=(decisions.Link("depends-on", b),))
    tree = search.chain(conn, b)
    assert [n["id"] for n in tree["depends_on"]] == [a]
    assert [n["id"] for n in tree["affects"]] == [c]
    assert search.chain(conn, a)["affects"][0]["children"][0]["id"] == c
