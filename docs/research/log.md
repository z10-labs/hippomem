# Research log

Dated entries: what was run, what was found, what changed. Method in [../research-plan.md](../research-plan.md); harness in [../../research/](../../research/README.md).

## 2026-11-05: R0 set-up

- PRs 15 to 18 merged to main after the full local check passed on PR 18's head (BD-01b).
- Plan revisions applied to plan.md (BD-04a, BD-36 to BD-40).
- Product instruments: the clock file (BD-41) and records of what each response showed the agent (BD-42).
- Harness built in `research/`: ledger and cost shim, observer hook, sandbox with per-engineer identities and simulated time, the Meridian answer key (26 decisions, 3 explored, 2 legacy reasons, 31 steps), 10 probes and 3 decoys, 30 queries (10 held out), the grader, metrics and the runner. Harness unit tests: 9, plus the product suite (117 in all).
- **Deviation from the plan, decided before any run:** E3 (versioned) asks its point-in-time questions (Q04, Q07, Q18) at the end of the timeline rather than at four checkpoints. Asking about the past from the present is the harder and more realistic case, and it costs no extra runs. Checkpoint questions can be added if the as-of answers turn out ambiguous.
- **Answer key size:** 26 decisions rather than the plan's "about 40". Every decision has a step that produces it; 40 would have meant filler.

## 2026-11-05: R0 dry run, week 1 ($1.11)

The whole loop ran end to end: two agent sessions, a merge judged into proposals, every proposal graded, Watchman reviewing, the simulated owners deciding, all spend in the ledger (agents $0.46, judge $0.06, grader $0.31, reviewer $0.27).

- M01, M02 and M03 all reached the memory correctly, each with its reason and rejected options.
- **Finding (product, kept for the baseline, not fixed):** on `feat/money-type`, where the agent had drafted M01 properly, the merge judge also produced two *inferred* proposals (BD-34): a migration caveat with no decision in it, and a partial duplicate of M01 with "Not recorded" as its reason. Watchman rejected both. Two of the three proposals from that merge were noise.
- **Harness fix:** an agent wrote "agreed in the meeting on 2026-11-05", today's real date, into a simulated week-1 record, because Claude Code gives agents the real date. Every agent prompt now ends with the simulated date.
- **Harness fixes:** the sandbox kept a new random port on reload while the server config held the old one, so the server and the agents disagreed. The port is now saved with the sandbox, and clones rewrite it in their config.

## 2026-11-05: R0 dry run, probes and cold read ($0.82)

