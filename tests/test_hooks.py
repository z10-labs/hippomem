"""Hooks against real temporary git repos, with the server reached in-process."""

from __future__ import annotations

import io
import json
import subprocess

import pytest
from starlette.testclient import TestClient

from hippomem import actors, config, drafts, hooks, proposals, server, service


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "product-api"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "sam@example.com")
    git(path, "config", "user.name", "Sam")
    (path / "src" / "billing").mkdir(parents=True)
    (path / "src" / "billing" / "time.py").write_text("UTC = True\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture
def home(tmp_path, conn, monkeypatch):
    token = actors.add(conn, "@pat", "engineer")
    monkeypatch.setenv("HIPPOMEM_TOKEN", token)
    mem = service.Memory(conn=conn, config=config.Config())
    client = TestClient(server.create(mem, allowed_hosts=("testserver",)).streamable_http_app())

    def post(home_dir, path, body, agent):
        response = client.post(path, json=body, headers={"Authorization": f"Bearer {token}", "X-Hippomem-Agent": agent})
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(hooks, "post", post)
    return tmp_path / "hm-home"


def run_hook(name, home, payload):
    out = io.StringIO()
    assert hooks.run_claude_hook(name, home, io.StringIO(json.dumps(payload)), out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_session_start_injects_the_digest(repo, home):
    output = run_hook("session-start", home, {"session_id": "s1", "cwd": str(repo)})
    context = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert 'repo="product-api" branch="main" session="s1"' in context


def test_pre_edit_shows_decisions_once_per_file(repo, home, conn):
    p = proposals.create(
        conn,
        config.Config().policy,
        kind="new",
        payload={"title": "Timestamps are UTC", "why": "zones", "anchors": [{"repo": "product-api", "path_glob": "src/billing/**"}]},
        source="agent",
        proposed_by="@sam",
    )
    from tests.helpers import ALEX

    proposals.accept(conn, config.Config().policy, p.id, ALEX)
    run_hook("session-start", home, {"session_id": "s1", "cwd": str(repo)})
    payload = {"session_id": "s1", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / "src/billing/time.py")}}
    first = run_hook("pre-edit", home, payload)
    assert "Timestamps are UTC" in first["hookSpecificOutput"]["additionalContext"]
    assert run_hook("pre-edit", home, payload) is None


def test_stop_blocks_once_when_a_dependency_changed_without_a_draft(repo, home):
    run_hook("session-start", home, {"session_id": "s1", "cwd": str(repo)})
    run_hook("prompt", home, {"session_id": "s1", "cwd": str(repo)})
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    output = run_hook("stop", home, {"session_id": "s1", "cwd": str(repo)})
    assert output["decision"] == "block" and "pyproject.toml" in output["reason"]
    assert run_hook("stop", home, {"session_id": "s1", "cwd": str(repo), "stop_hook_active": True}) is None


def test_hook_failure_is_logged_not_raised(repo, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("server down")

    monkeypatch.setattr(hooks, "post", broken)
    home = tmp_path / "hm"
    assert run_hook("session-start", home, {"session_id": "s1", "cwd": str(repo)}) is None
    assert "server down" in (home / "hooks.log").read_text()


def test_git_events_commit_and_merge_reconcile_a_branch(repo, home, conn, judge_stub):
    git(repo, "checkout", "-q", "-b", "feat/utc")
    assert hooks.run_git_event("checkout", home, ["a", "b", "1"], cwd=str(repo)) == 0
    branch = drafts.find_active(conn, "product-api", "feat/utc")
    draft_id = drafts.add(conn, branch.id, title="Store timestamps in UTC", why="refund windows span zones", engineer="@pat", paths=("src/billing",))
    (repo / "src/billing/time.py").write_text("UTC = True\nZONE = 'UTC'\n")
    git(repo, "commit", "-qam", "utc")
    hooks.run_git_event("commit", home, [], cwd=str(repo))
    assert drafts.get(conn, draft_id).backed_by == git(repo, "rev-parse", "HEAD")

    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge utc", "feat/utc")
    judge_stub.reply = {"drafts": [{"id": draft_id, "verdict": "built", "weight": "standard", "files": ["src/billing/time.py"]}]}
    hooks.run_git_event("merge", home, [], cwd=str(repo))
    assert drafts.get(conn, draft_id).status == "proposed"
    assert "ZONE = 'UTC'" in judge_stub.prompts[0]


def test_merging_main_into_a_feature_branch_is_not_a_merge_event(repo, home):
    git(repo, "checkout", "-q", "-b", "feat/x")
    git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "y")
    git(repo, "checkout", "-q", "feat/x")
    git(repo, "merge", "-q", "-m", "sync", "main")
    assert hooks.git_event_body("merge", str(repo), []) is None


def test_install_git_hooks_refuses_to_clobber_foreign_hooks(repo, tmp_path):
    written = hooks.install_git_hooks(str(repo), tmp_path / "hm")
    assert len(written) == 3 and hooks.GIT_HOOK_MARKER in (repo / ".git/hooks/post-commit").read_text()
    hooks.install_git_hooks(str(repo), tmp_path / "hm")  # ours: fine to rewrite
    (repo / ".git/hooks/post-merge").write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(RuntimeError, match="isn't Hippomem's"):
        hooks.install_git_hooks(str(repo), tmp_path / "hm")


def test_install_claude_code_keeps_existing_settings(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}, "model": "opus"}
    (project / ".claude/settings.local.json").write_text(json.dumps(existing))
    hooks.install_claude_code(str(project), tmp_path / "hm")
    hooks.install_claude_code(str(project), tmp_path / "hm")  # idempotent
    settings = json.loads((project / ".claude/settings.local.json").read_text())
    assert settings["model"] == "opus"
    assert len(settings["hooks"]["Stop"]) == 2 and settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo mine"
    mcp = json.loads((project / ".mcp.json").read_text())
    assert mcp["mcpServers"]["hippomem"]["headers"]["Authorization"] == "Bearer ${HIPPOMEM_TOKEN}"
