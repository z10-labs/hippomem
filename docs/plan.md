# Hippomem plan: from here

Written 2026-10-29, revised 2026-11-05. Phases 1-4 of [hippomem.md](hippomem.md) are built and merged, and the scenario harness passes 28 of 29 checks ([scenario-results.md](scenario-results.md)). This plan covers what comes next: the research that proves the memory is accurate, current and versioned, the pilot, and making the decision graph a first-class part of the product. How the research is run is in [research-plan.md](research-plan.md). Build choices so far are in [build-decisions.md](build-decisions.md).

**Revised 2026-11-05** after review: a smaller first ontology; one schema change before the experiments, then frozen while they run; one shared store settled now rather than later; Postgres as the default engine, with graph engines evaluated only for questions it fails.

Hippomem's job is quick, factual retrieval of **why** and **how** things are the way they are. The relationships between decisions are what carry the why, so they are a core model, not a feature added later.

## Where we are

**Built (PRs 15 to 18, merged 2026-11-05):** SQLite store, versioned decisions, drafts and branches, proposals with acceptance rules, merge reconciliation through a judge, explored records, MCP server and JSON API, Claude Code and git hooks, digest and before-edit lookup, scenario harness. 102 tests, 90% coverage.

**The ontology as built** (`src/hippomem/db.py`):

| Kind | What exists |
|---|---|
| Nodes | decision (versioned), system, explored record, draft, branch, actor, evidence, proposal |
| Attached to a decision, not nodes | constraint, alternative, anchor |
| Edges | `depends-on`, `supersedes`, `conflicts-with` (decision to decision only) |
| Search | similarity, relevance floor, then **one hop** along links, both directions |

**What the graph work needs that does not exist yet:**

| Gap | Why it matters |
|---|---|
| Constraints belong to one decision | "This constraint stopped being true, which decisions rest on it?" cannot be asked |
| Alternatives, constraints and anchors belong to the decision, not its version | Revising a decision keeps the old text but loses the old version's alternatives and constraints, so history is only half kept |
| Only three edge types | No `motivated-by`, `constrained-by`, `caused-by`, `implemented-by`, `affects`; the why has no structure beyond text |
| Rejected alternatives are not linked to explored records | A tried-and-dropped option and a never-built one look unrelated |
| Versions have `valid_from` and `recorded_at` but no `valid_to` | "What did we believe in March?" needs the end of each version's validity, which today is only implied by the next version |
| Links carry no provenance (who or what derived it, confidence) | A judge-inferred edge looks the same as an engineer-confirmed one |
| One-hop expansion only | Lineage, blast radius and staleness need multi-hop traversal |

## Track A: land the build (done)

PRs 15 to 18 merged on 2026-11-05. `docs/Improvements.spec` is the old Hippocampus POC plan; it stays untracked.

## Track R: research (Claude, autonomous)

Controlled experiments on a fictional team (Meridian) and a replay of a real open-source project, measuring whether the memory is factual, current, versioned, important and timely. Runs before the pilot, so the pilot starts on a product with measured numbers. Full method in [research-plan.md](research-plan.md).

## Track B: pilot (phase 5, needs the owner, after the research)

