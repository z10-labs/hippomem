"""The scenarios. Each sets up a situation, runs one or more real agents, and
checks the outcome in the code and in the memory.

Checks are deliberately behavioural: did the agent reach for what the team
ruled out, did it query, did the memory end up with what it should.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from scenarios.harness import Team, sh


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Outcome:
    scenario: str
    title: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(passed), detail))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


def branch(team: Team, repo: Path, name: str) -> None:
    sh(repo, "git", "checkout", "-q", "main")
    sh(repo, "git", "checkout", "-q", "-b", name, env={"HIPPOMEM_GIT_SYNC": "1"})


def baseline_copy(team: Team, repo: Path, name: str) -> Path:
    """A copy of the repo with no Hippomem at all, for the without-memory run."""
    target = team.root / "baseline" / f"{repo.name}-{name}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(repo, target, ignore=shutil.ignore_patterns("hooks"))
    for hook in (target / ".git" / "hooks").glob("post-*"):
        hook.unlink()
    sh(target, "git", "checkout", "-q", "main")
    sh(target, "git", "checkout", "-q", "-b", f"baseline-{name}")
    return target


def grep(repo: Path, needle: str, *globs: str) -> list[str]:
    hits = []
    for pattern in globs or ("**/*",):
        for path in repo.glob(pattern):
            if path.is_file() and ".git" not in path.parts and needle.lower() in path.read_text(errors="ignore").lower():
                hits.append(str(path.relative_to(repo)))
    return sorted(set(hits))


def tool(team: Team, token: str, name: str, **arguments: object) -> str:
    response = httpx.post(
        f"{team.url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers={"Authorization": f"Bearer {token}", "X-Hippomem-Agent": "claude-code", "Accept": "application/json, text/event-stream"},
        timeout=300,
    )
    return str(response.json()["result"]["content"][0]["text"])


def used_memory(team: Team, since: int) -> dict[str, int]:
    counts = {}
    for action in ("session_start", "query", "lookup", "draft", "get", "ask_author", "decide", "turn_stop"):
        counts[action] = len(team.usage(action, since))
    return counts


# --- 1. a settled argument -------------------------------------------------------


RATE_LIMIT_PROMPT = (
    "Add rate limiting to request_refund in src/ledger_api/refunds.py: at most 5 refund requests per customer per hour. "
    "Redis is the usual tool for this, so feel free to use it. Keep it small, and commit when done."
)


def s1_settled_argument(team: Team) -> Outcome:
    out = Outcome("S1", "Settled argument: the agent is nudged toward Redis, which the team rejected")
    repo = team.repo("ledger-api")
    branch(team, repo, "s1/rate-limit")
    since = team.last_usage_id()
    run = team.run_agent("S1", "with Hippomem", repo, RATE_LIMIT_PROMPT)
    used = used_memory(team, since)
    redis_with = grep(repo, "redis", "pyproject.toml", "src/**/*.py")
    out.check("with Hippomem: no Redis in code or dependencies", not redis_with, f"files mentioning redis: {redis_with}")
    out.check("with Hippomem: agent read the memory (query or lookup)", used["query"] + used["lookup"] > 0, json.dumps(used))
    out.check(
        "with Hippomem: answer refers to the decision", any(k in run.result.lower() for k in ("advisory", "postgres", "d-0001")), run.result[-400:]
    )

    base = baseline_copy(team, repo, "s1")
    team.run_agent("S1", "without Hippomem", base, RATE_LIMIT_PROMPT, with_hippomem=False)
    redis_without = grep(base, "redis", "pyproject.toml", "src/**/*.py")
    out.notes.append(f"Without Hippomem the agent {'used Redis' if redis_without else 'did not use Redis'} ({redis_without}).")
    sh(repo, "git", "checkout", "-q", "main")
    return out


# --- 2. a known dead end -------------------------------------------------------------


EVENTS_PROMPT = (
    "ledger-worker needs to react when a refund is requested. Add publishing of a 'refund_requested' event from request_refund "
    "in src/ledger_api/refunds.py. Kafka is what most teams use for this; set up whatever you think is right, add the dependency, and commit."
)


def s2_dead_end(team: Team) -> Outcome:
    out = Outcome("S2", "Known dead end: the agent is nudged toward Kafka, which the team already spiked and dropped")
    repo = team.repo("ledger-api")
    branch(team, repo, "s2/refund-events")
    since = team.last_usage_id()
    run = team.run_agent("S2", "with Hippomem", repo, EVENTS_PROMPT)
    kafka_with = grep(repo, "kafka", "pyproject.toml", "src/**/*.py")
    out.check("with Hippomem: no Kafka in code or dependencies", not kafka_with, f"files mentioning kafka: {kafka_with}")
    out.check(
        "with Hippomem: answer mentions the earlier spike",
        any(k in run.result.lower() for k in ("spike", "explored", "tried", "x-0001", "revisit")),
        run.result[-400:],
    )
    out.notes.append(f"memory use: {json.dumps(used_memory(team, since))}")

    base = baseline_copy(team, repo, "s2")
    team.run_agent("S2", "without Hippomem", base, EVENTS_PROMPT, with_hippomem=False)
    kafka_without = grep(base, "kafka", "pyproject.toml", "src/**/*.py")
    out.notes.append(f"Without Hippomem the agent {'added Kafka' if kafka_without else 'did not add Kafka'} ({kafka_without}).")
    sh(repo, "git", "checkout", "-q", "main")
    return out


# --- 3. a team-wide rule across repos -----------------------------------------------------


REFUND_TOTAL_PROMPT = (
    "On the account page (src/account.ts), show the customer's total refunded amount. The refunds live in the ledger database "
    "(see the refunds table in ../ledger-api/db/schema.sql); connecting to it directly with the pg package is the quickest way. Commit when done."
)


def s3_team_rule(team: Team) -> Outcome:
    out = Outcome("S3", "Team-wide rule in another repo: the agent is nudged to read the ledger database from the storefront")
    repo = team.repo("storefront")
    branch(team, repo, "s3/refund-total")
    run = team.run_agent("S3", "with Hippomem", repo, REFUND_TOTAL_PROMPT)
    pg_with = grep(repo, "'pg'", "package.json", "src/**/*.ts") + grep(repo, '"pg"', "package.json", "src/**/*.ts")
    out.check("with Hippomem: no direct database client added", not pg_with, f"pg found in: {pg_with}")
    out.check(
        "with Hippomem: goes through the ledger API", bool(grep(repo, "ledger(", "src/account.ts") or grep(repo, "ledger<", "src/account.ts")), ""
    )
    out.check("with Hippomem: answer refers to the rule", any(k in run.result.lower() for k in ("database", "api", "d-0002")), run.result[-400:])

    base = baseline_copy(team, repo, "s3")
    team.run_agent("S3", "without Hippomem", base, REFUND_TOTAL_PROMPT, with_hippomem=False)
    pg_without = grep(base, "'pg'", "package.json", "src/**/*.ts") + grep(base, '"pg"', "package.json", "src/**/*.ts")
    out.notes.append(f"Without Hippomem the agent {'connected to the database' if pg_without else 'did not add a database client'} ({pg_without}).")
    sh(repo, "git", "checkout", "-q", "main")
    return out
