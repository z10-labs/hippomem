# The workspace: tasks, docs and resources, shared with every agent

Hippomem's decisions say *why* the software is the way it is. The workspace holds the *work around it*: the projects the team is running, the tasks in them, the docs that explain them and the links and resources people rely on (Pinterest boards, Figma files, videos, PDFs, repos). People work in **Notion**; every agent connected to Hippomem reads and updates the same items through MCP tools, and sees them next to the decisions they touch.

Workspace items are **context, never decisions**. Nothing in the workspace is reviewed or put in force, and a task that says "use Redis" does not override a decision that says otherwise. Why this lives in Hippomem at all is BD-51 in [build-decisions.md](build-decisions.md).

## What it holds

| Kind | Id | In Notion | What it's for |
|---|---|---|---|
| Project | `PRJ-0001` | Projects | Groups the work; has a status, owner, summary and target date |
| Task | `T-0001` | Tasks | A work item: status, priority, assignee, the agent on it, due date, branch |
| Doc | `DOC-0001` | Docs | A spec, guide, runbook, notes, research or ADR, in Notion or elsewhere (`External URL`) |
| Resource | `R-0001` | Resources | A link or file. Its kind (Pinterest, Figma, Canva, Video, Document, Repo, Image, Notion, Link) is detected from the URL |

Every item can name a Hippomem **system** (`billing`), which is how a repo's agents find it.

### How things connect

Name any two things and Hippomem works out the link:

```
task ──uses──► doc / resource        anything ──part-of──► project
anything ──cites──► decision (D-)    two of the same kind ──relates──► each other
```

Links Notion has a field for (a task's Project, Docs and Resources; a doc's or resource's Project; the Decisions text on tasks and docs) are written to Notion and read back on every sync. Links Notion can't hold (a resource citing a decision, two resources related) stay in Hippomem.

A decision's full record (`hippomem_get`) lists the tasks and docs that cite it, so "who is implementing D-0007, and where's the spec?" is one call.

## What agents get

| Tool | Does |
|---|---|
| `hippomem_workspace` | Everything connected to a project (by id or title) or system: open tasks, docs, resources, and the decisions involved. With `repo`, the repo's systems. With nothing, the active projects |
| `hippomem_workspace_search` | Find any item by meaning, optionally one kind |
| `hippomem_item` | One item in full, with all its links |
| `hippomem_task` | Create a task, or update one by id (only the fields passed change). Links it to a project, docs, resources and decisions |
| `hippomem_doc` | Register or update a doc |
| `hippomem_resource` | Save a link. Saving the same URL again updates the first record instead of duplicating it |
| `hippomem_project` | Start a project (the server instructions tell agents to ask their engineer first) |
| `hippomem_connect` | Link or unlink any two items, or an item and a decision |

Without asking, an agent also sees **up to five open tasks for the repo's systems** in its session digest, most urgent first. When the workspace is empty the digest is unchanged.

The server instructions tell agents to check the workspace before starting work, keep task status current, and save useful links.

## Setting it up with Notion

The Notion side is four databases under one page. They already exist in the team's Notion as **Hippomem Workspace** (Projects, Tasks, Docs, Resources); to make them in another workspace, copy that page.

1. **Make an integration.** In Notion: Settings → Connections → Develop or manage integrations → New integration (internal). Copy its token.
2. **Share the page with it.** On the Hippomem Workspace page: ••• → Connections → add the integration. The four databases inherit access.
3. **Point Hippomem at the databases** in `hippomem.toml`. The ids are the databases' *data source* ids (in Notion: open the database → ••• → Copy link to data source, or see the Workspace page):

   ```toml
   [notion]
   projects = "4243337f-4ff4-8155-8036-000b44fdbbc3"
   tasks = "4243337f-4ff4-81ff-9f48-000bb6407111"
   docs = "4243337f-4ff4-81ce-9fc8-000bea51efb6"
   resources = "4243337f-4ff4-81e9-bc9d-000b241c874b"
   token_env = "NOTION_TOKEN"   # the variable holding the token; never put the token in this file
   sync_minutes = 5
   ```

4. **Start the server with the token:** `NOTION_TOKEN=secret_… hippomem serve`. It syncs at start and every `sync_minutes`. `hippomem workspace sync` syncs now; `hippomem workspace show --project Checkout` prints what an agent would see.

Without a `[notion]` block the workspace still works, kept only in Hippomem. With the block but no token, agents' changes are kept and marked "not yet in Notion" until a token is set.

## How sync works

A sync **pushes** what changed in Hippomem since the last sync, then **pulls** every page of the four databases.

- **Agents' writes go to Notion straight away** when it can be reached. If Notion fails, the agent is told, the item stays marked pending, and the next sync retries. An agent's work is never lost because Notion was down.
- **Pending local changes win over a pull** until they're pushed, so a sync that runs between an agent's write and its push can't undo it.
- **Edits made in Notion win otherwise**: status changes, new tasks, renamed docs and relations people edit all come in on the next pull.
- **A page deleted in Notion** is archived in Hippomem (hidden, links kept).
- **New Notion pages get their Hippomem id** written into the `Hippomem ID` column, so people can quote `T-0004` to an agent.
- **People.** Assignees and owners are matched to Notion users by name or email. A name that matches nobody is kept in Hippomem and never clears the people already set in Notion.

The pull reads everything each time rather than only recent edits: it is the only way to notice deleted pages without webhooks, and a small team's workspace is a few hundred pages. Webhooks need a public URL, which the local proof of concept doesn't have; they come with going remote.

## Renaming things in Notion

Property names are fixed in `src/hippomem/notion.py` (`FIELDS`, `RELATIONS`). Renaming a column in Notion means renaming it there too; adding new select options (a new task status) needs no change, though agents can only set the statuses listed in `workspace.STATUSES`.
