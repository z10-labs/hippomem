"""Scenario harness: a fake team on disk, a real Hippomem server, and real
headless Claude Code agents working in it.

Everything lives under one sandbox folder: the Hippomem home, the sample
repos, agent configs and results. Agents run as `claude -p` with only project
settings, only the Hippomem MCP server, no saved transcripts, and a per-run
budget cap (BD-10).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from hippomem import hooks

AGENT_MODEL = os.environ.get("HIPPOMEM_SCENARIO_MODEL", "sonnet")
RUN_BUDGET_USD = "1.50"
ALLOWED_TOOLS = "Read,Edit,Write,MultiEdit,Glob,Grep,Bash(git:*),Bash(python3:*),Bash(ls:*),Bash(cat:*),mcp__hippomem__*"
HIPPOMEM = [sys.executable, "-m", "hippomem.cli"]


def sh(cwd: Path, *args: str, env: dict | None = None) -> str:
    done = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed in {cwd}: {done.stderr.strip()}")
    return done.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class AgentRun:
    scenario: str
    label: str
    prompt: str
    result: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    session_id: str = ""
    error: str = ""
    seconds: float = 0.0


@dataclass
class Team:
    """The sandbox: Hippomem home, server, repos and tokens."""

    root: Path
    port: int = field(default_factory=free_port)
    tokens: dict[str, str] = field(default_factory=dict)
    server: subprocess.Popen | None = None
    runs: list[AgentRun] = field(default_factory=list)

    @property
    def home(self) -> Path:
        return self.root / "hippomem-home"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def repo(self, name: str) -> Path:
        return self.root / "repos" / name

    def hm(self, *args: str, token: str = "") -> str:
        env = {"HIPPOMEM_TOKEN": token or self.tokens["@alex"]}
        return sh(self.root, *HIPPOMEM, "--home", str(self.home), *args, env=env)

    # --- setup -------------------------------------------------------------------

    def create(self, config_toml: str, engineers: dict[str, str]) -> None:
        """engineers: id -> git email. @alex is the engineer the agents act for."""
        self.root.mkdir(parents=True, exist_ok=True)
        sh(self.root, *HIPPOMEM, "--home", str(self.home), "init")
        (self.home / "hippomem.toml").write_text(config_toml.replace("{port}", str(self.port)))
        for actor_id, email in engineers.items():
            token = sh(self.root, *HIPPOMEM, "--home", str(self.home), "actor", "add", actor_id, "--kind", "engineer")
            self.tokens[actor_id] = token.splitlines()[-1]
            if email:
                self.db().execute("UPDATE actor SET emails = ? WHERE id = ?", (email, actor_id)).connection.commit()
        token = sh(self.root, *HIPPOMEM, "--home", str(self.home), "actor", "add", "watchman", "--kind", "reviewer")
        self.tokens["watchman"] = token.splitlines()[-1]
        (self.home / "token").write_text(self.tokens["@alex"])
        sh(self.root, *HIPPOMEM, "--home", str(self.home), "config", "apply")

    def add_repo(self, name: str, files: dict[str, str], author: tuple[str, str] = ("Alex", "alex@example.com")) -> Path:
        path = self.repo(name)
        path.mkdir(parents=True)
        sh(path, "git", "init", "-q", "-b", "main")
        self.set_author(path, *author)
        for rel, text in files.items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text(text)
        sh(path, "git", "add", ".")
        sh(path, "git", "commit", "-q", "-m", "initial")
        hooks.install_git_hooks(str(path), self.home)
        self.write_agent_config(path)
        return path

    def set_author(self, repo: Path, name: str, email: str) -> None:
        sh(repo, "git", "config", "user.name", name)
        sh(repo, "git", "config", "user.email", email)

    def commit(self, repo: Path, message: str, files: dict[str, str]) -> None:
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        sh(repo, "git", "add", ".")
        sh(repo, "git", "commit", "-q", "-m", message, env={"HIPPOMEM_GIT_SYNC": "1"})

    def write_agent_config(self, repo: Path) -> None:
        """Agent config lives outside the repo so agents can't see or commit it."""
        config_dir = self.root / "agent-config" / repo.name
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "mcp.json").write_text(json.dumps(hooks.mcp_config(self.url, token=self.tokens["@alex"])))
        (config_dir / "settings.json").write_text(json.dumps(hooks.claude_settings(self.home)))

    def start_server(self) -> None:
        log = (self.root / "server.log").open("w")
        self.server = subprocess.Popen([*HIPPOMEM, "--home", str(self.home), "serve"], cwd=self.root, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.url}/api/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.3)
        raise RuntimeError(f"server didn't start; see {self.root / 'server.log'}")

    def stop_server(self) -> None:
        if self.server:
            self.server.terminate()
            self.server.wait(timeout=10)

    # --- agents --------------------------------------------------------------------

    def run_agent(self, scenario: str, label: str, repo: Path, prompt: str, with_hippomem: bool = True) -> AgentRun:
        run = AgentRun(scenario=scenario, label=label, prompt=prompt)
        config_dir = self.root / "agent-config" / repo.name
        command = [
            "claude",
            "-p",
            prompt,
            "--model",
            AGENT_MODEL,
            "--output-format",
            "json",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-budget-usd",
            RUN_BUDGET_USD,
            "--allowedTools",
            ALLOWED_TOOLS if with_hippomem else ALLOWED_TOOLS.replace(",mcp__hippomem__*", ""),
        ]
        if with_hippomem:
            command += ["--mcp-config", str(config_dir / "mcp.json"), "--settings", str(config_dir / "settings.json")]
        env = {**os.environ, "HIPPOMEM_TOKEN": self.tokens["@alex"], "HIPPOMEM_GIT_SYNC": "1", "HIPPOMEM_URL": self.url}
        started = time.time()
        done = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=env, timeout=900)
        run.seconds = round(time.time() - started, 1)
        try:
            out = json.loads(done.stdout)
            run.result = str(out.get("result", ""))
            run.cost_usd = float(out.get("total_cost_usd", 0.0))
            run.turns = int(out.get("num_turns", 0))
            run.session_id = str(out.get("session_id", ""))
            if out.get("is_error"):
                run.error = str(out.get("subtype", "error"))
        except json.JSONDecodeError:
            run.error = f"exit {done.returncode}: {done.stderr.strip()[:500] or done.stdout[:500]}"
        self.runs.append(run)
        return run

    @property
    def spent(self) -> float:
        return round(sum(r.cost_usd for r in self.runs), 2)

    # --- inspecting the memory ----------------------------------------------------------

    def db(self) -> sqlite3.Cursor:
        conn = sqlite3.connect(self.home / "memory.db", timeout=10)
        conn.row_factory = sqlite3.Row
        return conn.cursor()

    def rows(self, sql: str, *params: object) -> list[sqlite3.Row]:
        return self.db().execute(sql, params).fetchall()

    def usage(self, action: str, since_id: int = 0) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM usage WHERE action = ? AND id > ? ORDER BY id", action, since_id)

    def last_usage_id(self) -> int:
        row = self.rows("SELECT COALESCE(MAX(id), 0) AS m FROM usage")
        return int(row[0]["m"])
