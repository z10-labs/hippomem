# Hippomem

**Hippomem is a team's engineering memory.** It records why the team's software is the way it is: the designs it chose and the ones it rejected, the decisions and the constraints behind them. Agents and engineers read it before they decide something, and write to it as they work.

This document defines what Hippomem is, how it works and the plan for proving it. Use it to judge scope: work that doesn't move the plan below waits. How a team installs and uses it is in [using-hippomem.md](using-hippomem.md).

## The problem

Coding agents make many design decisions a day, inside conversations nobody rereads. An agent picks a queue over cron, tries Redis and drops it, decides the billing service should own all money writes. Git keeps the result. The reasoning, and the options that were tried and rejected, disappear when the session goes idle.

So the next agent, on another branch or in another repo, re-argues a settled question, reaches for the library the team already ruled out, or breaks a rule that covers a whole service it has never heard of. People have the same problem, but they can at least ask whoever has been around longest. Agents guess.

Hippomem keeps that reasoning and returns it at the moment someone is about to decide the same thing again.

## What it is not

- **Not tied to one repo.** It holds a team's memory across all its repos and services. (The markdown proof of concept was per-repo; see [History](#history).)
- **Not a record of people decisions.** Hiring, team size and org structure are not Hippomem records. They can appear only as *constraints* behind an engineering decision (see below).
- **Not business rules.** What the business must and must not do is Hippokampus's job. The two stay separate for now and share what they learn.
- **Not a place where agents decide what's true.** Agents propose. Acceptance follows the rules in [Who accepts](#who-accepts).

## What counts as an engineering decision

A choice about how the software is built that someone could reasonably have made differently, and that a later agent or engineer would need to know to avoid undoing it.

| In scope | Example |
|---|---|
| Code-level design choices | "Rate limiting uses Postgres advisory locks" |
| Module- and service-wide rules | "All billing writes go through the billing service; nothing else touches those tables" |
| Architecture choices | "Services talk through events, never by reading each other's database" |
| Rejected options and dead ends | "Kafka spiked and dropped: ops cost too high at ~1k jobs/day" |

| Out of scope | Why |
|---|---|
| People and org decisions ("no new services until 10 engineers") | Not engineering. Recorded only as a constraint on the decisions they shape |
| Business rules | Hippokampus |
| Trivia (naming, formatting) | Nobody would need it to avoid undoing work |

A constraint from outside engineering looks like this:

```
DECISION    Stay a modular monolith  ·  scope: team
Why:        one deploy pipeline, shared DB, no service-to-service ops
Constraint: team is 5 engineers (org) — revisit when that changes
Rejected:   split billing into its own service — ops cost too high for team size
```

## When it pays off

**For agents doing the work**

1. **Not repeating a settled argument.** An agent reaches for Redis; Hippomem returns the accepted decision to use Postgres-backed caching and why Redis was rejected.
2. **Not walking into a known dead end.** An agent starts adding Kafka; Hippomem returns the explored record and its "revisit if" condition.
3. **Respecting rules that cover a whole service.** An agent in the reporting service queries billing tables directly; the before-edit lookup flags the billing-owns-money-writes decision. No code links those two places, so nothing else would catch it.
4. **Picking up where earlier sessions left off.** Session 4 on a branch starts knowing what sessions 1–3 chose and dropped.
5. **Seeing conflicts between branches.** Two branches moving the same thing in opposite directions see each other's drafts before either PR opens.

**For review**

6. **Checking a PR against what's been decided.** A reviewer agent asks whether the diff contradicts, supersedes or depends on an accepted decision.
7. **Knowing what a change will affect.** Following the links shows every downstream decision before someone changes something load-bearing.

**For people**

8. **Onboarding.** "Why is the billing service built like this?" returns the decisions, rejected options and constraints.
9. **Revisiting old decisions.** When a constraint changes, the team sees which decisions rested on it.
10. **An audit trail.** Each decision records who made it (person or agent), what it rested on, who accepted it and what it replaced.

The pilot targets 1, 2, 4 and 6: they're the easiest to measure with and without Hippomem, and they don't need a backlog of existing decisions.

## How decisions get in

