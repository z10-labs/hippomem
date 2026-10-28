# Scenario harness

End-to-end checks with **real headless Claude Code agents** working in a fake team. Unit tests prove the parts; this proves the loop: agents read the memory before deciding, draft as they work, merges turn drafts into proposals, and engineers' answers land in the memory.

```bash
pip install -e ".[dev]"
python -m scenarios.run --sandbox /tmp/hippomem-scenarios          # everything
python -m scenarios.run --sandbox /tmp/hippomem-scenarios --only S1  # one scenario
```

Results go to `results.md` and `results.json` in the sandbox. The sandbox is wiped at the start of each run.

## The fake team

- Engineers **@alex** (owns billing; the agents work for Alex) and **@sam** (owns storefront).
- Systems: **billing** spans two repos, `ledger-api` and `ledger-worker`; **storefront** is one repo.
- The memory starts with three accepted decisions (rate limiting in Postgres with Redis rejected; services never read each other's databases; money in integer cents) and one explored record (a Kafka spike that was dropped).
- `ledger-api/src/ledger_api/payouts.py` has three commits by Sam and no decision covering it.

## Scenarios

| | Situation | What has to happen |
|---|---|---|
| S1 | Asked to add rate limiting, nudged toward Redis | No Redis; the agent read the memory and cites the decision. Also run without Hippomem for comparison |
| S2 | Asked to publish refund events, nudged toward Kafka | No Kafka; the agent mentions the earlier spike. Compared without Hippomem |
| S3 | In the storefront repo, nudged to read the ledger database directly | No database client; goes through the ledger API. Compared without Hippomem |
| S4 | Three sessions on one branch: decide, reverse with a reason, continue; then merge | Drafts and the reversal recorded; session 3 doesn't undo the reversal; the merge is judged into a proposal with the reversed option as a rejected alternative; another engineer accepts it |
| S5 | Another engineer's branch plans to move rate limiting into the gateway | The agent mentions that plan before merging its own change |
| S6 | An abandoned spike branch is closed | The judge keeps the lesson as an explored record with a revisit condition and drops the noise |
| S7 | The agent adds a dependency | The choice ends up drafted, nudged at most once |
| S8 | Asked to change code Sam wrote that nothing documents | The lookup suggests asking Sam; the agent asks; Sam's answer becomes a proposal |
| S9 | A proposal is waiting for Alex | It's raised at session start and the agent records Alex's approval as an engineer acceptance |

## Isolation and cost

Each agent runs as `claude -p` from inside its sandbox repo with:

- `--setting-sources project`: your user settings, hooks and plugins are not loaded
- `--strict-mcp-config --mcp-config <sandbox file>`: only the sandbox Hippomem server
- `--settings <sandbox file>`: only Hippomem's hooks
- `--no-session-persistence`: no transcripts saved to your `~/.claude`
- `--max-budget-usd 1.50` per run; the harness stops starting scenarios after $30 in total

Your global `CLAUDE.md` and rules still load, as they would for your real agents. Agent config files (with the sandbox token) live outside the repos, so agents can't read or commit them.

The judge (merge reconciliation and closed-branch sorting) is also headless Claude, called by the server.

`HIPPOMEM_SCENARIO_MODEL` picks the agents' model (default `sonnet`); `HIPPOMEM_JUDGE_MODEL` the judge's.
