# Hippomem — Agent Instructions

Hippomem is a team's engineering memory for AI coding agents. Read [docs/hippomem.md](docs/hippomem.md) before changing behaviour: it defines what the memory holds, the decision lifecycle and the plan. Decisions made while building it are in [docs/build-decisions.md](docs/build-decisions.md); add a row there for any new choice someone could reasonably have made differently.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Before every commit, run what CI runs:

```bash
ruff check . && ruff format --check . && mypy src/ && pytest
```

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses (frozen where possible), module-level functions. No ORM, no DI framework.
- SQLite through stdlib `sqlite3`. Schema changes go in `src/hippomem/db.py` as a new numbered migration; never edit a shipped one.
- Call the embedding model only through `hippomem.embedding` (`embedding.embed(...)`, not `from hippomem.embedding import embed`). Tests patch that one module; a `from` import would silently bypass the stub and load the real model.
- Anything that calls a model to judge drafts goes through `hippomem.judge`, so tests can replace it.

## Tests

`pytest`. The embedding model and the judge are stubbed in `tests/conftest.py`, so the suite runs offline and fast. End-to-end scenarios with real headless agents live in `scenarios/` and are run separately; see that folder's README.
