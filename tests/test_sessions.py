from __future__ import annotations

from hippomem import actors, decisions, drafts, proposals, sessions
from hippomem.policy import Actor
from tests.helpers import ALEX, POLICY, accepted_decision

ALEX_AGENT = Actor("@alex", "engineer", "claude-code")
SAM_AGENT = Actor("@sam", "engineer", "claude-code")


def start(conn, actor=ALEX_AGENT, session_id="s1", repo="product-api", branch="feat/refunds"):
    return sessions.start(conn, actor, session_id, repo, branch)


def test_digest_lists_decisions_in_force_trail_and_how_to_call_tools(conn):
    accepted_decision(conn, "Billing owns all money writes", "one place to audit", scope="system")
    accepted_decision(conn, "Web pages render on the server", systems_=("web",))
    s = start(conn)
    b = drafts.find_active(conn, "product-api", "feat/refunds")
    first = drafts.add(conn, b.id, title="Store the deadline", why="simple", engineer="@alex")
    drafts.add(conn, b.id, title="Compute the deadline", why="always correct", engineer="@alex", replaces=first, replaced_because="goes stale")
    text = sessions.digest(conn, POLICY, ALEX_AGENT, s, 2)
    assert 'repo="product-api" branch="feat/refunds" session="s1"' in text
    assert "Billing owns all money writes" in text
    assert "Web pages render" not in text  # another system
    assert "#1 dropped: Store the deadline. Because: goes stale" in text
    assert "#2 decided: Compute the deadline. Why: always correct" in text


def test_digest_shows_other_branches_touching_the_same_systems(conn):
    other = drafts.ensure_branch(conn, "product-worker", "feat/refund-job")
    drafts.add(conn, other, title="Move refund logic into a background job", why="retries", engineer="@sam")
    elsewhere = drafts.ensure_branch(conn, "product-web", "feat/ui")
    drafts.add(conn, elsewhere, title="New button colour", why="brand", engineer="@sam")
    text = sessions.digest(conn, POLICY, ALEX_AGENT, start(conn), 2)
    assert "product-worker:feat/refund-job (@sam) decided: Move refund logic" in text
    assert "New button colour" not in text


def test_unmapped_repo_digest_suggests_a_mapping(conn):
    text = sessions.digest(conn, POLICY, ALEX_AGENT, start(conn, repo="brand-new"), 2)
    assert "isn't mapped to a system yet" in text


def proposal(conn, title, by="@sam", systems_=("billing",), **payload):
    return proposals.create(
        conn,
        POLICY,
        kind="new",
        payload={"title": title, "why": "w", "systems": list(systems_), **payload},
        source="agent",
        proposed_by=by,
        proposed_via="claude-code",
    )


def test_proposals_are_surfaced_up_to_the_cap_relevant_first_never_own_never_twice(conn):
    proposal(conn, "Web thing", systems_=("web",))
    proposal(conn, "Billing thing one")
    proposal(conn, "Billing thing two")
    proposal(conn, "My own thing", by="@alex")
    s = start(conn)
    text = sessions.digest(conn, POLICY, ALEX_AGENT, s, 2)
    assert "Billing thing one" in text and "Billing thing two" in text
    assert "Web thing" not in text and "My own thing" not in text
    again = sessions.digest(conn, POLICY, ALEX_AGENT, s, 2)
    assert "Waiting for your engineer" not in again  # cap reached for this session
    other_session = sessions.digest(conn, POLICY, ALEX_AGENT, start(conn, session_id="s2"), 2)
    assert "Billing thing one" in other_session


def test_lookup_returns_anchored_and_system_wide_decisions(conn):
    anchored = proposal(conn, "Refund deadline is computed", anchors=[{"repo": "product-api", "path_glob": "src/billing/refunds/**"}])
    proposals.accept(conn, POLICY, anchored.id, ALEX)
    accepted_decision(conn, "Billing owns all money writes", "one place to audit", scope="system")
    start(conn)
    text = sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "src/billing/refunds/window.py")
    assert "Decisions covering src/billing/refunds/window.py" in text and "Refund deadline is computed" in text
    assert "Rules for billing that apply here" in text and "Billing owns all money writes" in text
    assert sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "README.md") == ""


def test_lookup_raises_open_proposals_about_the_file_immediately(conn):
    proposal(conn, "Locks time out after 5s", anchors=[{"repo": "product-api", "path_glob": "src/billing/locks.py"}])
    text = sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "src/billing/locks.py")
    assert "Raise these with your engineer now" in text and "Locks time out" in text


