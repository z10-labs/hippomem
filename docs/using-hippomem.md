# Using Hippomem

How a team installs and uses Hippomem. [hippomem.md](hippomem.md) defines what Hippomem is and why; this document is the product shape. Commands and config below are the intended design, not something that exists yet.

## The parts

| Part | What it is | Who touches it |
|---|---|---|
| **Hippomem server** | Holds the memory, answers queries, runs reconciliation. Runs as `hippomem serve` on one machine for the proof; hosted later | The admin, once |
| **MCP endpoint** | How every agent connects: one URL plus a token | Agents |
| **Agent plugin** | For Claude Code: the MCP connection plus hooks (session-start digest, before-edit lookup, per-turn draft check). Other agents get the MCP tools only | Each engineer, once |
| **Git integration** | Feeds commits, PRs, merges and closures to the server. A local git hook or `gh` polling for the proof; a GitHub app later | The admin, once |
| **Review queue** | Where proposals get accepted or rejected. A CLI for the proof; a web page, Slack or PR comments later | Engineers and reviewer agents |
| **`hippomem` CLI** | Setup, login, browsing and search from the terminal | Everyone |

## Setting it up as a team

**1. The admin creates the team.**

```bash
hippomem serve                          # proof: on your machine
hippomem team create z10-labs
hippomem github connect z10-labs        # git events for the team's repos
```

**2. Map repos to systems.** Hippomem drafts a map from the repos' structure: which repos and folders make up which service or module. The team corrects it. See [Systems](hippomem.md#systems).

**3. Set owners and acceptance rules.** Team-level config:

```yaml
# hippomem.yaml (team)
systems:
  billing:
    repos: [product-api:src/billing/**, product-worker:**]   # one system, many repos
    owners: ["@alex", "@sam"]
  sessions:
    repos: [product-api:src/sessions/**]
    owners: ["@sam"]

acceptance:
  evidence_only: auto              # extra evidence on an accepted decision
  standard: reviewer_agent_or_engineer
  heavy: engineer                  # architecture, security, supersedes, conflicts
  scope_wide: system_owner
  self_approval: never

review:
  surface_at: [session_start, task_done]
  max_per_session: 2

branches:
  stale_after_days: 30
  explored_requires: lesson        # only keep dead ends that taught something
```

## Engineers and their coding agents

**One-time setup per engineer:**

```bash
hippomem login                          # personal token
hippomem install claude-code            # adds MCP + hooks to ~/.claude
```

No per-repo setup: the server already knows which repos belong to which systems.

**What the coding agent gets:**

- **At session start:** a short digest of the decisions in force for this repo's systems, this branch's draft trail, other branches' drafts touching the same scope, and up to two pending proposals for its engineer to review.
- **Before an edit:** decisions that cover the file it's about to change. If none do and the code is load-bearing, the context to ask its author why it's built that way.
- **Tools:** `query`, `draft`, `chain`, `list`.
- **At the end of every turn:** a cheap check for decisions or reversals it made but didn't draft.
- **Identity:** every draft records the engineer and the agent, e.g. `@alex via claude-code`, so the audit trail stays readable.

**What the agent can't do:** accept anything itself, edit an accepted decision, or see other teams' memory. When it brings a proposal to its engineer, the engineer's answer is what counts.

## Reviewer agents

A dedicated reviewer agent such as Watchman or Sentinel gets its own credential with the **reviewer** role:

```bash
hippomem agent create watchman --role reviewer --systems all
```

| Git event | What the reviewer does |
|---|---|
| PR opened or updated | Checks the diff against decisions in force. Flags contradictions and things that depend on what's being changed, as PR comments |
| PR merged | Reconciles the branch's drafts into proposals, with the commits as evidence |
| Proposal in the queue | Accepts what the rules allow (evidence-only, standard weight). Never its own proposals, nothing from the same session. Heavy ones go to an engineer |
| Branch closed or stale | Sorts the trail and keeps dead ends worth remembering as explored records |

## Roles

| Role | Read | Draft | Propose | Accept | Configure |
|---|---|---|---|---|---|
| Coding agent | ✓ | ✓ | only through reconciliation | ✗ (brings proposals to its engineer) | ✗ |
| Reviewer agent | ✓ | ✗ | ✓ | low-risk only, never its own | ✗ |
| Engineer | ✓ | ✓ | ✓ | standard; heavy within their systems, never their own | ✗ |
| System owner | ✓ | ✓ | ✓ | everything in their system, including scope-wide | system config |
| Admin | ✓ | | | | everything |

## A typical day

1. Alex's agent starts on `feat/refund-window`. The digest says billing owns all money writes, and that another branch is moving refund logic into a background job. It also brings one pending proposal from Sam's work for Alex to approve.
2. The agent drafts "store the refund deadline on the order", then reverses it and drafts "compute it from the delivery date", with the reason.
3. The agent is about to change the delivery-timestamp code, which no decision covers. It asks Sam, who wrote it, why timestamps are stored in UTC. Sam's answer becomes a proposal.
4. The PR opens. Watchman flags that the change depends on the UTC decision and finds no contradictions.
5. The PR merges. Reconciliation produces one proposal, with the reversed option as a rejected alternative. Watchman accepts it: standard weight, not its own.
6. A week later, another agent touching refunds gets that decision back before writing any code.

## What the local proof needs

- **Needed:** `hippomem serve`, a token per agent, the Claude Code plugin, the team config file, git events from a local hook or `gh` polling, and the review queue as a CLI.
- **Can wait until it goes remote:** the GitHub app, the web review queue, Slack, multiple teams, and hosting.

The roles, config shape and event flow stay the same from local to remote. Only where the server runs and how events reach it change.