### Decisions are drafted during work, confirmed later

Decisions are made while agents and engineers work, not at PR time, and they churn: decide Redis, drop it, try in-memory, settle on advisory locks. If every one went straight into memory as a decision, the store would fill with noise and contradictions.

So work produces **drafts**, and something later confirms which drafts became real:

```
during work (branch)          at merge                       after
────────────────────          ────────                       ─────
draft: Redis lock     ──✗──►  rejected alternative on   ──►
draft: in-memory      ──✗──►  the final decision, with
                              "tried, dropped because…"
draft: PG advisory    ──✓──►  proposal, evidence =      ──►  accepted → in force
       lock                   the merged commits
```

### Two ways a decision is confirmed

| Kind | Example | Confirmed by |
|---|---|---|
| **Tied to code** | "Rate limiting uses Postgres advisory locks" | The merge shows it was built; then it's accepted |
| **Covers a scope** (module, service, team) | "Billing owns all money writes" | An engineer confirms it directly. Many decisions never touch a diff |

For a scope decision, the agent asks while the decision is being made ("Record this as a service-wide decision for billing?"). If nobody answers, it goes to the review queue.

### Implemented is not accepted

**Implemented** means it's in the code; the merge proves that. **Accepted** means the team stands behind it as the way to do things; review decides that. A hack that got merged is implemented and should never become accepted.

## Branches

Hippomem copies git's model. The **branch**, not the session, is the unit that carries work in progress: one branch often spans several sessions and agents.

```
main memory            = accepted decisions (what the team stands behind)
                           ▲ merge: reconcile → proposals → accepted
branch feat/rate-limit = draft trail layered over main
                           │ closed or stale: keep the lessons, drop the rest
                           ▼
                         explored records
```

1. **Every git branch gets a draft trail** the first time an agent works on it: timestamped entries like *decided X*, *dropped Y because Z*, *considering W*.
2. **An agent on a branch sees main plus its own branch.** A new session's digest says what earlier sessions on the branch chose and dropped. This pays off before anything merges.
3. **Other branches show up as "in progress", never as decided.** An agent working on the same service is warned when another branch is considering a conflicting approach.
4. **Merge reconciles.** Drafts reflected in the diff become proposals with the commits as evidence. Reversed drafts become rejected alternatives on the decision that won. The rest are dropped.
5. **A branch closed without merging is sorted.** A branch counts as closed when its PR closes unmerged, the branch is deleted, or it has had no activity for a set time (30 days to start). One pass asks of each draft: a lasting lesson, or noise? Noise is dropped; lasting dead ends become explored records.

### Explored records

```
EXPLORED  ·  Kafka for the job queue  ·  billing service
Tried:      2-day spike on feat/kafka-queue (closed 2026-10-12)
Outcome:    dropped
Why:        ops cost of running brokers > benefit at ~1k jobs/day;
            the ordering guarantee we wanted needed a single partition anyway
Revisit if: job volume > ~100k/day, or we adopt a managed broker
Evidence:   branch and session links
```

- **Never a decision.** Marked as explored wherever it appears, so no agent treats it as a rule.
- **Returned when someone is about to try the same thing**, on a query or when a branch's drafts start to resemble it.
- **"Revisit if" is required.** A dead end is usually true *for now*. The record says what would make it worth trying again, so it warns without blocking a good idea forever.
- **Can be promoted.** An engineer can turn it into a confirmed decision ("no Kafka until volume is above X").

## Capture never waits for a session to end

Sessions don't end. They do their work, go idle and are forgotten. Capture hangs off things that actually happen:

**At the moment of deciding.** The agent calls a `draft` tool when it decides or reverses something; the server instructions and session digest tell it to. Claude Code's Stop hook fires at the end of every *turn*, and a cheap check there catches decisions the agent didn't draft.

**On git events.**

| Event | What Hippomem does |
|---|---|
| Commit | Marks which drafts are now backed by code |
| Push / PR opened | Shows the branch's drafts to other branches as "in progress" |
| Merge | Reconciles drafts into proposals and rejected alternatives |
| PR closed, branch deleted, or stale | Sorts the trail into explored records and noise |

