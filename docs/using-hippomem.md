# Using Hippomem

How a team installs and uses Hippomem. [hippomem.md](hippomem.md) defines what Hippomem is and why; [architecture.md](architecture.md) shows how it's built. Everything below runs today on one machine; items marked *later* wait for the remote version.

## The parts

| Part | What it is | Who touches it |
|---|---|---|
| **Hippomem server** | Holds the memory, answers queries, reconciles merges. `hippomem serve` | The admin, once |
| **MCP endpoint** | How every agent connects: `http://<host>:<port>/mcp` plus a token | Agents |
| **Claude Code hooks** | Session-start digest, before-edit lookup, end-of-turn check. Other agents get the MCP tools only | Each engineer, once per project |
| **Git hooks** | post-checkout, post-commit and post-merge send events to the server. *Later:* a GitHub app | Each engineer, once per repo |
| **Review** | `hippomem review`, `accept`, `reject`, or the agent raising proposals at session start. *Later:* a web page, Slack, PR comments | Engineers and reviewer agents |
| **`hippomem` CLI** | Setup, login, search and review from the terminal | Everyone |

## Setting it up as a team

**1. The admin creates the memory and the team's actors.**

```bash
pip install git+https://github.com/z10-labs/hippomem.git
hippomem init --admin @you                          # memory + config in ~/.hippomem, your admin token saved
hippomem actor add @alex --email alex@example.com   # prints Alex's token once
hippomem actor add @sam  --email sam@example.com
hippomem actor add watchman --kind reviewer         # a reviewer agent
```

The `--email` is the engineer's git author email. It's how a question about code someone wrote reaches them.

**2. Describe systems, owners and repos** in `~/.hippomem/hippomem.toml`, then apply it:

```toml
[server]
host = "127.0.0.1"
port = 8765

[acceptance]
evidence_only = "auto"      # extra evidence on an accepted decision: auto | review
self_approval = "never"     # never | standard (a solo engineer may accept their own standard proposals)

[review]
max_per_session = 2         # proposals an agent may raise with its engineer per session

[branches]
stale_after_days = 30

[[systems]]
id = "billing"
name = "Billing"
owners = ["@alex"]
repos = ["ledger-api:src/billing/**", "ledger-worker"]   # one system, many repos; no path means the whole repo

[[systems]]
id = "storefront"
name = "Storefront"
owners = ["@sam"]
depends_on = ["ui-kit"]
repos = ["storefront"]
```

```bash
hippomem config apply
hippomem serve
```

Mappings can also be added later with `hippomem map add <repo> <glob> <system>`, or proposed by an agent (`hippomem_propose_mapping`) for an owner to confirm.

**3. Acceptance rules** come from `[acceptance]` plus these fixed rules:

| Proposal | Who can accept |
|---|---|
| Evidence for an accepted decision | Anyone, or automatically (`evidence_only = "auto"`) |
| New decision, standard weight | A reviewer agent or an engineer |
| Heavy, superseding, or conflicting with another decision | An engineer |
| Covering a module, service or team, or a repo mapping | An owner of one of its systems (any engineer if it has no owners) |
| Your own proposal | Nobody, unless `self_approval = "standard"` and it's standard weight |

## Engineers and their coding agents

**One-time setup per engineer, then per project and repo:**

```bash
hippomem login <your token>                 # saved to ~/.hippomem/token
export HIPPOMEM_TOKEN=<your token>          # Claude Code reads it for the MCP connection

cd your-repo
hippomem install claude-code                # .mcp.json (no secret in it) + .claude/settings.local.json (hooks)
hippomem install git-hooks                  # post-checkout, post-commit, post-merge
```

`.mcp.json` refers to `${HIPPOMEM_TOKEN}`, so it's safe to commit. `settings.local.json` holds paths on your machine and stays uncommitted. Existing hooks are kept; Hippomem's are appended.

**What the coding agent gets:**

- **At session start:** a digest with the repo, branch and session to pass to tools; decisions in force for the repo's systems; this branch's draft trail; other branches' drafts on the same systems; up to `max_per_session` proposals and any questions waiting for its engineer.
- **Before an edit:** decisions covering the file, system- and team-wide rules that apply, open proposals about it, and, when nothing covers a file someone else wrote, a suggestion to ask its author.
- **Tools:** `hippomem_query`, `hippomem_draft`, `hippomem_drop`, `hippomem_trail`, `hippomem_get`, `hippomem_chain`, `hippomem_list`, `hippomem_systems`, `hippomem_propose`, `hippomem_propose_mapping`, `hippomem_ask_author`, `hippomem_answer`, `hippomem_review`, `hippomem_decide`.
- **At the end of every turn:** if dependencies, schema or infrastructure files changed and nothing was drafted, one nudge to draft.
- **Identity:** everything it writes is recorded as `@alex via claude-code`.

**What the agent can't do:** accept anything on its own (it only relays its engineer's answer with `hippomem_decide`), edit an accepted decision, or skip the acceptance rules.

## Branches and merges

- Checking out a branch starts its draft trail. Drafts belong to the branch, so the next session on it picks up where the last one stopped.
- Commits mark drafts as backed by code.
- Merging into the default branch reconciles the trail: drafts that were built become proposals with the commits as evidence; drafts dropped along the way become their rejected alternatives; lasting dead ends become explored records.
- An abandoned branch: `hippomem branch close <branch>`. Branches idle longer than `stale_after_days` are closed when the server starts, or with `hippomem branch close-stale`.

## Reviewer agents

A reviewer agent such as Watchman gets its own token with the **reviewer** role (`hippomem actor add watchman --kind reviewer`). It can read everything, propose, and accept low-risk proposals (evidence and standard weight), never its own and never heavy or scope-wide ones. *Later:* PR-time checks of a diff against decisions in force, posted as PR comments.

## Roles

| Role | Read | Draft | Propose | Accept | Configure |
|---|---|---|---|---|---|
| Coding agent (engineer's token) | ✓ | ✓ | ✓ | relays its engineer's answer only | ✗ |
| Reviewer agent | ✓ | ✓ | ✓ | evidence and standard, never its own | ✗ |
| Engineer | ✓ | ✓ | ✓ | standard and heavy, never their own; scope-wide in systems they own | ✗ |
| Admin | ✓ | ✓ | ✓ | everything except their own | config, actors, mappings |

## Searching and reviewing from the terminal

```bash
hippomem query "should we use redis for rate limiting" --repo ledger-api
hippomem show D-0001
hippomem list --system billing
hippomem review
hippomem accept P-0007 --note "agreed in standup"
```

## What waits for the remote version

A hosted server with Postgres, a GitHub app instead of local git hooks, single sign-on instead of tokens, a web review queue and Slack, multiple teams, and PR-time checks by reviewer agents. The roles, the data model and the event flow stay the same.