HISTORY = {
    "commits": 7,
    "top_author_email": "sam@example.com",
    "top_author_name": "Sam",
    "last": {"commit": "abc", "email": "sam@example.com", "date": "2026-09-01", "subject": "store timestamps in UTC"},
}


def test_lookup_suggests_asking_the_author_once_per_session_and_file(conn):
    start(conn)
    text = sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "src/billing/time.py", HISTORY)
    assert "hippomem_ask_author" in text and "Sam <sam@example.com>" in text
    assert sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "src/billing/time.py", HISTORY) == ""
    assert sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "src/billing/new.py", {"commits": 1}) == ""


def test_no_ask_author_hint_for_your_own_code(conn):
    conn.execute("UPDATE actor SET emails = 'sam@example.com' WHERE id = '@sam'")
    assert sessions.lookup(conn, POLICY, SAM_AGENT, "s1", "product-api", "src/billing/time.py", HISTORY) == ""


def test_question_goes_to_the_author_and_their_answer_becomes_a_proposal(conn):
    conn.execute("UPDATE actor SET emails = 'sam@example.com' WHERE id = '@sam'")
    start(conn)
    sessions.lookup(conn, POLICY, ALEX_AGENT, "s1", "product-api", "src/billing/time.py", HISTORY)
    asked = sessions.ask_author(conn, ALEX_AGENT, "product-api", "src/billing/time.py", "Why UTC everywhere?", {})
    assert asked.startswith("Asked @sam (Q-0001)")
    sam_digest = sessions.digest(conn, POLICY, SAM_AGENT, start(conn, SAM_AGENT, "s9"), 2)
    assert "Question Q-0001 from @alex about product-api:src/billing/time.py: Why UTC everywhere?" in sam_digest
    p = sessions.answer(conn, POLICY, SAM_AGENT, "Q-0001", "Timestamps are stored in UTC", "refund windows span time zones")
    assert (p.source, p.payload["anchors"], p.payload["systems"]) == (
        "author_answer",
        [{"repo": "product-api", "path_glob": "src/billing/time.py"}],
        ["billing"],
    )
    decision_id = proposals.accept(conn, POLICY, p.id, ALEX)
    assert decisions.get(conn, decision_id).implemented


def test_question_to_unregistered_author_keeps_their_git_identity(conn):
    text = sessions.ask_author(conn, ALEX_AGENT, "product-api", "x.py", "why?", HISTORY)
    assert "Sam <sam@example.com>" in text


def test_turn_stop_nudges_once_when_decision_shaped_files_changed_without_a_draft(conn):
    s = start(conn)
    sessions.turn_start(conn, s.id)
    assert sessions.turn_stop(conn, s.id, ["src/billing/refunds.py"]) == ""
    reason = sessions.turn_stop(conn, s.id, ["package.json", "src/a.ts"])
    assert "package.json" in reason and "hippomem_draft" in reason
    assert sessions.turn_stop(conn, s.id, ["package.json"]) == ""  # once per turn


def test_turn_stop_is_quiet_when_something_was_drafted(conn):
    s = start(conn)
    sessions.turn_start(conn, s.id)
    drafts.add(conn, s.branch_id, title="Use zod for validation", why="already a dependency", engineer="@alex")
    assert sessions.turn_stop(conn, s.id, ["package.json"]) == ""


def test_decision_shaped_patterns():
    assert sessions.decision_shaped(["db/migrations/0003_add.sql", "src/app.py", "pyproject.toml", "infra/main.tf"]) == [
        "db/migrations/0003_add.sql",
        "pyproject.toml",
        "infra/main.tf",
    ]


def test_latest_session_fills_in_repo_and_branch(conn):
    start(conn)
    assert sessions.latest_for(conn, ALEX_AGENT).repo == "product-api"
    assert sessions.latest_for(conn, Actor("@kim", "admin")) is None


def test_actor_email_lookup(conn):
    actors.add(conn, "@pat", "engineer", emails=("Pat@Example.com",))
    assert actors.by_email(conn, "pat@example.com") == "@pat"
    assert actors.by_email(conn, "nobody@example.com") is None


def test_turn_stop_nudges_after_a_silent_commit(conn):
    s = start(conn)
    sessions.turn_start(conn, s.id)
    conn.execute("INSERT INTO event (kind, repo, branch, payload, at) VALUES ('commit', 'product-api', 'feat/refunds', '{}', '2099-01-01T00:00:00Z')")
    reason = sessions.turn_stop(conn, s.id, ["src/billing/refunds.py"])
    assert "committed code" in reason and "replaces=" in reason
