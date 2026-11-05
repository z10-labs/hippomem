# Hippomem research plan

Written 2026-11-05. This is how Claude will run the research behind [plan.md](plan.md) on its own: what it measures, the test projects, the agents it runs, the repeated runs, the hooks, and when it stops to ask. [build-decisions.md](build-decisions.md) records each product change the research leads to; `docs/research/` records the results.

## The aim, as things we can measure

Hippomem should give engineers and agents facts about why the system is the way it is that are:

| Property | What we measure | Pass bar (to start; revised only with a logged reason) |
|---|---|---|
| **Factual** | Of the claims the memory holds, how many match what the team actually decided, including the reason and the rejected options | ≥ 90% of accepted decisions correct; 0 invented decisions after review |
| **Complete enough** | Of the decisions the team actually made, how many reached the memory | ≥ 80% of heavy decisions, ≥ 60% of standard |
| **Not stale** | After a decision is reversed or a constraint stops being true, how long until the memory says so, and whether agents act on the old fact | 100% of reversals marked by the merge that made them; agents act on a superseded fact in < 5% of probe runs |
| **Versioned** | "What did we believe on date T?" answered correctly | ≥ 90% of point-in-time questions |
| **Important** | How much of what reaches engineers is worth their time | ≥ 70% of proposals accepted; ≤ 1 nudge per 3 turns |
| **Timely** | The relevant decision is in the agent's context **before** the edit or choice it bears on, not after | ≥ 85% of probe runs |
| **Useful** | Agents with Hippomem make choices consistent with the team's decisions more often than without it; a new agent can explain the system | Conformance gap ≥ 30 points over the baseline; cold-read score ≥ 80% |

These bars are a starting point. Each experiment's report says whether it met them, and any change to a bar is logged with its reason before the next run, never after seeing the numbers.

## Test projects

### T1: Meridian, a fictional team (controlled, known truth)

A small payments company that Claude builds for this research:

- **Repos:** `ledger-api` (Python), `ledger-worker` (Python), `storefront` (TypeScript), `ui-kit` (TypeScript), `infra` (Terraform and YAML). Systems: billing, payouts, storefront, ui-kit, platform. Billing spans two repos.
- **People:** four simulated engineers with different ownership (Alex billing, Sam storefront, Priya platform, Jo payouts) and one reviewer agent.
- **The oracle:** a timeline file (`research/meridian/timeline.yaml`) of about 40 decisions over 12 simulated weeks, written before any run. Each entry has the decision, why, rejected options, constraints, scope, the week it was made, and what later reverses or supersedes it. It includes:
  - ordinary code decisions (a library, a schema shape)
  - system-wide and team-wide rules (no direct reads of another service's database)
  - **reversals** mid-branch and across sessions
  - **supersessions** weeks later, with the reason
  - **constraints that stop being true** (a vendor rate limit lifted, a compliance rule changed)
  - **abandoned spikes** that should become explored records
  - an **incident** that changes a decision
  - **decoys:** changes that look like decisions but aren't (renames, formatting, a bumped patch version)
- **Time:** simulated. Commits carry the timeline's dates (`GIT_AUTHOR_DATE`, `GIT_COMMITTER_DATE`) and the server's clock is set per event, so staleness rules and point-in-time questions work on simulated weeks.

The oracle is the answer key. Agents never see it. The simulated engineers see only what a real engineer would know at that moment: the task for the day, and what they want, in plain words.

### T2: replaying a real open-source project (real, messy truth)

A project with dated architecture decision records (ADRs), some of them superseded, and a readable history. Claude picks it in R4 against these criteria and logs the choice:

- 15 or more ADRs with dates and statuses, at least 3 superseded
- the PRs that implemented them can be found
- small enough to replay (a few hundred merges in the window)
- permissive licence

Candidates to check: Backstage, and others found by searching GitHub for `docs/adr` or `architecture-decisions` folders.

**How the replay works:** walk the history in order. Feed each merge (diff, commit messages, PR description) through the real git-event and reconciliation path, **with the ADR files removed from the diff**, so the memory has to learn the decisions from the work, as it would on a team without ADRs. The ADRs are the answer key. Probe tasks and the cold-read run against snapshots at chosen dates.

T2 tests what T1 can't: whether the judge can read real, messy history, and whether facts it derives match what the project actually decided.

## The agents

| Role | How it runs | Model |
|---|---|---|
| **Coding agent** | `claude -p` in a sandbox clone, with Hippomem's MCP server and hooks, `--setting-sources project --strict-mcp-config` (none of the owner's settings) | Sonnet (what teams mostly run) |
| **Simulated engineer** | Not an agent. A script turns the timeline into the engineer's prompt for the day, and answers questions, proposals and "ask the author" from the oracle, the way the real person would | none |
| **Reviewer agent** | `claude -p` with the reviewer token, accepting or rejecting standard proposals | Sonnet |
| **Judge** | Hippomem's own merge reconciliation (`hippomem.judge`) | as configured |
| **Grader** | Scores free-text answers (a "why", an explanation) against the oracle with a fixed rubric | Opus, a different model from the agents, to reduce self-preference |
| **Orchestrator** | Claude, in this session and later ones: runs experiments, reads results, fixes the product, reruns | |