Unchanged from [hippomem.md](hippomem.md#plan). Pick two or more repos that share a system, run for a couple of weeks, and measure:

- how often agents query before deciding
- contradictions caught before merge
- proposal acceptance rate
- cold-read understanding across repos
- the same tasks with and without Hippomem

**Add to the pilot:** log every query with its result set, so the query set in Track C can be scored against real use.

## Track C: decision graph

C1 to C4 happen inside the research (phase R2), before the large re-measure. The schema is then frozen while experiments and the pilot run, so results are never measuring two different products. C5 and C6 use research and pilot data.

### C1. Ontology spec

A short, engine-neutral document, `docs/ontology.md`, versioned. It starts small and grows only when the query set or the experiments show a question the current types can't answer:

- **Node types:** Decision, Constraint (becomes a shared node), Explored record, System. Alternatives, anchors and evidence stay attached to a decision version.
- **Edge types**, each with direction, allowed endpoint types and properties:
  - `depends-on`, `supersedes`, `conflicts-with` (exist today, decision to decision)
  - `constrained-by` (decision → constraint)
  - `informed-by` (decision → explored record or another decision: the "because we tried X" link)
  - `same-as` (rejected alternative ↔ explored record)
- **Properties on every edge:** `derived_by` (rule, judge, engineer), `confidence`, `source`, `valid_from`, `valid_to`, `recorded_at`.
- **Invariants:** for example, an accepted decision resting on an invalidated constraint is flagged; a superseded decision has exactly one live successor chain.
- **Open parts:** tags and free text, so we do not over-model.
- **Not modelled as edges:** ownership and authorship (already `system_owner`, `made_by`), and implementation (already `evidence` and `anchor`). One fact gets one representation.

Candidates for later, each added only with a query that needs it: `reverses`, `invalidated-by`, `caused-by`, an Incident node (which also needs a way for incidents to get in), `affects`.

Relationships stay typed edges. A relationship becomes its own node only if the pilot shows people need to challenge or retract one on its own.

### C2. Query set

About thirty questions, written before any schema change, a third of them held out (see [research-plan.md](research-plan.md#the-query-set)). Examples: why did we choose X; what did we reject and why; what depends on this constraint; what contradicts this; what changed after incident Y; which decisions are stale. Each question has an expected answer from the scenario fixtures or the pilot repos. The set is both the ontology's test and the acceptance suite for the engine.

### C3. Schema migration

One new numbered migration (never edit a shipped one):

- constraint becomes a shared node with a link table to decisions
- alternatives, constraint links and anchors belong to a decision **version**, so revising a decision keeps the old version whole
- links can join any two records (decision, explored record, constraint), with the new edge types and the edge properties above
- `valid_to` on decision versions (`valid_from` exists)
- migrate existing per-decision constraints into shared ones; no data loss

This is the "schema change" from the open items in the brain task, and it is costly to undo, so it gets its own BD row.

### C4. Graph operations

Domain functions that sit between the tools and the store, so the engine behind them can change:

| Operation | Answers |
|---|---|
| `lineage(decision)` | how did we get here, with the reason at each step |
| `stale_dependents(constraint)` | what rests on something that stopped being true |
| `contradictions(scope)` | live decisions making incompatible claims: declared `conflicts-with` edges, plus the judge comparing a new proposal with similar accepted decisions at proposal time |
| `blast_radius(decision, depth)` | what a change touches, multi-hop with a depth cap |
| `why(decision)` | motivations, constraints and rejected alternatives, in one view |

MCP tools expose these. The judge (already the fuzzy-edge deriver) proposes new edge types the same way it proposes decisions now: as proposals, marked `derived_by: judge` with a confidence, until an engineer or a reviewer agent accepts.

### C5. Retrieval

Similarity and keyword search find entry nodes, then typed traversal assembles the answer. Start with depth-limited expansion over the new edge types. Move to Personalized PageRank only if the scenario and pilot results show plain expansion is too noisy. Community summaries (Microsoft-style GraphRAG) wait until someone actually asks global questions.

### C6. Engine decision (gate, not an assumption)

SQLite stays for the research and the pilot, per the "local only for now" direction. BD-04's reason for rejecting Postgres was weak and is corrected: the owner ruled out third-party hosting, not Postgres. At this data size (hundreds to a few thousand records) scale is not a reason to switch; remote access and concurrent writers are.

**Sync is settled: one shared store** that the whole team's agents connect to. "Local" means that server running on one machine. Per-person copies that merge would need conflict handling on reviewed records and contradict the end goal.

**Default engine: Postgres** with recursive queries for traversal and pgvector for similarity, one store for both. In research phase R6, load the same data into it and run the query set. A graph engine (Neo4j, Memgraph, FalkorDB, Postgres with Apache AGE, or an embedded one) is evaluated only for questions Postgres fails, checking each candidate's current state first because this landscape moves quickly. The operations in C4 are the swap boundary.

The parts of today's code that rely on one server process writing one thing at a time (ID counters, merge reconciliation, the self-approval check followed by acceptance) are listed in [architecture.md](architecture.md) as the checklist for the move.

## Decisions needed from the owner

| # | Decision | Needed by |
|---|---|---|
| 1 | Pilot repos (two or more sharing a system) | start of Track B |
| 2 | ~~Approve the shared-constraint schema change and the extra edge types~~ Approved with the revisions, 2026-11-05 | |
| 3 | ~~Correct BD-04~~ Done, 2026-11-05 | |
| 4 | ~~Local copies that sync, or one shared graph~~ One shared store, 2026-11-05 | |
| 5 | Whether a relationship ever needs to be its own challengeable node | after the pilot |

## Stop or rethink if

All the existing stop conditions in [hippomem.md](hippomem.md#stop-or-rethink-if), plus:

- the graph operations cannot answer most of the query set without hand-editing edges
- judge-derived edges are mostly rejected in review, which means the edge types are too fine or the judge prompt is wrong
- stale-dependent and contradiction flags are mostly false alarms, so engineers start ignoring them

## Order of work

1. Track A merges (done).
2. Research R0 and R1: the setup, the query set (C2), and a baseline of the product as it is.
3. Research R2: C1, C3 and C4, then freeze the schema.
4. Research R3 to R5: re-measure, the real-history replay, fixes.
5. Research R6: C6, Postgres against the query set.
6. Track B, the pilot, on the measured product, once the owner picks repos. C5 on pilot data.
