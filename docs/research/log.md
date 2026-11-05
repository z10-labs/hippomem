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
