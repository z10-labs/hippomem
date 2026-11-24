# Engineering Ops Graph — v0.01

The first slice of the full Engineering Ops Graph spec. **Goal:** one DLQ alert is investigated end to end by an agent, and a human confirms the root cause.

## 1. Scope

**In scope**

- One DLQ queue for one service
- GitHub deployments and PRs for that service
- Agent-led investigation with evidence
- Human confirmation of the root cause
- Slack summary

**Out of scope (later versions)**

- Decisions, options and approvals
- Gated actions (merge, deploy, decommission)
- Incidents, teams and work items
- Message replay

## 2. Ontology

| Node | Key | Notes |
| --- | --- | --- |
| Service | name | Aliases: app, microservice |
| ErrorEvent | eventId | DLQ message, error text, timestamp, embedding |
| Investigation | investigationId | status: OPEN, CONCLUDED, CLOSED |
| Evidence | evidenceId | kind: DEPLOY, PR, LOG, SIMILAR\_CASE; summary; link |
| RootCause | rootCauseId | summary, confidence 0–1, confirmedBy |
| Deployment | deploymentId | deployedAt, commit SHA |
| PullRequest | url | title, author, mergedAt |

| Relationship | From → To | Cardinality |
| --- | --- | --- |
| OCCURRED\_IN | ErrorEvent → Service | Exactly 1 |
| INVESTIGATES | Investigation → ErrorEvent | At least 1 |
| PRODUCED | Investigation → Evidence |  |
| CONCLUDED | Investigation → RootCause | 0 or 1 |
| DEPLOYED | Deployment → Service | Exactly 1 |
| INCLUDES | Deployment → PullRequest |  |
| REFERS\_TO | Evidence → any node |  |

### Starter ontology file (sketch)

```yaml
name: eng-ops-graph
version: 0.0.1
nodes:
  - id: service
    label: Service
    aliases: [app, microservice]
    properties:
      name: {type: STRING, key: true}
  - id: error-event
    label: ErrorEvent
    description: A single failed message landing in a dead-letter queue
    aliases: [DLQ message, dead letter]
    properties:
      eventId: {type: STRING, key: true}
      queue: {type: STRING, required: true}
      errorText: {type: STRING}
      occurredAt: {type: ZONED DATETIME, required: true}
      embedding: {type: VECTOR}
  - id: investigation
    label: Investigation
    aliases: [RCA, triage]
    properties:
      investigationId: {type: STRING, key: true}
      status: {type: STRING, required: true}
  - id: evidence
    label: Evidence
    properties:
      evidenceId: {type: STRING, key: true}
      kind: {type: STRING, required: true}
      summary: {type: STRING}
  - id: root-cause
    label: RootCause
    properties:
      rootCauseId: {type: STRING, key: true}
      summary: {type: STRING, required: true}
      confidence: {type: FLOAT}
      confirmedBy: {type: STRING}
relationships:
  - {id: error-in-service, type: OCCURRED_IN, from: error-event, to: service}
  - {id: investigates-error, type: INVESTIGATES, from: investigation, to: error-event}
  - {id: produced-evidence, type: PRODUCED, from: investigation, to: evidence}
  - {id: concluded-cause, type: CONCLUDED, from: investigation, to: root-cause}
extensions:
  indexes:
    - vector index on ErrorEvent.embedding
  mappings:
    - ErrorEvent from DLQ webhook payload
    - Deployment and PullRequest from GitHub webhooks
```

## 3. Actions

| Action | Tier | Run by | Effect |
| --- | --- | --- | --- |
| record-error-event | Free | System (webhook) | Creates an ErrorEvent linked to the service |
| open-investigation | Free | Agent | Creates an OPEN Investigation linked to the error |
| add-evidence | Free | Agent | Adds Evidence linked to the investigation |
| propose-root-cause | Free | Agent | Adds a RootCause with confidence, sets status to CONCLUDED |
| confirm-root-cause | Reviewed | Person only | Sets confirmedBy, sets status to CLOSED |

Every action:

- supports a dry run
- writes in one transaction
- logs who ran it and when

confirm-root-cause has one precondition: the requester must be a Person, not an Agent. If an agent tries it, the action fails with the suggestion "ask a human in Slack."

## 4. Flow

1. A message hits the DLQ, and the webhook calls record-error-event.
2. That automatically triggers the agent, which runs open-investigation.
3. The agent gathers evidence:
   - deployments to this service in the last 24 hours, and their PRs
   - the error text and message payload
   - similar past ErrorEvents, using vector search
4. The agent runs propose-root-cause with a confidence score.
5. The agent posts a summary to Slack: the cause, the evidence links, and **Confirm** or **Correct** buttons.
6. A human clicks Confirm, which runs confirm-root-cause. Clicking Correct lets the human edit the summary first.

## 5. Build components

- **Neo4j:** an Aura free tier or local instance, with constraints created from the ontology file.
- **Action service:** a small API, for example in Go or TypeScript, that loads the ontology and exposes the five actions as REST endpoints and MCP tools.
- **Webhooks:** DLQ to record-error-event, and GitHub to Deployment and PullRequest nodes.
- **Agent:** an LLM with MCP access to the actions and read-only Cypher, prompted with the ontology's descriptions and aliases.
- **Slack app:** posts summaries and handles the Confirm and Correct buttons.
- **CI:** validates the ontology file against the JSON Schema on every commit.

## 6. Success criteria

- A real DLQ message produces a Slack summary within 5 minutes.
- The engineers rate the proposed root cause as correct or useful in at least 60% of cases over two weeks.
- The second similar error surfaces the first investigation as evidence.
- The agent makes zero direct writes to the graph outside the actions.

## 7. Next step (v0.1)

- Add Decision, Option, Approval and Person.
- Add propose-decision and approve-decision.
- Agent opens fix PRs linked to investigations.
