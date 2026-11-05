"""Shared fixtures.

The real embedding model is a ~30MB download and slow to load. Tests replace
`hippomem.embedding.embed` with a deterministic hashed bag-of-words vector, so
similar text gives similar vectors and the suite stays offline and fast. All
code calls the model through the module, so this one patch covers everything.
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from hippomem import db, embedding, systems

DIMS = 64


def fake_embed(text: str) -> list[float]:
    vec = [0.0] * DIMS
    for word in text.lower().replace(".", " ").replace(",", " ").replace(":", " ").split():
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % DIMS] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


@pytest.fixture(autouse=True)
def stub_embeddings(monkeypatch):
    monkeypatch.setattr(embedding, "embed", fake_embed)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    ticks = itertools.count()

    def tick() -> str:
        n = next(ticks)
        return f"2026-10-28T{10 + n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"

    monkeypatch.setattr(db, "now", tick)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    for actor_id, kind in (("@alex", "engineer"), ("@sam", "engineer"), ("@kim", "admin"), ("watchman", "reviewer")):
        connection.execute("INSERT INTO actor (id, kind, name, created_at) VALUES (?, ?, ?, ?)", (actor_id, kind, actor_id, "2026-10-28T00:00:00Z"))
    systems.create_system(connection, "billing", "Billing", owners=("@alex",))
    systems.create_system(connection, "web", "Web")
    systems.add_mapping(connection, "product-api", "src/billing/**", "billing", created_by="@alex")
    systems.add_mapping(connection, "product-worker", "**", "billing", created_by="@alex")
    systems.add_mapping(connection, "product-web", "**", "web", created_by="@alex")
    yield connection
    connection.close()
