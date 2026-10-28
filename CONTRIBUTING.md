# Contributing

## Dev loop

```bash
git clone https://github.com/z10-labs/hippomem.git
cd hippomem
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Before pushing, run the same checks CI runs:

```bash
ruff check .
ruff format --check .
mypy src/
pytest --cov=src --cov-report=term-missing
```

The project is kept `ruff format`-clean, not hand-formatted.

## Test stubbing

`tests/conftest.py` replaces two things that would otherwise make the suite slow, networked or costly:

- **The embedding model.** `hippomem.embedding.embed` is patched with a deterministic hashed bag-of-words vector. All code calls the model through the module (`embedding.embed(...)`), so this single patch covers everything. If you add a `from hippomem.embedding import embed` somewhere, that module keeps its own reference to the real function and the stub silently stops applying.
- **The judge.** Merge reconciliation asks a model which drafts a diff actually built. `hippomem.judge.run` is patched so tests decide the verdict directly.

Retrieval scores in tests come from the fake embedding, not real semantics. Don't calibrate thresholds against it; calibrate against the real model separately and assert only filtering behaviour in unit tests.

## Recording decisions

If you make a choice someone could reasonably have made differently, add a row to [docs/build-decisions.md](docs/build-decisions.md) with the reason and what you rejected.
