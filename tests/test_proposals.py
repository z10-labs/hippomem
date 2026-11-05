from __future__ import annotations

import pytest

from hippomem import decisions, explored, proposals, systems
from hippomem.policy import Actor, Policy, required_level
from tests.helpers import ALEX, KIM, POLICY, SAM, WATCHMAN, accepted_decision


def new_payload(title="Rate limiting uses Postgres advisory locks", **extra):
    return {"title": title, "statement": title, "why": "no new infrastructure to run", "systems": ["billing"], **extra}


def propose(conn, payload=None, *, kind="new", weight="standard", target=None, by="@sam", via="claude-code"):
    return proposals.create(
        conn, POLICY, kind=kind, payload=payload or new_payload(), source="agent", proposed_by=by, proposed_via=via, weight=weight, target=target
    )


def test_accepting_a_new_proposal_creates_a_decision_with_provenance(conn):
    p = propose(
        conn, new_payload(alternatives=[{"option": "Redis", "reason": "another service to run"}], evidence=[{"kind": "commit", "ref": "abc123"}])
    )
    decision_id = proposals.accept(conn, POLICY, p.id, ALEX)
    d = decisions.get(conn, decision_id)
    assert (d.made_by, d.made_via, d.accepted_by, d.accepted_by_kind) == ("@sam", "claude-code", "@alex", "engineer")
    assert d.alternatives == (decisions.Alternative("Redis", "another service to run"),)
    assert [e["ref"] for e in decisions.evidence_for(conn, decision_id)] == ["abc123"]
    assert proposals.get(conn, p.id).status == "accepted"


def test_proposal_validation(conn):
    with pytest.raises(ValueError, match="why"):
        propose(conn, {"title": "x", "why": " "})
    with pytest.raises(ValueError, match="unknown system"):
        propose(conn, new_payload(systems=["nope"]))
    with pytest.raises(ValueError, match="target"):
        propose(conn, {"evidence": []}, kind="evidence", target="D-9999")


def test_nobody_approves_their_own_proposal(conn):
    p = propose(conn, by="@alex")
    with pytest.raises(proposals.Refused, match="own proposal"):
        proposals.accept(conn, POLICY, p.id, ALEX)
    assert proposals.accept(conn, POLICY, p.id, SAM)


def test_solo_policy_allows_self_approval_of_standard_only(conn):
    solo = Policy(self_approval="standard")
    standard = propose(conn, by="@alex")
    assert proposals.accept(conn, solo, standard.id, ALEX)
    heavy = propose(conn, new_payload("Split billing into its own service"), by="@alex", weight="heavy")
    with pytest.raises(proposals.Refused):
        proposals.accept(conn, solo, heavy.id, ALEX)


def test_reviewer_agent_accepts_standard_but_not_heavy_or_its_own(conn):
    standard = propose(conn)
    assert proposals.accept(conn, POLICY, standard.id, WATCHMAN)
    heavy = propose(conn, weight="heavy")
    with pytest.raises(proposals.Refused, match="needs an engineer"):
        proposals.accept(conn, POLICY, heavy.id, WATCHMAN)
    own = propose(conn, via="watchman")
    with pytest.raises(proposals.Refused, match="it wrote"):
        proposals.accept(conn, POLICY, own.id, WATCHMAN)


def test_scope_wide_decision_needs_a_system_owner(conn):
    p = propose(conn, new_payload("Billing owns all money writes", scope="system"), by="@pat")
    with pytest.raises(proposals.Refused, match="owner of billing"):
        proposals.accept(conn, POLICY, p.id, SAM)
    assert proposals.accept(conn, POLICY, p.id, ALEX)


def test_scope_wide_decision_in_unowned_system_falls_back_to_any_engineer(conn):
    p = propose(conn, new_payload("Web pages render on the server", systems=["web"], scope="system"), by="@pat")
    assert proposals.accept(conn, POLICY, p.id, SAM)


def test_supersede_links_and_demotes_the_old_decision(conn):
    old = accepted_decision(conn, "Use Redis for rate limiting")
    p = propose(conn, new_payload(), kind="supersede", target=old)
    assert required_level(p.kind, p.weight, p.payload) == "heavy"
    new = proposals.accept(conn, POLICY, p.id, ALEX)
    assert decisions.get(conn, old).status == "superseded"
    assert decisions.Link("superseded-by", new) in decisions.get(conn, old).links


def test_conflicting_link_makes_a_proposal_heavy(conn):
    other = accepted_decision(conn, "Rate limiting lives in the API gateway")
    p = propose(conn, new_payload(links=[{"type": "conflicts-with", "to": other}]))
    with pytest.raises(proposals.Refused):
        proposals.accept(conn, POLICY, p.id, WATCHMAN)


def test_evidence_is_accepted_automatically_by_rule(conn):
    d = accepted_decision(conn, "Use advisory locks")
    p = propose(
        conn,
        {
            "evidence": [{"kind": "commit", "ref": "def456"}],
            "anchors": [{"repo": "product-api", "path_glob": "src/billing/locks.py"}],
            "implemented": True,
        },
        kind="evidence",
        target=d,
    )
    assert p.status == "accepted"
    assert p.decided_by_kind == "rule"
    decision = decisions.get(conn, d)
    assert decision.implemented
    assert decisions.Anchor("product-api", "src/billing/locks.py") in decision.anchors


def test_evidence_waits_for_review_when_policy_says_so(conn):
    d = accepted_decision(conn, "Use advisory locks")
    p = proposals.create(
        conn, Policy(evidence_only="review"), kind="evidence", payload={"evidence": []}, source="agent", proposed_by="@sam", target=d
    )
    assert p.status == "open"


def test_promoting_an_explored_record(conn):
    x = explored.create(conn, title="Kafka", tried="spike", outcome="dropped", why="ops cost", revisit_if="volume > 100k/day")
    p = propose(conn, new_payload("No Kafka until volume passes 100k jobs a day"), kind="promote", target=x)
    d = proposals.accept(conn, POLICY, p.id, ALEX)
    assert explored.get(conn, x).promoted_to == d


def test_mapping_proposal_confirms_a_mapping(conn):
    p = proposals.create(
        conn,
        POLICY,
        kind="mapping",
        payload={"repo": "product-api", "path_glob": "src/web/**", "system_id": "web"},
        source="agent",
        proposed_by="@sam",
    )
    assert p.title == "Map product-api:src/web/** to web"
    proposals.accept(conn, POLICY, p.id, KIM)
    assert systems.systems_for(conn, "product-api", "src/web/a.ts") == ["web"]


def test_rejected_and_decided_proposals_cannot_be_decided_again(conn):
    p = propose(conn)
    proposals.reject(conn, p.id, ALEX, "not a decision")
    with pytest.raises(proposals.Refused, match="already rejected"):
        proposals.accept(conn, POLICY, p.id, ALEX)


def test_unknown_actor_kind_is_refused(conn):
    p = propose(conn)
    with pytest.raises(proposals.Refused):
        proposals.accept(conn, POLICY, p.id, Actor("x", "robot"))