**The review queue** collects anything that needs a person: unanswered scope decisions and proposals that need an engineer.

Hippomem does not read session transcripts. A background job that swept idle sessions for undrafted decisions was considered and left out for now; if the pilot shows agents miss many decisions, the fix goes into in-the-moment capture first.

## Building the memory as we go

Hippomem starts empty. We don't mine past PRs for decisions: a PR shows *what* changed, sometimes part of *why*, and almost never the options that were rejected. Decisions extracted from PRs would come with made-up reasons, and agents would trust them. That is worse than an empty store. We don't mine old agent transcripts either.

Instead, older code gets explained **when someone is about to change it**:

1. An agent is about to change something load-bearing that no decision covers.
2. Hippomem gives it the context: who wrote the code, the PR, the date and the diff.
3. The agent asks that engineer: *"Why advisory locks here? Was anything else tried?"*
4. The answer becomes a proposal, with that engineer as its source, and goes through acceptance like any other.

Only code that is actually being changed gets explained, so nobody is asked to document the whole codebase up front. The memory grows where the work is.

## Systems

Decisions attach to **systems**, not repos. A system is something the team owns and reasons about as a unit: a service, a module, a shared library, or a piece of infrastructure like the queue or the database. Repos don't line up neatly with systems:

| Situation | Example | Mapping |
|---|---|---|
| One repo, one system | The `sentinel` repo is the Sentinel service | `sentinel/**` → Sentinel |
| One repo, many systems (monorepo) | A product repo holds web, API and billing | `product/apps/web/**` → Web, `product/services/billing/**` → Billing |
| **One system, many repos** | Billing has an API repo and a worker repo | both repos → Billing |
| Shared code | A UI kit used by three apps | `ui-kit/**` → UI Kit; the apps are linked as *depends on* it |

One system spanning many repos is the common case for a team, and the main reason Hippomem isn't repo-based: "billing owns all money writes" has to apply in every repo that touches billing.

The mapping is a list of rows: **repo + path pattern → system**. It drives:

- **Session-start digest:** which decisions to load for the repo the agent is working in.
- **Before-edit lookup:** a file under a billing path brings up billing-wide decisions.
- **Scope decisions:** a decision attached to Billing applies in every repo mapped to it.
- **Review routing:** proposals for Billing go first to engineers who work on Billing.

An agent drafts the mapping from a repo's folders, deploy configs and READMEs; an engineer confirms it. (Hippokampus's context map works the same way: drafted, then confirmed.) The mapping lives in Hippomem, not in each repo. A path that isn't mapped yet falls back to the whole repo as its scope until someone maps it.

## Who accepts

Engineers and agents can both review. Who can *accept* depends on risk:

| Proposal | Can be accepted by |
|---|---|
| Adds evidence or a link to an already-accepted decision | Any reviewer agent, or an auto-accept rule |
| A new decision of ordinary weight | A reviewer agent or an engineer |
| An architectural or heavy decision, or one that supersedes or contradicts an existing decision | An engineer |

In every case:

- **No approving your own proposal.** The agent that wrote it can't accept it, and neither can another instance of it in the same session.
- **The record keeps who accepted it**, and whether that was a person or an agent.
- **Engineers can overturn agents.** An agent's acceptance stays open to challenge.

### How proposals reach reviewers

There is no single reviewer. **Any agent using Hippomem can bring pending proposals to its engineer for approval**, even proposals unrelated to the work at hand. That spreads review across the team instead of loading one person. To keep it from becoming interruption spam:

- **Ask at natural pauses:** the start of a session or after a task finishes, never mid-work.
- **Cap it:** one or two pending proposals per session.
- **Route by relevance:** first proposals in systems this engineer works on, then ones citing code they wrote, then anything.
- **Exception:** a pending proposal that affects what the agent is about to do is raised immediately.
- **No approving your own work:** the engineer who drove the agent that wrote a heavy proposal can't accept it; someone else does.

Dedicated reviewer agents (such as Watchman or Sentinel at PR time) can also accept what the rules above allow.

## How agents read it

Reading is triggered, not left to the agent's judgment:

