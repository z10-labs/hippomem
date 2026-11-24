# Engineering Ops Graph — Spec

A knowledge graph and action layer that helps run an engineering team. Agents investigate, propose and do the work. Humans approve anything risky. The graph keeps a record of what happened, why, and who decided.

## 1. Goals

- **Decision memory:** every significant technical decision (for example, Postgres over MongoDB, or decommissioning a service) is recorded with its options, evidence, proposer and approver.
- **Faster investigations:** errors such as DLQ messages, alerts and incidents are investigated automatically by agents, using past investigations as evidence.
- **Safe autonomy:** agents can do reversible work freely. Irreversible actions need an approved decision.
- **Auditability:** every change can be traced from incident to evidence to decision to work item to the affected service.

## 2. Architecture: three layers

| Layer | What it is | Example |
| --- | --- | --- |
| 1. Ontology | The blueprint: node types, relationships, properties, constraints and meaning | "A Decision chose exactly one Option" |
| 2. Data | The real instances, synced from existing tools | "Decision D-42: migrate orders to Postgres" |
| 3. Actions | Governed operations that change data, with preconditions, effects and side effects | "Decommission Service" |

Rule: **agents never write to the graph directly.** All writes go through actions.

## 3. Layer 1: Ontology

This uses the Neo4j Ontology Graph Specification format: one YAML file, validated in CI and semantically versioned.

### 3.1 Node types

| Node | Description | Key | Aliases |
| --- | --- | --- | --- |
| Service | A deployable system or component | name | app, microservice |
| Team | The group that owns services | name | squad |
| Person | A human engineer | email | engineer, owner |
| Agent | An AI agent that acts in the system | agentId | bot |
| ErrorEvent | A single error signal, such as a DLQ message or an alert | eventId | DLQ message, alert |
| Incident | A user-impacting problem grouping error events | incidentId | outage |
| Investigation | An inquiry into an error or incident | investigationId | RCA, triage |
| Evidence | A finding: log, query, metric, PR, past incident | evidenceId | finding |
| RootCause | A confirmed or proposed cause | rootCauseId | cause |
| Decision | A technical choice requiring approval | decisionId | ADR |
| Option | An alternative considered for a decision | optionId | alternative |
| Approval | A human sign-off on a decision or action | approvalId | sign-off |
| WorkItem | A ticket or task | ticketId | ticket, task |
| PullRequest | A code change | url | PR |
| Deployment | A release of a service | deploymentId | release, deploy |
| ActionRun | A logged execution of a layer-3 action | runId | audit entry |

### 3.2 Key properties and constraints

- **Decision.status:** PROPOSED, APPROVED, REJECTED or SUPERSEDED (required).
- **Service.status:** ACTIVE, DEPRECATED, DECOMMISSIONING or DECOMMISSIONED (required).
- **Investigation.status:** OPEN, CONCLUDED or CLOSED.
- **RootCause.confidence:** a float from 0 to 1. **RootCause.confirmedBy** is set only by a Person.
- **ErrorEvent.embedding** and **Investigation.summaryEmbedding:** vectors for similarity search.
- All timestamps are zoned datetimes.

### 3.3 Relationships

| Relationship | From → To | Cardinality |
| --- | --- | --- |
| OWNS | Team → Service | Each Service has exactly 1 owning Team |
| MEMBER\_OF | Person → Team |  |
| DEPENDS\_ON | Service → Service |  |
| OCCURRED\_IN | ErrorEvent → Service | Exactly 1 |
| PART\_OF | ErrorEvent → Incident | 0 or 1 |
| INVESTIGATES | Investigation → ErrorEvent / Incident | At least 1 |
| PRODUCED | Investigation → Evidence |  |
| CONCLUDED | Investigation → RootCause | 0 or 1 |
| SIMILAR\_TO | Investigation → Investigation | With score |
| CONSIDERED | Decision → Option | At least 2 |
| CHOSE | Decision → Option | Exactly 1 once APPROVED |
| BASED\_ON | Decision → Evidence |  |
| PROPOSED\_BY | Decision → Agent / Person | Exactly 1 |
| APPROVED\_BY | Decision → Approval → Person |  |
| AFFECTS | Decision / Incident → Service |  |
| SUPERSEDES | Decision → Decision |  |
| IMPLEMENTS | WorkItem / PullRequest → Decision |  |
| DEPLOYED | Deployment → Service |  |
| INCLUDES | Deployment → PullRequest |  |
| PERFORMED | ActionRun → any node |  |

### 3.4 Implied labels

- **CriticalService** implies Service.
- **SeniorEngineer** implies Person (used in approval rules).
- **ArchitectureDecision** implies Decision.

## 4. Layer 2: Data sources

