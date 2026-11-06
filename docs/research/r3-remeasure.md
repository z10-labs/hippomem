# R3: the fixed product, measured again

Run 2026-11-06 on main after PR 21 (BD-43 to BD-49), with the same answer key, steps, probes and queries as [R1](r1-baseline.md), on a fresh Meridian. Harness changes made before R3 ran are in [log.md](log.md) (grader split, engineer go-ahead, rewritten decoys, M24 excluded); R1's proposals were regraded with the same grader so the columns compare. Spend: R3 $39.42; **total $95.55 of $150**.

## Against the aim

| Property | Pass bar | R1 | R3 | |
|---|---|---|---|---|
| **Factual** | ≥ 90% of accepted decisions correct; none invented | 22/25 match the key; 3 true but trivial; 0 contradictions | **23/23** match the key; 1 stale phrase inside a correct proposal | **Pass** |
| **Complete enough** | ≥ 80% heavy, ≥ 60% standard | heavy 6/8, standard 16/17 | **heavy 8/8**, standard 15/17 | **Pass** |
| **Not stale** | every reversal marked by its merge; agents act on a stale fact < 5% | 0 of 2 supersessions recorded; replaced decisions stayed in force | **3 of 3** recorded with dates and links; **but 1 still-true decision wrongly taken out of force**. Stale-fact failures in probes: 0/12 with memory, 1/12 without | **Partly**: the main failure is fixed, a new one appeared |
| **Versioned** (as-of questions) | ≥ 90% | 100% | 100% (memory), 89% (repos alone) | **Pass** |
| **Important** | ≥ 70% of proposals accepted; ≤ 1 nudge per 3 turns | 53%; 9 of 37 turns | **88%** (23/26); 8 of 40 turns; 1.0 proposal per merge | **Pass** |
| **Timely** | ≥ 85% | 24/24 | **27/27** | **Pass** |
| **Useful** | conformance gap ≥ 30 points; cold read ≥ 80% | 90% vs 60% (+30); cold read memory 78%, both 84% | 90% vs 70% (**+20**); cold read memory **87%**, both **89%**, 0 wrong claims | **Cold read passes; conformance gap below the bar** |

## What changed, and why

- **Noise fell by half.** 26 proposals instead of 47, every one of them a real answer-key decision, every one stating it correctly. No inferred proposals got through the new bar (BD-44). Acceptance went from 53% to 88%.
- **Supersessions are recorded.** The Postgres-only rule (M03), the Postgres rate-limit counters (M07) and the ECB rates (M18) were each replaced by a supersession, accepted, dated and linked. `hippomem_get` shows the lineage.
- **The sole-owner deadlock is gone** (BD-49): the Postgres-only rule entered the memory in week 1 and left it, correctly, in week 9.
- **The memory answers better.** Cold read from the memory alone: 78% → 87%; rejected options 75% → 100%; stale questions 88% → 100%; wrong claims 3 → 0. With repos as well: 89%.
- **Ask the author works where it matters.** On P06 (half-even rounding only Jo can explain) the hint fired in 3 of 3 runs and the agent asked Jo every time; it fired nowhere else in the 30 memory-arm probe runs.

## What still fails

1. **Over-superseding (new).** The judge marked "send payouts individually" (M19) as replacing the payout idempotency keys from the incident (M16), because M16's wording mentioned bulk payouts. M16 is still true, and accepting M19 took it out of force. Nobody checked the *target* of a supersession: the grader and the simulated engineers judge what a proposal says, not what it removes. A real reviewer could miss it the same way.
2. **Asking isn't waiting.** On P06 the agent asked Jo and then changed the rounding anyway, because agents are told not to wait for answers (BD-35). The reason will reach the memory when Jo answers, after the code has changed.
3. **The conformance gap narrowed to 20 points**, below the 30-point bar. The memory arm held at 90%; the no-memory arm rose from 60% to 70%. R3's agents built a different Meridian, and on P02, P03 and P04 its code steered agents without memory better. With 3 runs per arm, 10 points is one probe; treat the gap as 20–30.
4. **Probes mostly end in "asked".** As in R1, agents with the memory stop to ask when the engineer's request contradicts a decision. On P03 that costs a task the no-memory agent completed correctly (3/3 followed vs 1/3).

## What R5 should change

| Failure | Change |
|---|---|
| 1 Over-superseding | The judge says `replaces` only when the old decision's statement becomes false, and `amends` when it stays true but its wording is out of date (amends links the two and keeps the old one in force). The proposal shows reviewers, in plain words, which decision will leave force |
| 2 Asking isn't waiting | Owner's call: for changes to *how settled code behaves*, the agent asks the author and stops that change until answered, carrying on with the rest |
| 3, 4 | Measurement, not product: more probe runs per arm in R5, and a probe set where the engineer's request doesn't itself contradict the decision |