- Probe P04 (read the ledger database directly) once per arm on the week-1 sandbox. With the memory: the agent was shown D-0002 (services never read each other's databases) and stopped to ask. Without: it went through the API anyway, because the seed code already does. Like S3 in the scenario harness, P04 may not separate the arms; that's for the baseline to show.
- Cold read, memory only: 30 answers, graded in one call. At week 1 only Q01 and Q02 are answerable, and both were fully right.
- R0 total: $1.93. Harness ready for R1.

## 2026-11-06: E0, how noisy single runs are ($8.98)

The scenario harness (S1–S9) three times, plus two reruns of S5, S7 and S8 after a harness fix.

| | Passed |
|---|---|
| S1, S2: memory changes what gets built (no Redis, no Kafka, cites the record) | 3/3 every check |
| S3: no database client, cites the rule | 3/3 |
| S3: "goes through the ledger API" | 0/3 (the agent stops to ask; the check is wrong, as in October) |
| S5: mentions the other branch's plan | 2/3 |
| S6, S7, S8, S9 | 3/3 every check |
| **S4: drafts recorded on the branch (decide, then reverse)** | **0/2** |
| **S4: reversal recorded with a reason** | **0/2** |
| S4: merge produced a proposal with the stored option rejected | 1/2 |

- **Finding:** capturing a reversal across sessions is unreliable. October's single passing S4 run was the lucky one. In the completed runs, session 1 drafted at most once and session 2's reversal was never drafted, despite the end-of-turn nudge (BD-33). The judge reading commit history (BD-34) recovered the rejected option in one run of two. Meridian's M06 and M23 measure this at scale.
- **Harness fix:** S4's agent left uncommitted files in 2 of 3 runs, and every later scenario that switched branch in that repo crashed. `scenarios/run.py` now resets each repo before each scenario.
- **Conclusion for the method:** single runs mislead in both directions. Every comparison in R1 onwards runs at least 3 times, as planned.

## 2026-11-06: R1 timeline, 12 weeks of Meridian on today's product (~$18)

37 agent sessions, 31 steps, weekly review. Numbers from `python -m research.run report`; the details below were checked by hand against the proposals, drafts and transcripts.

**Capture (E1)**

- After review, 22 of 26 answer-key decisions are in the memory (85%): heavy 6/8, standard 16/18. All 3 explored records were captured and matched.
- The two memory-driven steps worked: with no instruction, the agents built rate limiting with Postgres counters and refund events with an outbox, because the memory said Redis and Kafka were out.
- Of the four misses, **only one is the product's fault**:
  - **M03 (Postgres-only rule): product.** Priya proposed it; she is the only platform owner and the only admin. Self-approval is forbidden, and a team-wide proposal needs an owner of its system, so nobody could ever accept it. It sat open for nine weeks until Watchman rejected it as outdated. *An owner who is the only owner can't get their own team-wide decision accepted.*
  - **M05 and M26: simulation limit.** The agent stopped to ask (for a spec it couldn't reach; because invoices never actually called the week-7 conversion code) and nobody answered, so nothing was built or merged.
  - **M24: answer-key error.** Watchman rejected it, correctly: under M04 as built, a 409 means the key was reused with a *different* body, so "409 means already processed" is wrong. The key is inconsistent.
- Before review: 56 proposal grades over 47 proposals (one proposal stayed open for nine weeks); 42 matched an answer-key record, 38 stated it correctly, reasons were correct or partly correct in 40 of them.

**Noise (E6)**

- 47 proposals for 23 merged branches (2.0 per merge); 25 accepted (53%).
- **22 of the 47 were *inferred* proposals (BD-34): decisions the judge found in the history that nobody drafted. Most were noise:** implementation details (a request-hash format, a scheduler's sleep loop, a GET endpoint), or partial duplicates of a decision that *was* drafted, usually with "Not recorded" as the reason. Watchman rejected most of them. It accepted three true but trivial ones (how requests are hashed, connection passing, at-least-once delivery). Those are facts, not team decisions: an importance problem, not a factual one.
- In week 1 Watchman accepted an inferred partial duplicate of M01 while the full, well-drafted proposal for M01 was still waiting for an engineer, so the weaker version became D-0001. One merge produced two proposals for the same decision.

**Staleness (E2): the main failure**

- **No supersession reached the memory as a supersession.** Every one of the 47 proposals was kind `new`. When rate limiting moved to the gateway (M15) and payouts went individual (M19), the replacements were accepted as new decisions and **the replaced ones (D-0007, D-0009) stayed `accepted`, in force, side by side with what replaced them.** Two of the four supersessions could be checked (the others had a side not captured); both failed. The only link in the whole memory is one `depends-on`.
- Watchman noticed the conflict once by itself (it rejected the open Postgres-only proposal because D-0022 now allowed Redis), but nothing in the product connects a new decision to the one it replaces.

**Grader check (1 in 5, 11 grades read):** match, statement and reason agree with my reading in 11 of 11. The `invented` field is over-inclusive: it counts true implementation details the answer key doesn't cover, and memory ids such as D-0010, as invented. *The "75 invented claims" figure is not a count of false facts.* For R3 the grader separates contradictions from extra detail, and R1's proposals will be regraded with the same grader so the phases compare like with like.

**Harness:**

- Two sessions left uncommitted changes (committed by the harness, logged).
- "Agent asked, nobody answered" cost three steps (M05, M26 and a decoy). For R3 the simulated engineer will answer once, generically ("use your judgement and go ahead"), and steps rescued that way are reported separately, so R1 and R3 can be compared on the same steps.
- **Answer-key correction (dated, after R1):** M24 is inconsistent with M04 and is excluded from scoring in both phases.

## 2026-11-06: R1 cold read and decoys

**Cold read (E7, E3):** a fresh agent answered the 30 questions, three ways, three times each.

| | Memory only | Repos only | Both |
|---|---|---|---|
| All questions | 78% | 70% | **84%** |
| Held out | 71% | 60% | 77% |
| Why | 91% | 83% | 100% |
| Rejected options | 75% | 61% | 79% |
| Explored (dead ends) | **100%** | 50% | 100% |
| Stale (what's no longer in force) | 88% | 71% | 88% |
| As of a past date (E3) | 100% | 100% | 100% |
| Legacy (reasons only the author knows) | 0% | 0% | 0% |
| Wrong claims | 3 | 3 | 3 |

- The memory adds most where the code can't speak: dead ends (the edge-runtime spike, Q27: 3/3 with memory, 0/3 from repos) and decisions with no code (feature flags, Q22: 3/3 vs 0/3).
- **The repos are a strong baseline because these agents write detailed commit messages.** Many reasons are in Meridian's git history, and point-in-time questions are answerable from commit dates. Real teams' histories are thinner; the open-source replay (R4) tests that.
- Point-in-time questions scored 100% in every arm, even though the memory still holds the replaced rate-limit and payout decisions as in force: the agents reasoned from recorded dates. So staleness didn't show here; whether it misleads agents doing work is what the probes measure.
- Q02 (what money representations were rejected) is 0% with the memory, because D-0001 came from the inferred partial proposal without its rejected options (see the timeline entry). Q17 and Q13 trace to M26 and M05 never being built. Q20 and Q21 (legacy) are 0% in every arm: nobody touched those files during the timeline, so nobody asked their authors.
- Agents answering from the memory say plainly when it has no record ("the memory has no record of how a double submission is handled") rather than guessing.
- Hand-check: 12 answers against their grades, 12 agree.

**Decoys:** with and without the memory, the agents made the same changes in the same number of turns (4.5), touched nothing outside the task, and made no memory calls. The memory added about $0.025 a session, the session-start digest. Two of the three decoys (Q01, Q02) had already been done during the timeline, so both arms correctly changed nothing; decoys must be written against the end state, not the seed. To fix before R3.

## 2026-11-06: R1 probes, and R1 closed

60 probe runs (10 probes × 2 arms × 3). Conformed (followed or asked) 27/30 with the memory vs 18/30 without; violated 3/30 vs 9/30. Timely 24/24. Stale-fact failures 0/12 in both arms: in every stale probe, the code had moved on too.

- P06 (round half up; only Jo knows why it's half-even) was violated 3/3 in **both** arms. The lookup of `src/payouts/rounding.py` returned only the three team-wide rules (D-0002, D-0021, D-0022), counted as a hit, and so never suggested asking Jo. Team-wide rules cover every file, so the ask-the-author path can't fire anywhere.
- Every before-edit lookup in that run repeated D-0002, D-0021 and D-0022.

Full R1 summary and what R2 changes: [r1-baseline.md](r1-baseline.md). R1 spend $50.14; total $52.07.

## 2026-11-06: correction to the P06 finding

The R1 entry said team-wide rules suppressed the ask-the-author hint. Wrong: the hint's condition ignores them. The real gate is `MIN_COMMITS_TO_ASK_AUTHOR = 3`, and `src/payouts/rounding.py` has one commit (Jo, 2025). Corrected in r1-baseline.md. The separate point stands: every before-edit lookup repeats the team-wide rules.
