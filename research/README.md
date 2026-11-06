# Research harness

Controlled experiments on whether Hippomem's memory is factual, current, versioned, important and timely. The method, the pass bars and the phases are in [docs/research-plan.md](../docs/research-plan.md); results and the running log are in [docs/research/](../docs/research/).

```bash
pip install -e ".[dev]"
export PYTHONPATH=.
python -m research.run build     --sandbox ~/.hippomem-research/r1     # the team, repos and memory, no agents
python -m research.run timeline  --sandbox ~/.hippomem-research/r1     # 12 simulated weeks (E1, E2, E6)
python -m research.run probes    --sandbox ~/.hippomem-research/r1 --reps 3   # E4, E5
python -m research.run decoys    --sandbox ~/.hippomem-research/r1 --reps 2
python -m research.run coldread  --sandbox ~/.hippomem-research/r1 --reps 3   # E7, E3
python -m research.run report    --sandbox ~/.hippomem-research/r1 --out docs/research/r1.json
python -m research.run spent
```

`--phase` labels the spend in the ledger (default `R1`). Every command checks the ledger before it starts and refuses if its estimate would pass the $150 budget, or pass $100 without `--approved` (the owner is asked first).

## Meridian

A fictional payments team: Priya (platform, admin), Alex (billing), Jo (payouts), Sam (storefront and ui-kit), and Watchman, a reviewer agent. Five repos: `ledger-api`, `ledger-worker`, `storefront`, `ui-kit`, `infra`.

| File | What it is |
|---|---|
| [meridian/timeline.toml](meridian/timeline.toml) | The answer key: 26 decisions, 3 explored records and 2 reasons only their authors know, plus the 31 steps of work over 12 weeks that produce them |
| [meridian/probes.toml](meridian/probes.toml) | 10 probe tasks nudging toward what the team ruled out (4 of them toward a fact that has gone stale) and 3 decoys |
| [meridian/queries.toml](meridian/queries.toml) | 30 questions with key points, 10 held out |
| [meridian/world.py](meridian/world.py) | The team config and the repos' starting code |
| [meridian/simulate.py](meridian/simulate.py) | Plays the timeline and does the weekly review |

All three answer-key files were written before any run. They change only with a dated note in the log.

## How a run works

- **Agents** are headless Claude Code (`claude -p`, Sonnet) in sandbox repos, with Hippomem's real MCP server and hooks, and none of the owner's own settings (`--setting-sources project --strict-mcp-config`).
- **Simulated time:** the memory's clock follows a file (`HIPPOMEM_CLOCK_FILE`, BD-41) and git commits carry the simulated date.
- **Review** at the end of each simulated week: every open proposal is graded against the answer key first (the "before review" measure); Watchman, the reviewer agent, decides what it may; the simulated owners decide the rest from the grade. Questions to authors are answered from the answer key's legacy reasons.
- **Probes and cold reads** each run on their own full copy of the end-of-timeline sandbox, so no run sees another's work.

## Instruments

| Instrument | What it records |
|---|---|
| Ledger (`~/.hippomem-research/ledger.csv`) | Every agent, judge, reviewer and grader call's cost. The judge runs inside the server, so the server's `claude` is [claude_shim.py](claude_shim.py), which passes calls through and records their cost |
| Observer hook ([observer.py](observer.py)) | Every tool call each agent makes, with the highest memory `usage` id at that moment |
| Usage records (BD-42) | Which records each digest, lookup and query showed the agent |
| Transcripts | Each run's full `stream-json` output, in the sandbox |

Timeliness joins the last two: a decision counts as timely if a usage row showing it was written before the agent's first edit. The before-edit lookup for that same edit doesn't count.

## Grading

Code scores whatever it can: diffs against regexes, ids, statuses. The grader ([grader.py](grader.py), Opus, no tools) is used for reasons, explanations and inconclusive probe endings. One in five grades is checked by hand and logged.

## Real history: the open-source replay (R4)

[oss/replay.py](oss/replay.py) walks every merge of `alphagov/govuk-aws` from 1 July to 15 September 2017, oldest first, through Hippomem's real merge reconciliation and judge, with the ADR files (`docs/architecture/decisions/`) cut out of every diff and history. The ADRs, as written, are the answer key. No agents and no drafts: it measures what the memory recovers from merges alone.

```bash
python -m research.oss.replay adrs   --repo ~/.hippomem-research/r4/govuk-aws --out ~/.hippomem-research/r4/run
python -m research.oss.replay replay --repo ~/.hippomem-research/r4/govuk-aws --out ~/.hippomem-research/r4/run
python -m research.oss.replay grade  --repo ~/.hippomem-research/r4/govuk-aws --out ~/.hippomem-research/r4/run
```