1. **Server instructions** tell any MCP host when to query and when to draft.
2. **Session-start digest** maps the repo to its systems and injects the decisions in force for them, plus this branch's draft trail.
3. **Before-edit lookup** matches the file being edited against decision anchors (`repo + path glob`) and injects the decisions that cover it. Anchors are cached at session start so the lookup needs no network or model call.

Retrieval keeps what the proof of concept got right: similarity search with a relevance floor, then expansion along the decision graph in both directions (what a decision depends on, and what depends on it), with superseded decisions ranked lower and flagged rather than hidden.

## Plan

We prove the concept on our own machines first. The end goal is a remote memory the whole team's agents share; deployment comes after the proof.

**1. Reframe.** This document. Rename the package, CLI and README from Hippocampus to Hippomem.

**2. Model and store.** A fresh schema: system, repo mapping, branch, draft, decision, version, link, rejected alternative, explored record, constraint, evidence, proposal. Move search and link-following onto it. No import of the proof of concept's markdown records, and no backfill from past PRs.

**3. One server for many repos.** A single Hippomem server on our machine serving MCP over HTTP. Repos map to systems. Tools for query, draft, propose, chain, list and review. Server instructions.

**4. Capture and autonomy.** The `draft` tool and per-turn check, git-event handling (commit, push, merge, close), the session-start digest and before-edit lookup, asking the author when uncovered code is about to change, proposals surfaced to engineers at natural pauses, and a minimal review queue (a CLI or small local page).

**5. Pilot.** Run it for a couple of weeks on 2–3 real repos that share decisions. Measure:
- how often agents query before making a decision
- contradictions caught before merge
- proposal acceptance rate (a low rate means agents are writing noise)
- cold-read understanding across several repos
- the same tasks with and without Hippomem

**6. Go remote**, only if the pilot shows it's worth it: a shared store, authentication, hosting, and accounts per team and per agent.

### Stop or rethink if

- agents still ignore it even with the hooks
- most proposals get rejected
- the review queue takes more engineer time than the memory saves

## Open questions

1. Which repos does the pilot run on? Ideally two or more that share a system, to test one system across many repos.
2. How long until an inactive branch counts as stale? (30 days to start.)
3. How many pending proposals may an agent raise per session before it becomes noise? (One or two to start.)

## Decisions so far

| Date | Decision |
|---|---|
| 2026-10-26 | Hippocampus renamed Hippomem; focus moves from one repo to a team's engineering memory |
| 2026-10-26 | Markdown proof of concept kept on branch `markdown-poc`, tag `v0.1-markdown-poc`; its records are not imported |
| 2026-10-26 | Prove the concept locally first; remote team deployment comes after the pilot |
| 2026-10-26 | Hippomem stays separate from Hippokampus; they share learnings only |
| 2026-10-26 | Architecture choices made with coding agents are in scope, not only code-traceable decisions |
| 2026-10-26 | Decisions are drafted during work and confirmed at merge; reversed drafts become rejected alternatives |
| 2026-10-26 | Module-, service- and team-wide decisions are confirmed directly by an engineer |
| 2026-10-26 | Capture never depends on a session ending; drafting in the moment, per-turn checks and git events drive it |
| 2026-10-26 | People and org decisions are out of scope except as constraints |
| 2026-10-28 | No backfill from past PRs or old transcripts; older code is explained by its author when someone is about to change it |
| 2026-10-28 | Any agent using Hippomem can bring pending proposals to its engineer for approval, within interruption limits |
| 2026-10-28 | No transcript sweeper for now; Hippomem does not read session transcripts |
| 2026-10-28 | Decisions attach to systems mapped from repo + path patterns; one system can span many repos; agents draft the mapping, engineers confirm |

## History

Hippomem started as **Hippocampus**, a per-repo MCP server storing decision records as markdown under `.decisions/records/`. That proof of concept showed agents would query and write decision records, and that a fresh agent could understand a codebase's architecture from the records alone. Its final state is on branch `markdown-poc` (tag `v0.1-markdown-poc`). Research and validation runs: [hippocampus-research](https://github.com/z10-labs/hippocampus-research), [hippocampus-validation](https://github.com/z10-labs/hippocampus-validation).