**Scoring:** wherever an answer can be checked by code, it is: decision IDs, option names, yes/no, dates. The grader is used only for reasons and explanations, and Claude spot-checks one in five of its grades by hand, logging any disagreement.

**Measuring before and after review:** the simulated engineer accepts proposals that match the oracle, so the memory's accuracy after review partly reflects the oracle. Every experiment therefore reports accuracy **before review** (what the judge and agents proposed) and **after review** (what got in) separately. The first is the honest measure of the capture pipeline.

## Hooks and instruments

In every sandbox repo:

- Hippomem's own Claude Code hooks (SessionStart, UserPromptSubmit, PreToolUse before edits, Stop) and git hooks (post-checkout, post-commit, post-merge), installed by `hippomem install`, exactly as a team would.
- **An observation hook** (harness only, not product): PostToolUse and PreToolUse write every tool call with a timestamp to `events.jsonl`. This is what timeliness is measured from: when the relevant decision first entered the agent's context, and when the agent made the edit or choice it bears on.
- **Query logging** in the product's `usage` table: every query and lookup records its result set (IDs and scores), so retrieval quality can be scored offline and reused against later versions without new agent runs.
- **Cost ledger:** every agent and grader call appends its cost to `research/ledger.csv`. The harness refuses to start a run that would push the total past the budget.

## Experiments

Each experiment has a written hypothesis, a fixed procedure and its metric before it runs. Agent behaviour varies, so every comparison runs **at least 3 times per arm** and reports the spread, not one example.

| # | Question | Procedure | Main metric |
|---|---|---|---|
| **E0** | How noisy are single runs? | Rerun the existing scenario harness (S1–S9) 3 times | Check pass rate and spread per scenario |
| **E1** | Does the memory capture what the team decided, correctly? | Run Meridian's 12 weeks end to end; compare the memory with the oracle | Decision recall and precision; correct "why"; alternatives recall; invented facts (before and after review) |
| **E2** | Does it stay current? | The reversals, supersessions and lapsed constraints in the timeline; then probe tasks that touch them | Reversals marked correctly and when; probe runs acting on a stale fact |
| **E3** | Is it versioned correctly? | Point-in-time questions at 4 checkpoints (weeks 3, 6, 9, 12) | Correct answers as of T |
| **E4** | Does it change what agents build? | 10 probe tasks in areas with decisions, with and without Hippomem, 3 runs each; plus 3 decoy tasks where memory should **not** change anything | Conformance with the team's decision; correct citations; wrong influence on decoys |
| **E5** | Does the right fact arrive in time? | From E4's runs and the observation hook | Share of runs where the decision was in context before the first conflicting edit; precision of the digest and lookup; tokens added per session |
| **E6** | Is it worth engineers' time? | From E1's run | Proposals per merged branch; acceptance; nudges per turn; nudges ignored; questions asked of authors and answered |
| **E7** | Can a new agent understand the system from it? | A fresh agent answers the query set three ways: memory only, repo only, both | Score against the oracle |
| **E8** | Does it hold up on real history? | T2 replay; E1, E2 and E7 measures | The same metrics, on real truth |
| **E9** | Do the graph operations answer the graph questions? | After the schema change: lineage, stale dependents, blast radius, contradictions, why | Query-set score for the graph questions |

