"""Client side: Claude Code hooks, git hooks, and installing both.

Hooks run on the engineer's machine in the repo, read git state, and call the
server. A hook must never break the agent or git: on any error it writes the
traceback to `hooks.log` in the Hippomem home and exits quietly.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

import httpx

from hippomem import config as cfg
from hippomem import gitinfo

EDIT_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"
GIT_HOOK_MARKER = "# installed by hippomem"


# --- talking to the server ---------------------------------------------------------


def _token(home: Path) -> str:
    token = os.environ.get("HIPPOMEM_TOKEN", "")
    if not token and (home / "token").exists():
        token = (home / "token").read_text().strip()
    if not token:
        raise RuntimeError("no Hippomem token: set HIPPOMEM_TOKEN or run `hippomem login`")
    return token


def post(home: Path, path: str, body: dict, agent: str) -> dict:
    url = os.environ.get("HIPPOMEM_URL", cfg.load(cfg.config_path(home)).url)
    headers = {"Authorization": f"Bearer {_token(home)}", "X-Hippomem-Agent": agent}
    response = httpx.post(url + path, json=body, headers=headers, timeout=float(os.environ.get("HIPPOMEM_HOOK_TIMEOUT", "300")))
    data = response.json()
    if response.status_code >= 400:
        raise RuntimeError(f"{path}: {data.get('error', response.text)}")
    return dict(data)


def log_failure(home: Path, what: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    with (home / "hooks.log").open("a") as f:
        f.write(f"--- {what}\n{traceback.format_exc()}\n")


# --- Claude Code hooks ------------------------------------------------------------


def _state_file(home: Path, session_id: str) -> Path:
    return home / "sessions" / f"{session_id}.json"


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def session_start(payload: dict, home: Path, agent: str) -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    body = {"session_id": payload["session_id"], "repo": gitinfo.repo_name(cwd), "branch": gitinfo.current_branch(cwd)}
    text = post(home, "/api/session/start", body, agent)["text"]
    return _context("SessionStart", text)


def prompt(payload: dict, home: Path, agent: str) -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    state = {"head": gitinfo.head(cwd), "dirty": gitinfo.changed_files(cwd), "looked_up": []}
    state_file = _state_file(home, payload["session_id"])
    if state_file.exists():
        state["looked_up"] = json.loads(state_file.read_text()).get("looked_up", [])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))
    post(home, "/api/turn/start", {"session_id": payload["session_id"]}, agent)
    return None


def pre_edit(payload: dict, home: Path, agent: str) -> dict | None:
    cwd = payload.get("cwd") or os.getcwd()
    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        return None
    rel = gitinfo.relative_path(cwd, file_path)
    state_file = _state_file(home, payload["session_id"])
    state = json.loads(state_file.read_text()) if state_file.exists() else {"looked_up": []}
    if rel in state.get("looked_up", []):
        return None  # already shown this session
    body = {"session_id": payload["session_id"], "repo": gitinfo.repo_name(cwd), "path": rel, "history": gitinfo.file_history(cwd, rel)}
    text = post(home, "/api/lookup", body, agent)["text"]
    state["looked_up"] = [*state.get("looked_up", []), rel]
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))
    return _context("PreToolUse", text) if text else None


def stop(payload: dict, home: Path, agent: str) -> dict | None:
    if payload.get("stop_hook_active"):
        return None
    cwd = payload.get("cwd") or os.getcwd()
    state_file = _state_file(home, payload["session_id"])
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    before = set(state.get("dirty", []))
    changed = [f for f in gitinfo.changed_files(cwd, state.get("head", "")) if f not in before]
    reason = post(home, "/api/turn/stop", {"session_id": payload["session_id"], "changed": changed}, agent)["reason"]
    return {"decision": "block", "reason": reason} if reason else None


CLAUDE_HOOKS: dict[str, Callable[[dict, Path, str], dict | None]] = {
    "session-start": session_start,
    "prompt": prompt,
    "pre-edit": pre_edit,
    "stop": stop,
}


def run_claude_hook(name: str, home: Path, stdin: Any = None, stdout: Any = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    agent = os.environ.get("HIPPOMEM_AGENT", "claude-code")
    try:
        payload = json.loads(stdin.read() or "{}")
        output = CLAUDE_HOOKS[name](payload, home, agent)
    except Exception:
        log_failure(home, f"claude hook {name}")
        return 0
    if output:
        stdout.write(json.dumps(output))
    return 0


# --- git hooks -------------------------------------------------------------------


def git_event_body(kind: str, cwd: str, args: list[str]) -> dict | None:
    repo = gitinfo.repo_name(cwd)
    branch = gitinfo.current_branch(cwd)
    if kind == "checkout":
        if args and len(args) >= 3 and args[2] != "1":
            return None  # file checkout, not a branch switch
        return {"kind": "checkout", "repo": repo, "branch": branch}
    if kind == "commit":
        return {
            "kind": "commit",
            "repo": repo,
            "branch": branch,
            "commit": gitinfo.head(cwd),
            "files": gitinfo.commit_files(cwd),
            "diff": gitinfo.commit_diff(cwd),
            "default_branch": gitinfo.default_branch(cwd),
        }
    if kind == "merge":
        merged = gitinfo.merged_branch(cwd)
        default = gitinfo.default_branch(cwd)
        into_default = branch == default if default else branch in ("main", "master", "trunk", "develop")
        if not merged or not into_default:
            return None  # only merges into the default branch confirm drafts
        commits, diff = gitinfo.merge_range(cwd)
        return {"kind": "merge", "repo": repo, "branch": merged, "into": branch, "commits": commits, "diff": diff}
    raise ValueError(f"unknown git event {kind!r}")


def run_git_event(kind: str, home: Path, args: list[str], cwd: str | None = None) -> int:
    try:
        body = git_event_body(kind, cwd or os.getcwd(), args)
        if body is not None:
            result = post(home, "/api/event", body, "git")
            print(json.dumps(result))
    except Exception:
        log_failure(home, f"git event {kind}")
    return 0


def _hippomem_command() -> str:
    return f"{shlex.quote(sys.executable)} -m hippomem.cli"


GIT_HOOKS = {"post-commit": "commit", "post-merge": "merge", "post-checkout": "checkout"}


def git_hook_script(event: str, home: Path) -> str:
    command = f'{_hippomem_command()} --home {shlex.quote(str(home))} git-event {event} "$@"'
    log = shlex.quote(str(home / "git-events.log"))
    return (
        "#!/bin/sh\n"
        f"{GIT_HOOK_MARKER}\n"
        "# Sends this git event to Hippomem in the background so git never waits on it.\n"
        'if [ -n "$HIPPOMEM_GIT_SYNC" ]; then\n'
        f"  {command} >> {log} 2>&1\n"
        "else\n"
        f"  ({command} >> {log} 2>&1 &)\n"
        "fi\n"
    )


def install_git_hooks(repo_dir: str, home: Path, force: bool = False) -> list[str]:
    hooks_dir = Path(repo_dir) / gitinfo.git(repo_dir, "rev-parse", "--git-path", "hooks")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for hook, event in GIT_HOOKS.items():
        path = hooks_dir / hook
        if path.exists() and GIT_HOOK_MARKER not in path.read_text() and not force:
            raise RuntimeError(f"{path} already exists and isn't Hippomem's; merge it by hand or pass --force")
        path.write_text(git_hook_script(event, home))
        path.chmod(0o755)
        written.append(str(path))
    return written


# --- Claude Code configuration ---------------------------------------------------------


def claude_settings(home: Path) -> dict:
    """Hook settings for Claude Code. The token comes from HIPPOMEM_TOKEN or the home's token file."""
    base = f"{_hippomem_command()} --home {shlex.quote(str(home))} hook"

    def entry(name: str, matcher: str | None = None) -> list[dict]:
        item: dict = {"hooks": [{"type": "command", "command": f"{base} {name}", "timeout": 60}]}
        if matcher:
            item["matcher"] = matcher
        return [item]

    return {
        "hooks": {
            "SessionStart": entry("session-start"),
            "UserPromptSubmit": entry("prompt"),
            "PreToolUse": entry("pre-edit", EDIT_TOOLS),
            "Stop": entry("stop"),
        }
    }


