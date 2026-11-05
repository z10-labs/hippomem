"""Team configuration: hippomem.toml.

The file holds settings (server, acceptance, review, branches) and can seed
systems, owners and repo mappings. Seeded mappings count as confirmed: an
engineer wrote them.

    [server]
    host = "127.0.0.1"
    port = 8765

    [acceptance]
    evidence_only = "auto"     # auto | review
    self_approval = "never"    # never | standard

    [review]
    max_per_session = 2

    [branches]
    stale_after_days = 30

    [[systems]]
    id = "billing"
    name = "Billing"
    owners = ["@alex"]
    depends_on = []
    repos = ["product-api:src/billing/**", "product-worker:**"]
"""

from __future__ import annotations

import os
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from hippomem import actors, db, systems
from hippomem.policy import Policy

DEFAULT_PORT = 8765


@dataclass(frozen=True)
class SystemSpec:
    id: str
    name: str
    owners: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    repos: tuple[tuple[str, str], ...] = ()  # (repo, path_glob)
    description: str = ""


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    policy: Policy = field(default_factory=Policy)
    max_per_session: int = 2
    stale_after_days: int = 30
    systems: tuple[SystemSpec, ...] = ()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def home() -> Path:
    """Where the memory file and config live: $HIPPOMEM_HOME or ~/.hippomem."""
    return Path(os.environ.get("HIPPOMEM_HOME", Path.home() / ".hippomem"))


def db_path(base: Path | None = None) -> Path:
    return (base or home()) / "memory.db"


def config_path(base: Path | None = None) -> Path:
    return (base or home()) / "hippomem.toml"


def load(path: Path) -> Config:
    if not path.exists():
        return Config()
    return parse(tomllib.loads(path.read_text()))


def parse(raw: dict) -> Config:
    server = raw.get("server", {})
    acceptance = raw.get("acceptance", {})
    policy = Policy(evidence_only=acceptance.get("evidence_only", "auto"), self_approval=acceptance.get("self_approval", "never"))
    if policy.evidence_only not in ("auto", "review"):
        raise ValueError("acceptance.evidence_only must be 'auto' or 'review'")
    if policy.self_approval not in ("never", "standard"):
        raise ValueError("acceptance.self_approval must be 'never' or 'standard'")
    return Config(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", DEFAULT_PORT)),
        policy=policy,
        max_per_session=int(raw.get("review", {}).get("max_per_session", 2)),
        stale_after_days=int(raw.get("branches", {}).get("stale_after_days", 30)),
        systems=tuple(_system(s) for s in raw.get("systems", ())),
    )


def _system(raw: dict) -> SystemSpec:
    repos = []
    for entry in raw.get("repos", ()):
        repo, _, glob = entry.partition(":")
        repos.append((repo, glob or systems.WHOLE_REPO))
    return SystemSpec(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        owners=tuple(raw.get("owners", ())),
        depends_on=tuple(raw.get("depends_on", ())),
        repos=tuple(repos),
        description=raw.get("description", ""),
    )


def apply(conn: sqlite3.Connection, config: Config) -> None:
    """Create or update the systems, owners and mappings the config declares."""
    with db.transaction(conn):
        for spec in config.systems:
            for owner in spec.owners:
                if actors.get(conn, owner) is None:
                    raise ValueError(f"system {spec.id!r} names owner {owner!r}, who is not an actor yet: run `hippomem actor add {owner}`")
            if systems.system_exists(conn, spec.id):
                conn.execute("UPDATE system SET name = ?, description = ? WHERE id = ?", (spec.name, spec.description, spec.id))
                conn.execute("DELETE FROM system_owner WHERE system_id = ?", (spec.id,))
                for owner in spec.owners:
                    conn.execute("INSERT INTO system_owner (system_id, actor_id) VALUES (?, ?)", (spec.id, owner))
            else:
                systems.create_system(conn, spec.id, spec.name, spec.description, spec.owners)
        for spec in config.systems:
            for dep in spec.depends_on:
                systems.add_dependency(conn, spec.id, dep)
            for repo, glob in spec.repos:
                systems.add_mapping(conn, repo, glob, spec.id, created_by="config")


TEMPLATE = """# Hippomem team configuration. See docs/using-hippomem.md.

[server]
host = "127.0.0.1"
port = 8765

[acceptance]
evidence_only = "auto"     # evidence added to an accepted decision: auto | review
self_approval = "never"    # never | standard (a solo engineer may accept their own standard proposals)

[review]
max_per_session = 2        # pending proposals an agent may raise with its engineer per session

[branches]
stale_after_days = 30

# [[systems]]
# id = "billing"
# name = "Billing"
# owners = ["@you"]
# repos = ["product-api:src/billing/**", "product-worker:**"]
"""