### The query set

About 30 questions written **before** the first run, each with an expected answer from the oracle. A third of them are **held out**: never looked at while fixing the product, and used only to confirm a fix generalises. Kinds of question:

- why did we choose X; what did we reject and why
- what is in force for this file or system now
- what did we believe on date T
- what rests on this constraint; what changed after this incident
- what contradicts this; what would this change touch
- which decisions are stale

## Phases

| Phase | What happens | Cost (estimate) |
|---|---|---|
| **R0 Set up** | Merge PRs 15 to 18. Research branch. Apply the agreed revisions to plan.md. Build Meridian (repos, timeline, query set, held-out split), the simulated engineer, the observation hook, query logging, the cost ledger, the grader and the metrics report. Unit tests for the harness itself | ~$3 (dry runs) |
| **R1 Baseline** | E0, then E1 to E7 on the current product, unchanged. These numbers are what every later change is compared with | ~$45 |
| **R2 Schema change** | The minimal C3 from plan.md: shared constraints; alternatives, constraints and anchors versioned with the decision; links between any records, with who or what derived them and a confidence; `valid_to`. Then the C4 graph operations and judge-suggested links. Each choice gets a BD row | ~$2 |
| **R3 Re-measure** | E1 to E7 and E9 again on the same timeline and query set. Then the schema is frozen | ~$45 |
| **R4 Real history** | Choose the open-source project, build the replay, run E8 | ~$25 |
| **R5 Fix and confirm** | Fix what R3 and R4 show is failing, one change at a time, each rerunning only the experiments it touches, then a final check on the held-out questions | ~$25 (reserve) |
| **R6 Engine check** | Load the same data into Postgres with recursive queries and pgvector and run the query set. Graph engines are evaluated only for questions Postgres fails | ~$0 (no agents) |

Total about $145 of the $150.

Order matters. The baseline (R1) runs before any product change, so every improvement is measured against the product as it is today. Schema changes happen in R2, before the large re-measure, and not during it.

## Running it unattended

- **Where things live:** harness code in `research/` in the repo; sandbox repos and raw transcripts in a scratch folder outside the repo (large, and not worth committing); results in `docs/research/E*.md` with the numbers and a few quoted examples; a dated `docs/research/log.md` of what was run, what was found, and what changed.
- **Long runs** go in the background. Claude is notified when they end and does not poll.
- **Every product change** is a commit with a BD row, merged through its own PR, so the history of fixes lines up with the history of results.
- **Reporting to the owner:** a brain inbox note at the end of each phase with the headline numbers; the brain task log for everything else.

## When Claude stops and asks

- Spend reaches $100, or a phase is going to cost more than 1.5× its estimate
- A fix would change who may accept what (the acceptance rules), or what agents are allowed to do
- A result contradicts the vision in [hippomem.md](hippomem.md), not just the implementation: for example, the memory only works if engineers review far more than they would tolerate
- Anything would be published outside this repo, or would touch the owner's real repos
- One of the [stop conditions](plan.md#stop-or-rethink-if) is met

## What this can and can't show

It can show whether the mechanisms produce accurate, current, versioned facts on a controlled project and on real history, and whether agents use those facts at the right moment. It can't show whether a real team will tolerate the prompts, review the proposals carefully, or keep using it for months. Simulated engineers answer instantly and always correctly; real ones don't. That stays for the pilot on the owner's repos, after this.
