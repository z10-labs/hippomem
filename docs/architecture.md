# Hippomem architecture

How the proof of concept is built. What Hippomem is and why is in [hippomem.md](hippomem.md); how a team uses it is in [using-hippomem.md](using-hippomem.md); every choice made along the way is in [build-decisions.md](build-decisions.md).

## The pieces

```
 engineer's machine                                           Hippomem server (one process)
 ─────────────────                                            ─────────────────────────────
 Claude Code ──MCP over HTTP (/mcp)──────────────────────────► tools: query, draft, propose, decide, …
   hooks: hippomem hook session-start|prompt|pre-edit|stop ──► JSON API (/api): session, lookup, turn
 git hooks: post-checkout|post-commit|post-merge ────────────► JSON API (/api/event)
 hippomem CLI (query, review, accept, …) ────────────────────► JSON API
                                                                │
                                                                ├── SQLite memory file (WAL)
                                                                ├── embedding model (fastembed, local)
                                                                └── judge: headless `claude -p` for merges and closed branches
```

Every request carries a bearer token. Engineers, reviewer agents and admins each have one; a coding agent uses its engineer's token and names itself in `X-Hippomem-Agent`, so the record reads "@alex via claude-code".

## Modules

| Module | What it does |
|---|---|
| `db.py` | Connection, numbered migrations, transactions, sequential ids (D-, P-, X-, Q-) |
| `systems.py` | Systems, owners, dependencies, and repo + path glob → system mapping |
| `decisions.py` | Versioned decisions with scope, anchors, rejected alternatives, constraints, links, evidence |
| `explored.py` | Dead ends with a required "revisit if" |
| `proposals.py` | The only write path: create, accept (applies it), reject |
| `policy.py` | Who may accept what |
| `search.py` | Similarity + one-hop graph expansion; explored records and drafts kept separate |
| `drafts.py` | Branches and their draft trails |
| `reconcile.py` | Merge reconciliation and closed-branch sorting, with a judge fallback |
| `lifecycle.py` | Git events: checkout, commit, merge, close, stale |
| `sessions.py` | Session digest, before-edit lookup, end-of-turn check, surfacing proposals, author questions |
| `judge.py` | The one place a model is asked to judge |
| `service.py` | Operations shared by MCP tools and the JSON API, with usage logging |
| `server.py` | MCP tools, server instructions, JSON API |
| `hooks.py`, `gitinfo.py` | Client side: Claude Code hooks, git hooks, installers |
| `config.py`, `actors.py`, `cli.py`, `render.py` | Team config, tokens, the CLI, text for agents |

## A decision's path

```
agent decides while working ──hippomem_draft──► draft (branch trail)
agent reverses ──hippomem_draft(replaces=…)──► old draft "dropped" + reason
git commit ──post-commit──► drafts touching those paths are "backed"
merge into main ──post-merge──► judge(trail, diff)
        built ──► proposal (commits as evidence, dropped drafts as rejected alternatives)
        dead end with a lesson ──► explored record
        noise ──► discarded
proposal ──surfaced to an engineer at session start──► accepted ──► decision (version 1)
```

Decisions covering a whole module, service or team skip the judge: they go to an owner as proposals, either from `hippomem_propose` or from scope drafts at merge. A branch closed without merging, or stale for `stale_after_days`, has its trail sorted into explored records.

## What an agent sees without asking

| When | What |
|---|---|
| Session start | Digest: how to call the tools; decisions in force for the repo's systems; this branch's trail; other branches' drafts on the same systems; up to N proposals and any questions for its engineer |
| Before editing a file | Decisions anchored to it, system- and team-wide rules that apply, open proposals about it, and, if nothing covers it and someone else wrote it, a suggestion to ask the author |
| End of a turn | If decision-shaped files changed (dependencies, schema, infrastructure) and nothing was drafted: one nudge |
| Always | Server instructions: query before non-trivial choices; draft decisions and reversals; never accept on your own |

## Failure behaviour

- Hooks never fail the agent or git. Errors go to `hooks.log` or `git-events.log` in the Hippomem home.
- A failed judge at merge falls back to the trail's structure and logs a `judge_failed` event; a failed judge at close leaves the trail to sort again (`hippomem branch close` retries).
- Search says when nothing matched, and that this does not mean no constraints apply.

## Moving to remote later

The roles, data model and event flow don't change. What changes: the SQLite file becomes Postgres (pgvector for embeddings), git events come from a GitHub app instead of local hooks, tokens come from a real identity provider, and the review queue gets a web page. The module boundaries above are where those swaps happen.