def mcp_config(url: str, token: str = "${HIPPOMEM_TOKEN}", agent: str = "claude-code") -> dict:
    """MCP server entry. With the default token placeholder Claude Code expands
    $HIPPOMEM_TOKEN from the environment, so the file holds no secret."""
    return {
        "mcpServers": {"hippomem": {"type": "http", "url": f"{url}/mcp", "headers": {"Authorization": f"Bearer {token}", "X-Hippomem-Agent": agent}}}
    }


def _merge_json(path: Path, addition: dict) -> None:
    """Add our entries without dropping anything already in the file. Hook
    lists are appended to (once), other objects are merged key by key."""
    current = json.loads(path.read_text()) if path.exists() else {}
    for key, value in addition.items():
        if key == "hooks" and isinstance(current.get("hooks"), dict):
            for event, entries in value.items():
                existing = current["hooks"].setdefault(event, [])
                for item in entries:
                    if item not in existing:
                        existing.append(item)
        elif isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n")


def install_claude_code(project_dir: str, home: Path) -> list[str]:
    """Write .mcp.json and .claude/settings.local.json in a project. Neither
    holds the token, so .mcp.json is safe to commit; settings.local.json holds
    machine-specific paths and is meant to stay uncommitted."""
    url = cfg.load(cfg.config_path(home)).url
    mcp_path = Path(project_dir) / ".mcp.json"
    settings_path = Path(project_dir) / ".claude" / "settings.local.json"
    _merge_json(mcp_path, mcp_config(url))
    _merge_json(settings_path, claude_settings(home))
    return [str(mcp_path), str(settings_path)]
