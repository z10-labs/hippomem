# R1 baseline: today's product, measured

Run 2026-11-05 to 06 on the product as merged in PR 18 (plus the instruments in BD-41 and BD-42). Method in [../research-plan.md](../research-plan.md); every step and check in [log.md](log.md). Spend: R0 $1.93, R1 $50.14, **$52.07 of $150**.

## Against the aim

| Property | Pass bar | R1 | Verdict |
|---|---|---|---|
| **Factual** (accepted decisions correct) | ≥ 90%, no invented decisions | 22 of 25 accepted decisions match the answer key and state it correctly; the other 3 are true but trivial implementation details, not false facts | **Pass** on truth, see *Important* |
| **Complete enough** | ≥ 80% heavy, ≥ 60% standard | heavy 6/8 (75%), standard 16/18 (89%). Of the 4 misses, 1 is the product's (M03), 2 are a simulation limit, 1 an answer-key error | **Borderline**: heavy just under, and the miss is a product bug |
| **Not stale** | every reversal marked by the merge that made it; agents act on a superseded fact in < 5% of probes | **0 of 2** checkable supersessions recorded; the replaced decisions stay *in force*. Agents acted on a stale fact in 0 of 12 stale probes | **Fail** in the memory, though agents weren't misled this time |
| **Versioned** (as-of questions) | ≥ 90% | 100% (9 of 9 answers) | **Pass**, but see note |
| **Important** | ≥ 70% of proposals accepted; ≤ 1 nudge per 3 turns | 53% accepted (25 of 47); nudges 9 of 37 turns | **Fail** on proposals (noise), pass on nudges |
| **Timely** (decision in context before the edit) | ≥ 85% | **24 of 24** probe runs where a record existed | **Pass** |
| **Useful** | conformance gap ≥ 30 points; cold read ≥ 80% | conformance 90% with vs 60% without (+30); violations 10% vs 30%. Cold read: memory alone 78%, repos alone 70%, both **84%** | **Pass, narrowly** |

## What works

- **The memory changes what agents build.** Asked to add Kafka, Tailwind, or an ECB lookup the team had moved away from, agents with the memory stopped and cited the decision; without it they built it (P05 3/3 vs 0/3, P09 3/3 vs 0/3). With no instruction at all, timeline agents chose Postgres counters and an outbox because the memory ruled out Redis and Kafka.
- **The right record arrives in time.** In every probe where the decision existed, it was in the agent's context before its first edit (24/24).
- **Drafted decisions are captured well.** 22 of 26 decisions reached the memory, with their reasons; 18 of 25 rejected options were named. All three abandoned spikes became explored records with revisit conditions, and the memory is what makes dead ends answerable (cold-read explored questions 100% vs 50% from the repos).
- **The reviewer agent is useful.** Watchman rejected most of the noise and caught a real inconsistency in the answer key itself (M24 vs M04).
- **No cost on unrelated work.** Decoy tasks took the same turns with and without the memory; the digest adds about $0.025 a session.

## What fails, in order of harm

1. **Supersession isn't recorded (staleness).** Every proposal, from agents and from the judge, is kind `new`. When rate limiting moved to the gateway and payouts went individual, the new decisions were accepted and **the replaced ones stayed `accepted` and in force**, side by side. The memory contradicts itself, and the digest shows both. Agents weren't misled in these probes because the code had changed too, but nothing guarantees that.
2. **Inferred proposals are mostly noise.** 22 of the 47 proposals were "decisions found in the history that nobody drafted" (BD-34). Most were implementation details or partial duplicates of a decision that *was* drafted, often with "Not recorded" as the reason. They halve the acceptance rate, and in week 1 one of them **displaced the good proposal for the same decision**, so D-0001 lost its rejected options (cold-read Q02: 0%).
3. **A sole owner can't get their own team-wide decision accepted.** Priya, the only platform owner and the only admin, proposed the Postgres-only rule; self-approval is forbidden and a team-wide proposal needs an owner, so it sat open for nine weeks and never entered the memory. *Fixing this changes who may accept what, so it needs the owner's decision.*
4. **"Ask the author" never fires.** The before-edit lookup suggests asking a file's author only when nothing covers the file, but team-wide decisions cover every file, so the lookup is never empty. Asked to change the half-even rounding only Jo knew the reason for, agents changed it every time, with and without the memory (P06: 3/3 violated in both arms).
5. **The before-edit lookup repeats the same team-wide rules on every edit,** and the session digest lists every decision in force (15 by week 12), replaced ones included. Context grows with the memory, not with relevance.

## Caveats

- **Simulated engineers don't answer questions.** Three steps produced nothing because the agent stopped to ask (M05, M26, a decoy). The memory can't capture what was never built.
- **Meridian's git history is unusually good.** These agents write detailed commit messages, so the repos alone answer 70% of the cold read, and as-of questions are answerable from commit dates. That flatters the repo baseline and understates the memory's value; the open-source replay (R4) is the test with real history.
- **Probes mostly end in "asked".** The nudges contradict the code as much as the memory, so agents in both arms often stopped to ask. Asking counts as conforming, but it's friction: on P03 the agent without memory just did the right thing, while the agent with it stopped to ask.
- **Single timeline run.** E1 ran once (12 weeks); the probes and cold read ran 3 times per arm. Treat capture numbers as one sample.
- **Answer key:** M24 is inconsistent with M04 (found by Watchman) and is excluded from scoring.

## What R2 changes

| Failure | Change | Needs the owner? |
|---|---|---|
| 1 Supersession | Proposals can say what they replace: `supersedes` on drafts and proposals; the judge compares each new proposal with similar accepted decisions and proposes a supersession when it replaces one; accepting it retires the old one and links them. Plus the schema change (C3): versioned alternatives, constraints and anchors; links between any records with provenance; `valid_to` | No |
| 2 Inferred noise | The judge sees the branch's drafts and the accepted decisions, never proposes something already drafted or accepted, and infers only choices between options with a stated reason, not implementation details | No |
| 3 Sole-owner deadlock | e.g. when the proposer is the system's only owner, any other engineer may accept | **Yes**: acceptance rules |
| 4 Ask the author | Only code- and system-level coverage counts; team-wide rules don't suppress the hint | No |
| 5 Repeated context | Team-wide rules once per session in the digest, not on every lookup; replaced decisions out of the digest | No |
