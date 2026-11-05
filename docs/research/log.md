# Research log

Dated entries: what was run, what was found, what changed. Method in [../research-plan.md](../research-plan.md); harness in [../../research/](../../research/README.md).

## 2026-11-05: R0 set-up

- PRs 15 to 18 merged to main after the full local check passed on PR 18's head (BD-01b).
- Plan revisions applied to plan.md (BD-04a, BD-36 to BD-40).
- Product instruments: the clock file (BD-41) and records of what each response showed the agent (BD-42).
- Harness built in `research/`: ledger and cost shim, observer hook, sandbox with per-engineer identities and simulated time, the Meridian answer key (26 decisions, 3 explored, 2 legacy reasons, 31 steps), 10 probes and 3 decoys, 30 queries (10 held out), the grader, metrics and the runner. Harness unit tests: 9, plus the product suite (117 in all).
- **Deviation from the plan, decided before any run:** E3 (versioned) asks its point-in-time questions (Q04, Q07, Q18) at the end of the timeline rather than at four checkpoints. Asking about the past from the present is the harder and more realistic case, and it costs no extra runs. Checkpoint questions can be added if the as-of answers turn out ambiguous.
- **Answer key size:** 26 decisions rather than the plan's "about 40". Every decision has a step that produces it; 40 would have meant filler.