| Source | Feeds | Sync |
| --- | --- | --- |
| Service catalogue (Backstage or similar) | Service, Team, DEPENDS\_ON | Scheduled |
| Queue / monitoring (SQS, PagerDuty, Datadog) | ErrorEvent, Incident | Webhook, real time |
| Jira / Linear | WorkItem | Webhook |
| GitHub | PullRequest, Deployment | Webhook |
| ADR docs / Slack | Decision, Evidence | Via actions |

Mappings live in the ontology's extensions, not the core model.

## 5. Layer 3: Actions

### 5.1 Tiers

| Tier | Meaning | Who can run | Examples |
| --- | --- | --- | --- |
| Free | Read-only or reversible | Agents and people | Open investigation, add evidence, propose decision, open draft PR |
| Reviewed | Affects production but recoverable | Agent runs, person approves | Replay DLQ messages, restart service |
| Gated | Irreversible or high blast radius | Needs an APPROVED decision and an authorised person | Merge to main, deploy, decommission, delete data |

### 5.2 Action definition schema

Every action is declared in YAML with inputs, tier, preconditions, effects and side effects. Each precondition has a failure message and a suggested next action, so agents can recover on their own.

```yaml
action: decommission-service
tier: gated
inputs: [service, reason, decision, requester]
preconditions:
  - id: has-approved-decision
    check: decision.status = APPROVED and decision AFFECTS service
    on_fail: {message: No approved decision, suggest_action: propose-decision}
  - id: no-dependents
    check: no ACTIVE Service DEPENDS_ON service
    on_fail: {message: Services still depend on this, suggest_action: list-dependents}
  - id: no-open-incidents
    check: no OPEN Incident AFFECTS service
    on_fail: {message: Open incidents exist, suggest_action: list-incidents}
  - id: authorised
    check: requester has label SeniorEngineer
    on_fail: {message: Needs senior engineer, suggest_action: request-approval}
effects:
  - set service.status = DECOMMISSIONING
  - create WorkItem IMPLEMENTS decision
  - create ActionRun
side_effects:
  - create Jira ticket
  - post to owning team's Slack channel
```

### 5.3 Execution rules

- **Dry run first:** every action supports dry\_run, which evaluates preconditions without writing anything.
- **Atomic:** effects run in a single Neo4j transaction. They all apply or none do.
- **Logged:** every run creates an ActionRun node recording who, when, inputs, outcome and the failed preconditions.
- **Side effects after commit:** external calls happen only after the graph write succeeds, and are retried on failure.

### 5.4 Core action catalogue

| Action | Tier |
| --- | --- |
| record-error-event | Free (system) |
| open-investigation | Free |
| add-evidence | Free |
| find-similar-investigations | Free |
| propose-root-cause | Free |
| confirm-root-cause | Reviewed (person only) |
| propose-decision | Free |
| approve-decision / reject-decision | Gated (person only) |
| open-pull-request | Free |
| replay-dlq-messages | Reviewed |
| merge-pull-request | Gated |
| deploy-service | Gated |
| decommission-service | Gated |

## 6. Key flows

### 6.1 DLQ error investigation

1. **System:** a queue webhook runs record-error-event, which creates an ErrorEvent OCCURRED\_IN the service.
2. **Agent (automatic):** runs open-investigation.
3. **Agent:** gathers evidence from recent deployments and their PRs, upstream producers, logs, and similar past investigations via vector search.
4. **Agent:** runs propose-root-cause with a confidence score and posts a summary to the owning team.
5. **Person:** confirms or corrects the root cause.
6. **Agent:** writes the fix on a branch and opens a PR linked to the investigation.
7. **Person:** approves replay-dlq-messages and merges the PR. If the fix is a bigger change, it becomes a proposed decision.

### 6.2 Agent hits a missing decision

1. A user asks the agent to decommission Legacy Billing.
2. The agent dry-runs decommission-service, which fails has-approved-decision and suggests propose-decision.
3. The agent drafts the decision with options and evidence, and it is saved as PROPOSED.
4. **Work continues:** the agent prepares removal PRs as drafts linked to the decision.
5. A person approves the decision, and the agent reruns the action, which passes.

### 6.3 Merge gating

A required GitHub status check queries the graph: is this PR linked to an APPROVED decision, or is it a free-tier change? If not, the merge stays blocked. If a decision is rejected, its PRs are kept and linked as explored options.

## 7. Agent integration

- Actions are exposed as MCP tools and a REST API.
- Agents get read access to the graph plus the ontology's descriptions and aliases, so they can map natural language to queries.
- Agents receive structured precondition failures with suggested next actions.

## 8. Non-goals (for now)

- Agents approving their own decisions.
- Replacing Jira, PagerDuty or GitHub. The graph links to them and does not replace them.
- RDF/OWL export.

## 9. Open questions

- Who counts as an approver per tier: per team, or org-wide?
- How long do PROPOSED decisions live before they expire?
- What confidence threshold lets an agent auto-post a root cause?
- What is the source of truth for service dependencies?
