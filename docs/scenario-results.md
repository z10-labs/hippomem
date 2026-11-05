# Scenario results

Results of the [scenario harness](../scenarios/README.md): real headless Claude Code agents (Sonnet) working in a fake two-engineer team, against a real Hippomem server and the real judge. Run on 2026-10-28.

**Run 2 (after fixes): 28 of 29 checks passed, $1.10 of agent spend.** Run 1 found three product gaps and one harness bug; they were fixed (BD-33 to BD-35) before run 2.

## With and without Hippomem

The same prompt, in the same repo, once with Hippomem connected and once without. Each prompt nudges the agent toward something the team had already ruled out.

| Scenario | Nudged toward | With Hippomem | Without Hippomem |
|---|---|---|---|
| S1 rate limiting | Redis, which the team rejected (D-0001) | Built it in Postgres, cited D-0001, drafted the window design | Added Redis to the dependencies and code, plus a new `LEDGER_REDIS_URL` setting |
| S2 refund events | Kafka, which the team spiked and dropped (X-0001) | Didn't add Kafka; cited the earlier spike and its revisit condition, proposed a Postgres outbox, asked before building | Added Kafka to the dependencies and a producer module |
| S3 storefront refund total | Reading the ledger database directly, against a team-wide rule (D-0002) | Didn't add a database client; cited D-0002 and offered to go through the ledger API, then stopped to ask | Didn't add a database client either; stopped to ask for the schema and connection details |

S1 and S2 show the memory changing what gets built. S3 doesn't separate the two: both agents paused before connecting to another service's database. The harness counts S3's "goes through the ledger API" check as failed because the agent asked instead of writing the call. That's the one failed check in run 2, and it's a weakness of the check rather than of the product.

## Lifecycle

| Scenario | What happened in run 2 |
|---|---|
| S4 decision reversed across sessions, then merged | Session 1 drafted "store the deadline on the order". Session 2 reversed it with `replaces` and the reason ("goes stale when a delivery date is corrected"). Session 3 read the trail from the digest, kept the computed approach and said so. On merge the judge produced one proposal with the stored option as a rejected alternative; another engineer accepted it |
| S5 another branch's plan | The agent mentioned that another branch is moving rate limiting into the gateway |
| S6 abandoned spike | The judge kept "a separate cron container missed runs silently" as an explored record with a revisit condition and discarded the rename |
| S7 picking a dependency | The agent chose httpx and drafted the choice unprompted |
| S8 changing undocumented code | The lookup suggested asking Sam; the agent asked through `hippomem_ask_author` without waiting, also avoided the cron approach because of explored record X-0002, and Sam's answer became a proposal |
| S9 a proposal waiting for the engineer | Raised at session start; the agent recorded the engineer's approval as an engineer acceptance, via the agent |

## What run 1 found, and the fixes

| Found | Fix |
|---|---|
| S4: session 1 stored the deadline and committed without drafting. The end-of-turn check only watched dependency, schema and infrastructure files, so it stayed quiet, and the later reversal had nothing to replace | The check also nudges when a turn commits code without drafting, and names reversals explicitly (BD-33). In run 2, session 1 drafted after the nudge |
| S4: the merge proposal had the right reason but no rejected alternative, because the reversal was never drafted | The judge now also reads the branch's commit history, attaches reversed options it finds there, and reports decisions nobody drafted as `inferred` proposals (BD-34) |
| S8: the agent got the "ask the author" hint and skipped it | The hint now says the agent doesn't have to wait for the answer (BD-35). In run 2 it asked |
| S1–S3: the comparison runs crashed on a branch-name clash in the harness | Fixed in the harness |
| S7: the prompt named httpx, so the agent correctly saw no choice to draft | The prompt now leaves the library to the agent |

## What this does and doesn't show

- It shows the mechanics work end to end with real agents: digest, lookup, drafts, reversals, git events, the judge, proposals, acceptance, author questions, explored records.
- It shows, on two out of three tries, an agent avoiding a choice the team had already ruled out because of the memory.
- It does not show what happens on a real team with real repos over weeks: whether engineers tolerate the prompts, whether proposals are worth accepting, and whether the memory stays accurate. That's the pilot (phase 5).
- Each scenario ran once per variant. Agent behaviour varies between runs; treat single results as examples, not rates.
- The agents ran with the owner's global `CLAUDE.md` and rules loaded, as real agents would.
