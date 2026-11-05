# Hippocampus — Improvement Plan

Execution plan for coding agents. Derived from a full read of `main` at `5bc2891`, a run of the
test suite, and direct probing of the tool surface. Every claim marked **Verified** below was
reproduced against the actual code, not inferred from the README.

---

## How to use this plan

Each work package (WP) is one branch, one PR, one review. Do not batch them.

Before starting any WP:

1. Read this whole file. The WPs share state; WP-07 changes a file format WP-02 also touches.
2. Check the dependency graph in [Sequencing](#sequencing). Do not start a WP whose dependencies
   are unmerged.
3. Reproduce the **Repro** section of your WP. If the bug does not reproduce, stop and report —
   it may already be fixed by an earlier merge, and the WP may need rescoping.

While working:

- Stay inside the **Files** list. If a fix seems to require touching a file not listed, stop and
  say so in the PR description rather than expanding scope silently.
- Every WP ships with tests. A WP with no new test is not done.
- Do not "improve" adjacent code you happen to be reading. Note it in the PR body instead;
  it becomes a future WP.
- Run `pytest` before every commit. The suite is fast (~1s with stubbed embeddings) — there is
  no excuse for a red commit.

Conventions: readability first, no premature abstraction, descriptive names, no cleverness.
Match the surrounding style — this codebase uses dataclasses, `from __future__ import annotations`,
and module-level functions. Do not introduce classes, DI frameworks, or an ORM.

---

## Baseline

```bash
git clone https://github.com/z10-labs/hippocampus.git && cd hippocampus
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" 'mcp<2'      # the pin is WP-01; without it, install is broken
pytest                                # expect: 71 passed
```

Current state: 1,647 lines, 71 tests, 5 source modules, no CI, no linter.

---

## Sequencing

```
WP-01 (pin mcp) ─── unblocks everything, merge first, same day
   │
   ├── WP-11 (CI) ──────────── can run in parallel after WP-01
   │
   ├── WP-02 (cold start) ──┬── WP-07 (performance)   [both touch indexer.py; 02 first]
   │                        │
   ├── WP-03 (alternatives) ─┼── WP-13 (docs + revalidation)   [needs 03, 04 merged]
   │                        │
   ├── WP-04 (status) ──────┤
   │      │                 │
   │      └── WP-05 (deferred indexing)
   │
   ├── WP-06 (relevance floor)
   ├── WP-08 (hardening)
   ├── WP-09 (chain: blast radius)
   └── WP-10 (two-phase enforcement)

WP-12 (mcp 2.x port) — after WP-11, independent of the rest
```

**Priority order if capacity is limited:** WP-01, WP-02, WP-03, WP-04. Those four are the
difference between a tool that misleads an agent and one that helps it. Everything else is
improvement on a working base.

---

# P0 — Correctness and installability

## WP-01 — Pin the `mcp` dependency below 2.0

**Priority:** P0, ship today. **Size:** trivial.

### Problem

The documented install produces a server that cannot start. `pyproject.toml` declares
`mcp>=1.0.0`; the resolver selects mcp 2.0.0, which relocated `mcp.server.fastmcp`.

### Repro — Verified

```bash
python -m venv /tmp/v && /tmp/v/bin/pip install -q -e ".[dev]"
/tmp/v/bin/python -c "from mcp.server.fastmcp import FastMCP"
# ModuleNotFoundError: No module named 'mcp.server.fastmcp'
/tmp/v/bin/python -m pytest -q
# ERROR tests/test_server.py — Interrupted: 1 error during collection
```

With `mcp<2` installed (resolves to 1.29.0), the import succeeds and all 71 tests pass.

### Files

- `pyproject.toml`

### Change

```toml
dependencies = [
    "mcp>=1.0.0,<2",
    "fastembed>=0.4.0",
    "numpy>=1.26.0",
]
```

Add a short comment above the pin pointing at WP-12 so the next reader knows it is deliberate
and temporary.

### Acceptance

- Fresh venv, `pip install -e ".[dev]"`, `pytest` → 71 passed, no manual pin needed.
- `python -c "from hippocampus.server import mcp"` exits 0.

### Out of scope

Porting to the 2.x API. That is WP-12 and needs its own testing.

---

## WP-02 — Fix cold start, stale reads, and deleted records

**Priority:** P0. **Size:** medium. **Depends on:** WP-01.

### Problem

The index at `.hippocampus/index.json` is gitignored and is only ever written as a side effect
of `hippocampus_log`. Three failure modes follow:

1. **Cold start.** A fresh clone has records in `.decisions/records/` and no index. Every query
   returns a confident false negative.
2. **Stale reads.** Records edited by hand, or during PR review, are not re-indexed until
   someone logs an unrelated decision.
3. **Deleted records are served forever.** `build_index` seeds `by_id` from the existing index
   and only ever adds to it. Nothing removes entries whose source file is gone.

Failure mode 1 is the most damaging output this tool can produce. The agent is told
*"no past constraints apply"* — the exact opposite of the truth — and proceeds to re-litigate
the decision the project exists to preserve.

### Repro — Verified

Cold start, same repo, same query, same record on disk:

```
# no index yet
>>> hippocampus_query("should we use in-memory rate limiting?")
"No relevant decisions found. Proceed — no past constraints apply."

# after build_index(root, force=True)
>>> hippocampus_query("should we use in-memory rate limiting?")
DR-0001  [direct | score: 0.302]  (performance · heavy)
  Redis for shared rate-limit counters
  Rejected: In-memory counters - breaks across replicas
```

Deleted record:

```
indexed: ['DR-0001']
# file deleted from .decisions/records/, then build_index(root, force=False)
after deleting the file, index still contains: ['DR-0001']
# and it is still returned by hippocampus_query("database choice")
```

### Files

- `src/hippocampus/indexer.py`
- `src/hippocampus/server.py`
- `tests/test_indexer.py`, `tests/test_server.py`

### Change

**1. Prune deleted records in `build_index`.** After scanning `record_files`, drop any entry in
`by_id` whose id was not seen in this scan. Reverse links are already recomputed across the full
corpus afterward, so they will heal automatically. Return the count as `removed` in the result dict.

**2. Add `ensure_index(root) -> None` to `indexer.py`.** Cheap freshness check, safe to call on
every tool invocation:

- If `.decisions/records/` does not exist → no-op.
- Compute `max(mtime)` across `*.md` and the file count.
- If the index is missing, or `max(mtime) > built_at`, or the file count differs from the entry
  count → call `build_index(root, force=False)`.

The stat scan is cheap. The incremental build already skips unchanged files, so the common case
embeds nothing.

**3. Call it.** At the top of `hippocampus_query`, `hippocampus_list`, and `hippocampus_chain`,
and once in `main()` before `mcp.run()` so the first query of a session is not the one that pays
for the initial build.

**4. Fix the messaging.** Three distinct states must read differently. The current wording asserts
absence of constraints in all of them:

| State | Message |
|---|---|
| `.decisions/records/` missing or empty | `No decision records exist yet in this project. Nothing has been recorded.` |
| Records exist, index empty after build | `N records found but none could be indexed — check record format (each needs a '# DR-NNNN: Title' heading).` |
| Records indexed, nothing scored above floor | `No decisions matched this query above the relevance threshold.` |

Never emit "Proceed — no past constraints apply." The tool cannot know that. It can only report
what it did or did not find.

### Acceptance

- Test: write two records to disk, never call `log`, call `hippocampus_query` → both are found.
- Test: index a record, edit its `## Why` on disk, query again → the new Why is returned.
- Test: index a record, delete the file, `build_index(force=False)` → entry is gone, `removed == 1`.
- Test: index two records where A `depends-on` B, delete B, rebuild → A's relationship no longer
  resolves to a phantom entry and query does not crash.
- Test: each of the three empty-state messages, asserted by substring.
- No test asserts the string "no past constraints apply" anywhere.

### Notes for the implementer

Watch the mtime comparison: `built_at` is unix ms (`int(time.time() * 1000)`), file mtimes are
converted with `int(st_mtime * 1000)`. Keep both in ms. On filesystems with coarse mtime
granularity a record written in the same millisecond as the build can be missed — the file-count
check is the backstop, but prefer `>=` over `>` if you see flakiness in tests.

---

## WP-03 — Capture rejected alternatives

**Priority:** P0. **Size:** medium. **Depends on:** WP-01.

### Problem

"What was rejected, and why" is the project's opening pitch — the one thing git does not record.
Records written through `hippocampus_log` cannot contain it:

- `hippocampus_log` has no `alternatives` parameter.
- `write_standard_record` hardcodes `_See description above._`.
- `write_heavy_record` hardcodes `_No alternatives documented._`.
- `_parse_alternatives` only matches lines beginning `- `, so it reads both templates as empty.

Alternatives stated in prose in the `description` are silently dropped on the round trip.

### Repro — Verified

```
logged: "Use Postgres LISTEN/NOTIFY for the job queue. We rejected RabbitMQ (extra ops burden)
         and rejected Redis streams (no durability guarantee we need)."

written to disk:
  ## Alternatives Considered
  _No alternatives documented._

returned to the next agent:
  DR-0001  Postgres LISTEN/NOTIFY for job queue
    Rejected: (none documented)
```

### Files

- `src/hippocampus/logger.py`
- `src/hippocampus/server.py`
- `src/hippocampus/indexer.py`
- `tests/test_logger.py`, `tests/test_indexer.py`, `tests/test_server.py`

### Change

**1. Thread the parameter through.** Add `alternatives: Optional[list[str]] = None` to
`write_standard_record` and `write_heavy_record`. Render as a markdown bullet list under the
existing heading; fall back to the current placeholder only when genuinely absent.

**2. Expose it on the tool.** `hippocampus_log` gains `alternatives: Optional[str] = None`,
accepting a JSON array of strings (`'["RabbitMQ — extra ops burden", "Redis streams — no durability"]'`).
Reuse the existing relationships JSON error-handling shape for consistency, including the example
in the error string.

**3. Require it for `heavy`.** In Phase 2, if `classification.weight == "heavy"` and no
alternatives were supplied, return an error rather than writing a placeholder record. Message
should be actionable: name the parameter, give the JSON shape, and state that a heavy record
without alternatives has no value to a future reader. Standard records stay optional but the
docstring should encourage them.

**4. Update the docstring.** Agents read it as the spec. Document the parameter, the format, and
the heavy requirement. Add the guidance that each entry should be `option — reason rejected`,
not a bare option name.

**5. Loosen `_parse_alternatives`.** Accept `-`, `*`, and `1.` bullet styles. Keep the existing
guard that skips continuation lines starting with `because`. Keep the 80-char truncation.

### Acceptance

- Test: log with alternatives → the written file contains one bullet per entry under
  `## Alternatives Skipped` / `## Alternatives Considered`.
- Test: full round trip — log, reindex, query → `Rejected:` lines contain the supplied text.
- Test: `weight="heavy"` with `confirmed=True` and no alternatives → error return, no file written.
- Test: `_parse_alternatives` handles `-`, `*`, and numbered bullets.
- Test: malformed alternatives JSON → clear error, no file written.

### Out of scope

Extracting alternatives from prose with an LLM. The two-phase flow is the mechanism for getting
the agent to supply them explicitly; make the parameter exist first.

---

## WP-04 — Surface record status, and demote superseded decisions

**Priority:** P0. **Size:** small. **Depends on:** WP-01.

### Problem

`apply_supersedes` correctly patches the target file to `**Status**: superseded by DR-NNNN`, and
`IndexEntry` carries `status`. But `RetrievalResult` has no `status` field, so it never reaches
the agent. A dead decision renders identically to a live one.

### Repro — Verified

```
--- DR-0001 status line on disk ---
['**Status**: superseded by DR-0002']

--- what the agent sees ---
DR-0002  [direct | 0.338]  SQS for job queue
DR-0001  [direct | 0.164]  Postgres LISTEN/NOTIFY for job queue    ← dead, rendered as live
```

The README argues that pure similarity search "would happily hide" the superseding record. It
does not hide it here — the graph hop works. It simply fails to say which of the two is dead.

### Files

- `src/hippocampus/types.py`
- `src/hippocampus/retriever.py`
- `src/hippocampus/server.py`
- `tests/test_retriever.py`, `tests/test_server.py`

### Change

**1.** Add `status: str = "accepted"` to `RetrievalResult`. Populate it at all four construction
sites in `retriever.query` — direct, outbound, inbound, soft-related. Missing one is the likely
bug here; grep for `RetrievalResult(` and confirm the count.

**2.** Render prominently in `hippocampus_query` and `hippocampus_list`. Superseded and deprecated
records get a marker on the id line, before the title, where it cannot be skimmed past:

```
DR-0001  [direct | score: 0.164]  (architectural · heavy)  ⚠ SUPERSEDED BY DR-0002
  Postgres LISTEN/NOTIFY for job queue
```

Parse the target id out of the status string when present; degrade to a bare `⚠ SUPERSEDED`
if it is not parseable.

**3.** Demote in ranking. Apply a multiplier (start at `0.5`) to the similarity score of any
record whose status is not `accepted` or `proposed`, before the top-N cut. Keep the multiplier a
named module constant so it is tunable and testable. Do not filter superseded records out — the
superseded argument is part of the history and the README promises it stays readable.

**4.** Also render status in `hippocampus_chain` output.

### Acceptance

- Test: supersede DR-0001 with DR-0002, query → output contains `SUPERSEDED BY DR-0002` on the
  DR-0001 line.
- Test: given equal raw similarity, an accepted record outranks a superseded one.
- Test: a superseded record still appears in results (not filtered away).
- Test: `hippocampus_list` shows status.

---

# P1 — Retrieval quality

## WP-05 — Index deferred decisions

**Priority:** P1. **Size:** medium. **Depends on:** WP-02, WP-04.

### Problem

`write_deferred_entry` appends to `.decisions/deferred.md`. Nothing ever reads it. `build_index`
globs `.decisions/records/*.md` only. So *"we consciously chose not to decide this yet"* — which
the README explicitly calls out as worth remembering — lands in a file the agent will never look
at. That is the precise failure mode the project was built to fix, reproduced inside the project.

### Files

- `src/hippocampus/indexer.py`
- `src/hippocampus/retriever.py`, `src/hippocampus/server.py`
- `tests/test_indexer.py`

### Change

Parse `deferred.md` into index entries. Each `## YYYY-MM-DD — title` block becomes one entry with
id `DEF-NNNN` (sequential by position in file), `weight="deferred"`, `status="deferred"`, and the
`What was deferred` / `Why deferred` fields mapped onto `document` and `why`.

Render with a distinct badge so an agent does not mistake a deferral for a decision:

```
DEF-0003  [direct | score: 0.41]  (deferred)  ⏸ NOT YET DECIDED
  Multi-region replication
  Why: Deferred until we have a second region. Review trigger: EU customer signs.
```

Guard the id scheme: `DEF-` must not collide with `DR-` anywhere that assumes the `DR-\d{4}`
shape — check `_parse_relationships`, `hippocampus_chain`, and `apply_supersedes` before merging.
Deferred entries participate in retrieval but not in the relationship graph.

While here: `write_deferred_entry` hardcodes `Why deferred: Not documented` and
`Review trigger: Not specified`. Add optional `why` and `review_trigger` parameters and thread
them from `hippocampus_log`, mirroring WP-03. A deferral with no review trigger never gets
revisited, which makes it a leak rather than a decision.

### Acceptance

- Test: write two deferrals, build index, query → both retrievable.
- Test: deferred entries render with the `NOT YET DECIDED` marker.
- Test: `hippocampus_list(weight="deferred")` returns them.
- Test: a `DEF-` id does not break `hippocampus_chain` or relationship parsing.

---

## WP-06 — Add a relevance floor and retune thresholds

**Priority:** P1. **Size:** small. **Depends on:** WP-02.

### Problem

`hippocampus_query` returns `top_n` results unconditionally with no minimum score. A query about
CSS grid returns the five least-irrelevant infrastructure decisions in the repo, formatted
identically to genuine constraints. Noise at this position is expensive: the agent treats the
output as prior art.

Two related inconsistencies:

- Phase 1 of `hippocampus_log` filters at `score >= 0.20`. `hippocampus_query` filters at nothing.
  Same index, same embedding model, two different notions of relevance.
- `SOFT_RELATED_THRESHOLD = 0.80` is far too high for `all-MiniLM-L6-v2`. Genuinely related
  technical sentences typically land in the 0.4–0.6 cosine range on this model. The soft-related
  branch is close to dead code — verify by instrumenting a real corpus before changing the number.

### Files

- `src/hippocampus/retriever.py`
- `src/hippocampus/server.py`
- `tests/test_retriever.py`

### Change

Introduce module constants and use them in both call paths:

```python
MIN_DIRECT_SCORE = 0.25        # below this, a "match" is noise
SOFT_RELATED_THRESHOLD = 0.50  # was 0.80 — effectively unreachable
```

Filter direct hits below `MIN_DIRECT_SCORE` before assembling results. Relationship-expanded
results are exempt — they earn their place structurally, not by similarity, and that is the point
of the second hop. Expose `min_score: Optional[float] = None` on `query()` so a caller can
override; have Phase 1 of `log` pass its own value rather than filtering separately.

**Calibrate before you commit to the numbers.** Build an index over 20–30 real records, run a set
of queries you know the right answers to, and print the score distribution for hits and misses.
Put the resulting numbers and the reasoning in the PR body. Do not tune against `fake_embed` —
its scale is unrelated to the real model.

### Acceptance

- Test: query with no semantically related records → empty result, and the
  "nothing matched above threshold" message from WP-02, not a list of junk.
- Test: a relationship-expanded record with `score == 0.0` still appears (floor does not apply).
- Test: `min_score` override is respected.
- Tests assert filtering behaviour via injected scores, never a hardcoded numeric threshold —
  otherwise retuning the constant breaks the suite.

---

## WP-07 — Performance: vectorize, cache, batch

**Priority:** P1. **Size:** medium. **Depends on:** WP-02 (both change `build_index`; merge 02 first).

### Problem

numpy is already a declared dependency and is unused. Measured at 500 records, 384 dims:

| Operation | Now | Achievable |
|---|---|---|
| Cosine scan | 27 ms | 0.35 ms (**77×**) |
| `json.loads` per query | 100 ms | ~0 (in-memory cache) |
| Index on disk | 5.9 MB | 0.77 MB |

Three separate causes:

1. `_cosine` is a pure-Python loop and recomputes both magnitudes on every comparison — including
   the stored embedding's magnitude, which never changes.
2. `load_index` re-reads and re-parses the entire JSON file on every single query.
3. `_save_index` writes with `indent=2`, which on lists of 384 floats is most of the file size.

Additionally, `build_index` calls `embed()` once per record in a loop. fastembed is substantially
faster given a batch.

### Files

- `src/hippocampus/indexer.py`
- `src/hippocampus/retriever.py`
- `tests/conftest.py`, `tests/test_indexer.py`, `tests/test_retriever.py`

### Change

Do these in order, and measure after each. Stop early if the numbers are good enough — do not
gold-plate a cache for a corpus of thirty records.

**1. Normalize at index time.** Store unit-length embeddings. Cosine similarity then reduces to a
dot product, and the magnitude work disappears entirely.

**2. Replace the scan with numpy.** Hold embeddings as a single `(N, D)` float32 array; a query
becomes `matrix @ q_normalized` plus `argsort`. Assert parity against the current `_cosine` to
within `1e-5` in a test so the refactor is provably behaviour-preserving.

**3. Add an in-memory cache.** Key on `(path, built_at, file mtime)`. Invalidate when
`build_index` writes. This is the single biggest win and the simplest — do it before anything
else if you only do one.

**4. Drop `indent=2`** from `_save_index`. It is a gitignored derived cache; nobody reads it.

**5. Batch the embedding calls.** Add `embed_many(texts: list[str]) -> list[list[float]]` and
collect all texts needing embedding in `build_index` before making one call.

**6. Add an index schema `version` field.** Any of the above changes the on-disk shape. Bump the
version; on mismatch, `load_index` discards and forces a full rebuild rather than crashing on a
`KeyError` in someone's existing project.

**Optional, only if measurements justify it:** store embeddings in a float32 `.npy` sidecar next
to `index.json`, keeping metadata in JSON. Cleaner and 8× smaller, but it introduces a two-file
consistency problem. Skip unless the corpus is large.

### Acceptance

- Test: numpy scoring matches the reference `_cosine` implementation within `1e-5`.
- Test: an index written with an older/absent `version` triggers a rebuild instead of raising.
- Test: the cache returns fresh data after `build_index` writes (no stale reads through the cache).
- `conftest.py` updated: it currently patches `indexer.embed` and `retriever.embed` separately
  because `retriever` does `from ... import embed`. If you add `embed_many`, patch it in both
  places too, or the tests will silently hit the real model and start downloading 30 MB.
- PR body includes before/after timings from a real corpus.

### Note

This retires the README's "retrieval is a linear scan" limitation for corpora into the low
thousands, without an ANN index. Update that section in WP-13, and be accurate: it is still a
linear scan, just a vectorized one.

---

# P2 — Hardening

## WP-08 — Small correctness fixes

**Priority:** P2. **Size:** small. **Depends on:** WP-01.

Four independent fixes. One PR is acceptable since each is a few lines, but use separate commits.

**1. Stale lock file.** `_with_lock` creates `.decisions/.write.lock` with `O_CREAT | O_EXCL` and
has no staleness check. One crashed or killed process leaves the lock behind and every subsequent
write fails permanently with `Could not acquire decision log lock within 5 seconds`, with no hint
that the fix is to delete a file. Prefer `fcntl.flock` on a persistent lock file — the OS releases
it on process death, which removes the failure mode entirely. If portability to Windows matters,
keep the current scheme but write the pid, and on contention check whether that pid is alive
before waiting out the full timeout. Either way, the error message must name the lock file path.

**2. `apply_supersedes` rewrites every matching line.** The `re.sub` has no `count=1`, so a record
containing the string `**Status**:` anywhere else in its body (in a quoted example, say) gets both
lines rewritten. Add `count=1`.

**3. `_parse_file` field extraction.** The `(re.search(...) or type('', (), {'group': lambda s, n: ''})()).group(1)` construction is repeated four times and builds a
throwaway class to fake a match object. Replace with a small helper:

```python
def _field(content: str, name: str, default: str = "") -> str:
    m = re.search(rf'\*\*{name}\*\*:\s*(.+)', content)
    return m.group(1).strip() if m else default
```

Pure refactor. No behaviour change. Existing tests must pass untouched.

**4. `_next_id` is racy across processes.** It reads the max id, then the caller writes. Two agents
logging concurrently in the same repo can pick the same number. It is inside `_with_lock` for the
write, but `_next_id` itself is called within `write()` so this is probably fine — **verify it**,
and if it is fine, add a test asserting two sequential writes produce distinct ids so it stays fine.

### Acceptance

- Test: a stale lock file does not block writes indefinitely (simulate by writing a dead pid, or
  by verifying flock releases).
- Test: `apply_supersedes` on a record containing two `**Status**:` occurrences patches only the
  header one.
- Test: two consecutive `write_standard_record` calls produce DR-0001 and DR-0002.
- All 71 existing tests still pass with no modification.

---

## WP-09 — `hippocampus_chain`: add the blast-radius direction

**Priority:** P2. **Size:** small. **Depends on:** WP-02.

### Problem

The tool's own docstring says it is *"useful before modifying a decision that others may depend
on"* — but `render()` only follows `depends-on` outward, which is what the target depends *on*.
The set of records that would break if you change it is the *inbound* direction. That is
`reverse_links`, already computed at index time and sitting unused by this tool.

The README sells this explicitly: *"Inbound — what depends on this decision. The blast radius if
you change it."* `hippocampus_query` does the inbound hop. `hippocampus_chain` does not.

There is also dead code in `render()`: `unvisited` is computed and used only as a truthiness check
for printing a header, then the loop iterates the full `deps` list anyway. Clean it up while here.

### Files

- `src/hippocampus/server.py`
- `tests/test_server.py`

### Change

Render two sections. Upward: what constrains this decision (current behaviour). Downward: what
breaks if it changes, following `depended-on-by`, `overridden-by`, and `superseded-by` reverse
links recursively.

```
Decision chain for DR-0006:

▶ DR-0006  (performance · heavy)   Redis for shared counters

  ── depends on ──────────────────
  └─ DR-0002  Single shared cache tier

  ── blast radius (would be affected by a change) ──
  └─ DR-0014  Sliding-window rate limiter
  └─ DR-0011  Distributed lock on job claim
```

Keep the existing cycle guard. Use a separate `visited` set per direction, or a record can be
suppressed in one section because the other already rendered it. Add a depth cap (10 is plenty)
so a pathological graph cannot produce unbounded output into an agent's context window.

### Acceptance

- Test: A ← B, A ← C → `chain("DR-A")` lists B and C under blast radius.
- Test: a cycle A → B → A terminates and renders each record once per direction.
- Test: an unknown id returns the existing `(not in index)` message rather than raising.
- Test: depth cap engages on a chain longer than the limit.

---

## WP-10 — Enforce the two-phase log flow

**Priority:** P2. **Size:** small. **Depends on:** WP-03.

### Problem

The README describes the two-phase `log` flow as the mechanism that pushes back against
low-quality records: *"An agent that logs `used Redis` with no Why produces a record worth
nothing. The two-phase log flow exists to push against exactly this."*

Nothing enforces it. An agent can pass `confirmed=True` on the first call and skip Phase 1
entirely, never seeing the related-decisions list it is supposed to link against. The safeguard
is advisory, and agents optimizing for turn count will route around it.

### Files

- `src/hippocampus/server.py`
- `tests/test_server.py`

### Change

Phase 1 returns a short token derived from the description (an 8-char hash is sufficient — this
is a guard rail, not a security boundary). Phase 2 requires it:

- `confirmed=True` with no token → return Phase 1 output plus an explicit instruction to call
  again with the token. Do not write.
- Token mismatch → same, with a note that the description changed since Phase 1.

Keep the tokens in a module-level dict with a bounded size. The server is a per-session stdio
process; persistence across restarts is not needed and would be a liability.

Update the `hippocampus_log` docstring to document the token, since that docstring is the
contract agents actually read.

**Judgment call for the PR discussion:** this trades agent convenience for record quality. If you
want the softer version, make the token optional but have Phase 2 without it emit a visible
warning in the record's provenance. Decide deliberately and — appropriately — log the decision.

### Acceptance

- Test: `confirmed=True` on a first call writes no file and returns Phase 1 content.
- Test: Phase 1 → Phase 2 with the returned token writes the record.
- Test: Phase 2 with a stale token for a changed description is rejected.
- Test: the deferred path still works end to end.

---

## WP-11 — CI and linting

**Priority:** P2 (but cheap and high leverage). **Size:** small. **Depends on:** WP-01.

### Problem

No CI. WP-01 — a broken install for every new user — would have been caught the day mcp 2.0
shipped by a scheduled job doing nothing more than a clean install and a test run.

### Files

- `.github/workflows/ci.yml` (new)
- `pyproject.toml` (ruff and mypy config, dev extras)

### Change

Workflow on push, PR, and a weekly schedule. Matrix over Python 3.11 and 3.12 to match the
declared `requires-python`. Steps: clean install from `pyproject.toml`, `ruff check`,
`ruff format --check`, `pytest --cov`, `mypy src/`.

The weekly cron matters more than the push trigger here: this repo's realistic breakage mode is
an upstream dependency moving, not a bad commit.

Add a second job that installs from a clean checkout exactly as the README instructs
(`pip install git+...` equivalent) and asserts `hippocampus-mcp --help` exits 0. That job is the
one that catches WP-01-class failures.

Start `mypy` non-strict; the codebase is fully annotated already so it should be close to clean.
Do not chase strictness in this WP — get the gate in place, tighten later.

### Acceptance

- CI green on `main`.
- Deliberately reverting the WP-01 pin locally makes the install job fail. Verify this; a CI job
  that cannot fail is decoration.
- Coverage reported (no threshold gate yet — set one once the P0 work lands and coverage is stable).

---

# P3 — Follow-through

## WP-12 — Port to the mcp 2.x API

**Priority:** P3. **Size:** unknown until scoped. **Depends on:** WP-11.

WP-01 pins below 2.0 to stop the bleeding. This removes the pin properly.

Scope it first: read the mcp 2.x migration notes, determine where `FastMCP` moved and what
changed in the decorator and run APIs, and post findings in the issue before writing code. If the
surface is broadly compatible, this is small. If not, decide explicitly whether to support both
via a compatibility shim or to require 2.x and bump hippocampus to 0.3.0.

Do not start this until CI exists — the whole point is having a gate that proves the port works
on a clean install.

---

## WP-13 — Reconcile README and re-run validation

**Priority:** P3, but **blocking any public launch**. **Size:** small. **Depends on:** WP-03, WP-04, WP-07.

### Problem

The README makes claims the current code does not support. Most are fixed by the P0 work, which
is why this WP comes last — but they must be checked off deliberately rather than assumed.

The one needing real attention:

> Will agents write records with accurate relationships and alternatives?
> **3/3 records per run, both fields non-empty**

Against `main`, the alternatives field is structurally guaranteed to be empty for every record
written through the tool (WP-03). Whatever produced that result, it is not reproducible against
this code path. Someone will check — the repo links its own validation data, which is exactly the
kind of honesty that invites verification.

### Change

1. After WP-03 merges, re-run the validation suite in `hippocampus-validation` and publish the
   actual numbers. If they hold, say so with the commit hash. If they do not, update the table.
   A weaker honest number is worth more than a strong unreproducible one, particularly for a
   project whose entire pitch is *preserving the real reasoning*.
2. Update the **Limitations** section: revise the "linear scan" note per WP-07, and add a note on
   the classifier's known failure modes.
3. Document the cold-start behaviour and any new CLI/reindex entry point from WP-02.
4. Document the `alternatives` parameter and the heavy-record requirement.
5. Pin the mcp version in the install instructions if WP-12 has not landed.
6. Add `CONTRIBUTING.md` covering the dev loop, the test-stubbing approach in `conftest.py`
   (non-obvious and easy to break), and the record-format contract.

---

## Deliberately not in this plan

Recorded so they are not silently reintroduced as scope creep:

- **ANN / vector database.** WP-07 makes the linear scan fast enough for the stated target of
  tens to hundreds of records, with headroom. Adding an ANN index would be optimizing past the
  actual use case.
- **Replacing the regex classifier with a model.** The README already names this as a limitation
  and both outputs are overridable per call. Fast, offline, and predictable is a defensible
  trade. Revisit only if misclassification shows up in real usage data.
- **Remote/multi-user server mode.** Records are files in a repo; git is the sync layer. That is
  the design, and it is a good one.
- **A record-linting CLI** (`hippocampus lint` in CI: missing Why, dangling DR references, records
  with no alternatives). Genuinely useful and a natural fit once WP-03 and WP-11 land — but it is
  a new feature, not a fix. File it as a follow-up issue, not a WP here.

---

## Definition of done — every PR

- [ ] Branch named `wp-NN-short-slug`, one WP per PR.
- [ ] The **Repro** in the WP no longer reproduces.
- [ ] New tests, all acceptance criteria covered.
- [ ] Full suite green (`pytest`), no skipped tests added.
- [ ] No files touched outside the WP's **Files** list, or the deviation is explained in the PR body.
- [ ] PR body states what was verified and how — commands and output, not assertions.
- [ ] Behaviour changes reflected in the affected tool docstrings. Agents read those as the spec;
      a stale docstring is a live bug.
- [ ] Any non-obvious design choice made along the way is logged as a decision record in this
      repo's own `.decisions/`. Dogfood it — it is the fastest way to find the next set of gaps.