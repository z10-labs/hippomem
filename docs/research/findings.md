# What the research found

The research behind [plan.md](../plan.md), run 2026-11-05 to 06 under [research-plan.md](../research-plan.md). Phase reports: [R1 baseline](r1-baseline.md), [R3 re-measure](r3-remeasure.md); every run, check, correction and harness change: [log.md](log.md). Spend: **$142.36 of $150** at API list prices. On the owner's claude.ai plan this was usage, not a bill (log, 2026-11-06).

## The answer

Hippomem's aim is an engineering memory that is factual, current, versioned, worth reading and on time. On a controlled team with a written answer key, **after two rounds of fixes it now meets every pass bar but one** (the gap between agents with and without it). On real history it recovers about 60% of a project's recorded architecture decisions from merges alone, plus real decisions nobody wrote down, but with thinner reasons.

| Property | Pass bar | R1 (as it was) | R5 (as it is now) |
|---|---|---|---|
| **Factual** | ≥ 90% of accepted decisions correct; none invented | 22/25 correct, 0 contradictions | **24/25 correct**; 1 wrong detail about a past decision inside a correct proposal |
| **Complete enough** | ≥ 80% heavy, ≥ 60% standard | heavy 6/8, standard 16/17 | **heavy 8/8, standard 16/17** (96% overall) |
| **Not stale** | every replacement recorded; nothing still true taken out of force | **0 of 2** replacements recorded; replaced decisions stayed in force | **4 of 4** recorded with dates and links; **0** wrongly taken out of force |
| **Versioned** | ≥ 90% on "what was true on date T" | 100% | 100% |
| **Worth reading** | ≥ 70% of proposals accepted | 53% (47 proposals, 22 of them noise) | **93%** (27 proposals, one per merge) |
| **On time** | ≥ 85% of runs: the decision is in context before the edit | 24/24 | **27/27** (R3), 6/6 (R5) |
| **Useful** | conformance gap ≥ 30 points; cold read ≥ 80% | +30 points; cold read 78% from memory | **+20 points** (R3); cold read **88%** from memory, **91%** with repos, 0 wrong claims |

## What changed between R1 and R5, and why

Each change came from a failure a run showed, and each was checked by a later run.

| Found | Change | Result |
|---|---|---|
| Replaced decisions stayed in force beside their replacements (R1) | Agents can say what a decision replaces; the merge judge compares every built draft with the decisions in force and proposes a supersession, which takes the old one out of force with a date and a link; `hippomem_get` shows the lineage (BD-43, BD-45) | 0/2 → 3/3 (R3) → 4/4 (R5) |
| The fix then over-superseded: a decision that was still true was taken out of force (R3) | `replaces` only when the old decision stops being true; `amends` links without removing; reviewers see in words what leaves force (BD-50) | 1 → 0 wrongly removed (R5) |
| Half the proposals were noise, mostly decisions "inferred" from history with no written reason (R1) | Inference needs a choice with a written reason, nothing already drafted or in force, max 2 per merge (BD-44) | 47 → 26 proposals; acceptance 53% → 88% → 93% |
| A sole owner's team-wide rule could never be accepted (R1) | Owner's call: any other engineer may accept it (BD-49) | the Postgres-only rule entered the memory in week 1 (R3, R5) |
| "Ask the author" never reached long-settled code; then agents asked and changed it anyway (R1, R3) | The hint also covers code untouched for 90 days; owner's call: hold the behaviour change until the author answers (BD-47, BD-50) | P06: 0/3 → asked 3/3 (R3) → asked and waited 3/3 (R5) |
| Team-wide rules were repeated before every edit (R1) | They stay in the session digest (BD-48) | less repeated context per edit |

## Real history: GOV.UK's `govuk-aws` (R4)

All 267 merges from July to mid-September 2017 went through Hippomem's real merge reconciliation and judge. The 30 ADRs written in that period were removed from every diff and history and used, as written, as the answer key. There were no agents and no drafts, so this measures the weakest path: what the memory learns from merges alone.

- **96 proposals from 267 merges** (0.36 per merge). 69 of them (72%) were real decisions another engineer could have made differently; 27 were implementation details.
- **18 of the 29 in-window ADRs recovered** (62%) with the decision stated correctly. The misses are mostly process decisions with no code (record ADRs, migration strategy, pending ADRs) and changes that landed after the window.
- **30 real decisions that no ADR records**, such as a 1 GB `max_allowed_packet` so large restores don't fail, or EC2 instance ids as Puppet certificate names.
- **Reasons are thin:** of the 42 proposals matching an ADR, the reason was right in 11, partly right in 24. Commit messages carry less of the "why" than an ADR. This is the case for drafting while working rather than reconstructing at merge.
- **6 proposals (6%) contradicted an ADR**, some where the code had drifted from what the ADR said.
- **Not tested here:** supersession. Nothing was accepted during the replay, so the judge never had decisions in force to compare against.

## What still doesn't work, or isn't known

1. **The conformance gap is 20–30 points, not reliably 30.** With the memory, agents conformed 90% of the time in both R1 and R3. Without it, 60% in R1 and 70% in R3, because the R3 agents built a Meridian whose code steered better on its own. Three runs per arm puts one probe at 10 points.
2. **Agents with the memory stop and ask a lot.** When an engineer's request contradicts a decision, that's right. On P03, though, the agent without the memory just did the right thing, while the agent with it stopped to ask.
3. **Held-out questions score lower** (73% from memory vs 95% on the rest). Two of the held-out ten are about legacy reasons nobody asked for during the timeline, and a "what depends on this" question remains weak (61% from memory). The memory answers what was recorded; it doesn't yet infer how decisions connect.
4. **Reasons only the author knows reach the memory only when someone asks.** No cold-read question about them scored above 0, because no step of the timeline touched those files.
5. **Simulated engineers aren't real ones.** They answer instantly, accept what matches the answer key, and say "go ahead" when asked. Whether a real team tolerates the review load, about one proposal per merged branch and a question or two a week, is what the pilot must show.
6. **One timeline run per phase.** E1 numbers are single samples (R3 and R5 agree closely, which helps); probes and cold reads ran 2–3 times per arm.

## Recommendation

The product is ready for the pilot on the owner's repos. Watch three things there that this research couldn't: whether engineers review proposals carefully or wave them through; whether agents drafting during real work produce better reasons than R4's commit messages; and how often "stop and ask" helps compared with how often it gets in the way.

Before the pilot, the next product step is the "what depends on this" question: links between decisions are still made only when someone says so. Suggesting `depends-on` and `informed-by` links at merge, for review like any other proposal, was plan.md's C4. The cold read now shows where it would help.
