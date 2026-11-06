"""A research sandbox: a team of simulated engineers, a real Hippomem server,
git repos, simulated time, and real headless agents.

Differs from scenarios/harness.py in what experiments need: every engineer
has their own token and git identity; the memory's clock and git dates follow
simulated time; every agent run is observed tool call by tool call; and every
model call, the judge's included, is recorded in the cost ledger.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from hippomem import hooks
from research import claude_shim, ledger, observer

AGENT_MODEL = os.environ.get("HIPPOMEM_RESEARCH_AGENT_MODEL", "sonnet")
RUN_BUDGET_USD = 1.50
AGENT_TIMEOUT_SECONDS = 1200
CODE_TOOLS = "Read,Edit,Write,MultiEdit,Glob,Grep,Bash(git:*),Bash(python3:*),Bash(ls:*),Bash(cat:*),Bash(npm test:*),Bash(pytest:*)"
MEMORY_TOOLS = "mcp__hippomem__*"
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


@dataclass(frozen=True)
class Engineer:
    id: str  # '@alex'
    name: str
    email: str
    kind: str = "engineer"


@dataclass
class AgentRun:
    run_id: str
    engineer: str
    repo: str
    prompt: str
    with_memory: bool
    result: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    session_id: str = ""
    error: str = ""
    seconds: float = 0.0
    transcript: str = ""  # path to the stream-json output


@dataclass
class Sandbox:
    root: Path
    book: ledger.Ledger
    phase: str = ""
    experiment: str = ""
    port: int = field(default_factory=free_port)
    engineers: dict[str, Engineer] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    server: subprocess.Popen | None = None
    runs: list[AgentRun] = field(default_factory=list)
    now: str = "2026-01-05T09:00:00Z"
    approved: bool = False  # the owner approved spending past the ask point

    # --- layout ------------------------------------------------------------------

    @property
    def home(self) -> Path:
        return self.root / "hippomem-home"

    @property
    def db_path(self) -> Path:
        return self.home / "memory.db"

    @property
    def clock_file(self) -> Path:
        return self.root / "clock"

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def repo(self, name: str) -> Path:
        return self.root / "repos" / name

    # --- setup ---------------------------------------------------------------------

    def create(self, config_toml: str, engineers: list[Engineer], reviewer: str = "watchman") -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.set_time(self.now)
        self._hm_admin("init", "--admin", engineers[0].id)
        self.tokens[engineers[0].id] = (self.home / "token").read_text().strip()
        (self.home / "hippomem.toml").write_text(config_toml.replace("{port}", str(self.port)))
        for e in engineers:
            self.engineers[e.id] = e
            if e.id not in self.tokens:
                out = self._hm_admin("actor", "add", e.id, "--kind", e.kind, "--email", e.email)
                self.tokens[e.id] = out.splitlines()[-1].strip()
        out = self._hm_admin("actor", "add", reviewer, "--kind", "reviewer")
        self.tokens[reviewer] = out.splitlines()[-1].strip()
        self._hm_admin("config", "apply")
        claude_shim.write_launcher(self.root / "bin")

    def save(self) -> None:
        """Write what's needed to reopen this sandbox in another process."""
        state = {"engineers": [e.__dict__ for e in self.engineers.values()], "tokens": self.tokens, "now": self.now, "port": self.port}
        (self.root / "sandbox.json").write_text(json.dumps(state, indent=2))

    @classmethod
    def load(cls, root: Path, book: ledger.Ledger, phase: str = "", experiment: str = "") -> Sandbox:
        state = json.loads((root / "sandbox.json").read_text())
        box = cls(root=root, book=book, phase=phase, experiment=experiment, port=state["port"])
        box.engineers = {e["id"]: Engineer(**e) for e in state["engineers"]}
        box.tokens = state["tokens"]
        box.set_time(state["now"])
        return box

    def _hm_admin(self, *args: str) -> str:
        return sh(self.root, *HIPPOMEM, "--home", str(self.home), *args, env=self._env(""))

    def hm(self, actor: str, *args: str) -> str:
        """Run the hippomem CLI as `actor`."""
        return sh(self.root, *HIPPOMEM, "--home", str(self.home), *args, env=self._env(actor))

    def _env(self, actor: str) -> dict:
        env = {
            "HIPPOMEM_CLOCK_FILE": str(self.clock_file),
            "HIPPOMEM_GIT_SYNC": "1",
            "HIPPOMEM_URL": self.url,
            ledger.LEDGER_ENV: str(self.book.path),
            ledger.PHASE_ENV: self.phase,
            ledger.EXPERIMENT_ENV: self.experiment,
        }
        if actor:
            env["HIPPOMEM_TOKEN"] = self.tokens[actor]
        return env

    def set_time(self, iso: str) -> None:
        """Move simulated time. The server reads it on every call (BD-41); git uses it for new commits."""
        self.now = iso
        self.clock_file.write_text(iso + "\n")

    # --- git ---------------------------------------------------------------------------

    def git(self, repo: str, *args: str, as_: str = "") -> str:
        e = self.engineers[as_] if as_ else None
        env = {**self._env(as_), "GIT_AUTHOR_DATE": self.now, "GIT_COMMITTER_DATE": self.now}
        if e:
            env.update({"GIT_AUTHOR_NAME": e.name, "GIT_AUTHOR_EMAIL": e.email, "GIT_COMMITTER_NAME": e.name, "GIT_COMMITTER_EMAIL": e.email})
        return sh(self.repo(repo), "git", *args, env=env)

    def add_repo(self, name: str, files: dict[str, str], author: str) -> Path:
        """A new repo with one commit. Hippomem's git hooks are installed
        separately (`install_hooks`), after any history that predates the memory."""
        path = self.repo(name)
        path.mkdir(parents=True)
        sh(path, "git", "init", "-q", "-b", "main")
        self._write(path, files)
        self.git(name, "add", ".", as_=author)
        self.git(name, "commit", "-q", "-m", "initial", as_=author)
        return path

    def install_hooks(self, name: str) -> None:
        hooks.install_git_hooks(str(self.repo(name)), self.home, force=True)

    def has_changes(self, repo: str) -> bool:
        return bool(self.git(repo, "status", "--porcelain"))

    def clone(self, root: Path, phase: str = "", experiment: str = "") -> Sandbox:
        """A full copy of this sandbox (memory, repos, clock) at `root`, with
        its own port, for runs that must not see each other's effects."""
        if self.server:
            raise RuntimeError("stop the server before cloning")
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(self.root, root, ignore=shutil.ignore_patterns("agent-config", "transcripts", "events.jsonl", "runs.jsonl", "server.log"))
        copy = Sandbox(root=root, book=self.book, phase=phase or self.phase, experiment=experiment or self.experiment, approved=self.approved)
        copy.engineers, copy.tokens = dict(self.engineers), dict(self.tokens)
        config = copy.home / "hippomem.toml"
        config.write_text(re.sub(r"(?m)^port = \d+$", f"port = {copy.port}", config.read_text(), count=1))
        copy.set_time(self.now)
        copy.save()
        claude_shim.write_launcher(root / "bin")
        for repo in (root / "repos").iterdir():
            copy.install_hooks(repo.name)
        return copy

    def commit(self, repo: str, author: str, message: str, files: dict[str, str]) -> None:
        self._write(self.repo(repo), files)
        self.git(repo, "add", ".", as_=author)
        self.git(repo, "commit", "-q", "-m", message, as_=author)

    def checkout(self, repo: str, branch: str, author: str, new: bool = True) -> None:
        self.git(repo, "checkout", "-q", "main", as_=author)
        self.git(repo, "checkout", "-q", *(["-b"] if new else []), branch, as_=author)

    def merge(self, repo: str, branch: str, author: str, message: str = "") -> None:
        """Merge into main with a merge commit, as a reviewed PR would; the post-merge hook reconciles."""
        self.git(repo, "checkout", "-q", "main", as_=author)
        self.git(repo, "merge", "-q", "--no-ff", "-m", message or f"Merge {branch}", branch, as_=author)

    def head(self, repo: str) -> str:
        return self.git(repo, "rev-parse", "HEAD")

    @staticmethod
    def _write(path: Path, files: dict[str, str]) -> None:
        for rel, text in files.items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    # --- server ------------------------------------------------------------------------

    def start_server(self) -> None:
        env = {
            **os.environ,
            **self._env(""),
            "HIPPOMEM_JUDGE_CLAUDE": str(self.root / "bin" / "claude"),
            claude_shim.REAL_CLAUDE_ENV: shutil.which("claude") or "claude",
            ledger.KIND_ENV: "judge",
        }
        log = (self.root / "server.log").open("a")
        self.server = subprocess.Popen([*HIPPOMEM, "--home", str(self.home), "serve"], cwd=self.root, stdout=log, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + 90
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
            self.server.wait(timeout=15)
            self.server = None

    # --- agents --------------------------------------------------------------------------

    def agent_config(self, engineer: str, repo: str) -> Path:
        """MCP and hook settings outside the repo, so agents can't see or commit them."""
        config_dir = self.root / "agent-config" / engineer.lstrip("@") / repo
        if not config_dir.exists():
            config_dir.mkdir(parents=True)
            (config_dir / "mcp.json").write_text(json.dumps(hooks.mcp_config(self.url, token=self.tokens[engineer])))
            memory_hooks = hooks.claude_settings(self.home)["hooks"]
            observe = observer.settings(sys.executable)["hooks"]
            merged = {event: memory_hooks.get(event, []) + observe.get(event, []) for event in {*memory_hooks, *observe}}
            (config_dir / "settings.json").write_text(json.dumps({"hooks": merged}))
            (config_dir / "settings-observe-only.json").write_text(json.dumps(observer.settings(sys.executable)))
        return config_dir

    def run_agent(self, engineer: str, cwd: Path, prompt: str, *, with_memory: bool = True, with_code: bool = True, label: str = "") -> AgentRun:
        """Run one headless Claude Code session as `engineer` in `cwd` and record its cost."""
        self.book.check(RUN_BUDGET_USD, allow_past_ask=self.approved)
        run_id = f"{self.experiment or 'x'}-{len(self.runs) + 1:03d}" + (f"-{label}" if label else "")
        # Claude Code tells agents the real date; in a simulation that's wrong, and agents write it into records.
        day = datetime.strptime(self.now[:10], "%Y-%m-%d").strftime("%A %-d %B %Y")
        prompt = f"{prompt}\n\n(Today is {day}.)"
        run = AgentRun(run_id=run_id, engineer=engineer, repo=cwd.name, prompt=prompt, with_memory=with_memory)
        config_dir = self.agent_config(engineer, cwd.name if (self.root / "repos" / cwd.name).exists() else "_workspace")
        tools = ",".join(t for t, on in ((CODE_TOOLS, with_code), (MEMORY_TOOLS, with_memory)) if on) or "Read"
        command = [
            "claude", "-p", prompt,
            "--model", AGENT_MODEL,
            "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-budget-usd", f"{RUN_BUDGET_USD:.2f}",
            "--allowedTools", tools,
        ]  # fmt: skip
        if with_memory:
            command += ["--mcp-config", str(config_dir / "mcp.json"), "--settings", str(config_dir / "settings.json")]
        else:
            command += ["--settings", str(config_dir / "settings-observe-only.json")]
        env = {
            **os.environ,
            **self._env(engineer),
            observer.EVENTS_ENV: str(self.events_file),
            observer.DB_ENV: str(self.db_path),
            observer.RUN_ENV: run_id,
            "GIT_AUTHOR_DATE": self.now,
            "GIT_COMMITTER_DATE": self.now,
        }
        transcripts = self.root / "transcripts"
        transcripts.mkdir(exist_ok=True)
        run.transcript = str(transcripts / f"{run_id}.jsonl")
        started = time.time()
        try:
            done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env, timeout=AGENT_TIMEOUT_SECONDS)
            Path(run.transcript).write_text(done.stdout)
            self._read_result(run, done.stdout, done.returncode, done.stderr)
        except subprocess.TimeoutExpired as err:
            run.error = f"timed out after {AGENT_TIMEOUT_SECONDS}s"
            Path(run.transcript).write_text(err.stdout.decode() if isinstance(err.stdout, bytes) else (err.stdout or ""))
            run.cost_usd = claude_shim.cost_from_output(Path(run.transcript).read_text())
        run.seconds = round(time.time() - started, 1)
        self.book.record(phase=self.phase, experiment=self.experiment, kind="agent", label=run_id, cost_usd=run.cost_usd)
        self.runs.append(run)
        with (self.root / "runs.jsonl").open("a") as f:
            f.write(json.dumps(run.__dict__) + "\n")
        return run

    @staticmethod
    def _read_result(run: AgentRun, stdout: str, returncode: int, stderr: str) -> None:
        final = None
        for line in stdout.splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("type") == "result":
                final = data
        if final is None:
            run.error = f"exit {returncode}: {stderr.strip()[:500] or stdout[-500:]}"
            return
        run.result = str(final.get("result", ""))
        run.cost_usd = float(final.get("total_cost_usd", 0.0))
        run.turns = int(final.get("num_turns", 0))
        run.session_id = str(final.get("session_id", ""))
        if final.get("is_error"):
            run.error = str(final.get("subtype", "error"))

    # --- reading the memory ---------------------------------------------------------------

    def rows(self, sql: str, *params: object) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def events(self, run_id: str = "") -> list[dict]:
        if not self.events_file.exists():
            return []
        lines = [json.loads(line) for line in self.events_file.read_text().splitlines() if line.strip()]
        return [e for e in lines if not run_id or e.get("run") == run_id]
