"""The workspace: tasks, docs, resources and projects, their links, and the Notion sync."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from hippomem import config, notion, service, sessions, workspace
from hippomem.policy import Actor
from tests.helpers import accepted_decision

NOTION = notion.NotionConfig(projects="ds-projects", tasks="ds-tasks", docs="ds-docs", resources="ds-resources")
ALEX = Actor("@alex", "engineer", "claude-code")


class FakeNotion:
    """Just enough of the Notion API: data source queries, page create and
    update, users. Pages are kept in Notion's own JSON shape."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.source_of: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.users = [{"id": "user-praise", "name": "Praise", "person": {"email": "praise@example.com"}}]
        self.fail_writes = False

    def add(self, source: str, **props: dict) -> str:
        page_id = str(uuid.uuid4())
        self.pages[page_id] = {"id": page_id, "url": f"https://notion.so/{page_id.replace('-', '')}", "properties": dict(props)}
        self.source_of[page_id] = source
        return page_id

    def _store(self, page_id: str, properties: dict) -> None:
        for name, value in properties.items():
            (kind,) = value.keys()
            body = value[kind]
            if kind == "people":
                names = {u["id"]: u["name"] for u in self.users}
                body = [{"id": p["id"], "name": names.get(p["id"], "")} for p in body]
            self.pages[page_id]["properties"][name] = {"type": kind, kind: body}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path.removeprefix("/v1")
        self.requests.append((request.method, path, body))
        if request.method == "GET" and path == "/users":
            return httpx.Response(200, json={"results": self.users, "has_more": False})
        if request.method == "POST" and path.startswith("/data_sources/"):
            source = path.split("/")[2]
            results = [p for pid, p in self.pages.items() if self.source_of[pid] == source]
            return httpx.Response(200, json={"results": results, "has_more": False})
        if self.fail_writes:
            return httpx.Response(503, json={"message": "service unavailable"})
        if request.method == "POST" and path == "/pages":
            page_id = self.add(body["parent"]["data_source_id"])
            self._store(page_id, body["properties"])
            return httpx.Response(200, json=self.pages[page_id])
        if request.method == "PATCH" and path.startswith("/pages/"):
            page_id = next(pid for pid in self.pages if pid.replace("-", "") == path.split("/")[2].replace("-", ""))
            self._store(page_id, body["properties"])
            return httpx.Response(200, json=self.pages[page_id])
        return httpx.Response(404, json={"message": f"no route {request.method} {path}"})

    def client(self) -> notion.Client:
        return notion.Client("secret", transport=httpx.MockTransport(self.handler))

    def prop(self, page_id: str, name: str):
        return notion.decode(self.pages[page_id]["properties"][name])


def title(text: str) -> dict:
    return {"type": "title", "title": [{"plain_text": text}]}


def text(value: str) -> dict:
    return {"type": "rich_text", "rich_text": [{"plain_text": value}] if value else []}


def status(name: str) -> dict:
    return {"type": "status", "status": {"name": name}}


def relation(*ids: str) -> dict:
    return {"type": "relation", "relation": [{"id": i} for i in ids]}


@pytest.fixture
def fake():
    return FakeNotion()


@pytest.fixture
def mem(conn, fake):
    return service.Memory(conn=conn, config=config.Config(notion=NOTION), notion_client=fake.client())


# --- values and links --------------------------------------------------------


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://www.pinterest.com/team/moodboard/", "Pinterest"),
        ("https://pin.it/abc", "Pinterest"),
        ("https://www.figma.com/file/x/Design", "Figma"),
        ("https://youtu.be/xyz", "Video"),
        ("github.com/z10-labs/hippomem", "Repo"),
        ("https://docs.google.com/document/d/1", "Document"),
        ("https://example.com/spec.pdf", "Document"),
        ("https://cdn.example.com/logo.png", "Image"),
        ("https://example.com/blog/post", "Link"),
    ],
)
def test_resource_kind_comes_from_the_url(url, kind):
    assert workspace.detect_resource_kind(url) == kind


def test_task_status_is_checked_and_matched_case_insensitively(conn):
    task = workspace.create(conn, "task", "Add rate limiting", "@alex", status="in progress", priority="high")
    assert (task.id, task.status, task.priority) == ("T-0001", "In progress", "High")
    with pytest.raises(ValueError, match="task status must be one of"):
        workspace.create(conn, "task", "Bad", "@alex", status="Doing")


def test_links_take_one_direction_whichever_order_they_are_named(conn):
    project = workspace.create(conn, "project", "Checkout", "@alex")
    task = workspace.create(conn, "task", "Build cart", "@alex")
    doc = workspace.create(conn, "doc", "Cart spec", "@alex")
    decision = accepted_decision(conn, "Carts live in Postgres", "one database")

    assert workspace.link_shape(conn, project.id, task.id) == (task.id, "part-of", project.id)
    assert workspace.link_shape(conn, doc.id, task.id) == (task.id, "uses", doc.id)
    assert workspace.link_shape(conn, decision, doc.id) == (doc.id, "cites", decision)
    other = accepted_decision(conn, "Carts expire after a day", "storage")
    with pytest.raises(ValueError, match="through proposals"):
        workspace.link_shape(conn, decision, other)
    with pytest.raises(KeyError):
        workspace.link_shape(conn, task.id, "D-0099")


def test_project_context_has_open_tasks_docs_resources_and_cited_decisions(conn):
    decision = accepted_decision(conn, "Carts live in Postgres", "one database")
    project = workspace.create(conn, "project", "Checkout", "@alex", system_id="billing")
    open_task = workspace.create(conn, "task", "Build cart", "@alex")
    done_task = workspace.create(conn, "task", "Sketch cart", "@alex", status="Done")
    doc = workspace.create(conn, "doc", "Cart spec", "@alex")
    board = workspace.create(conn, "resource", "Checkout inspiration", "@alex", url="https://pinterest.com/team/checkout")
    for item in (open_task, done_task, doc, board):
        workspace.connect(conn, item.id, project.id, "@alex")
    workspace.connect(conn, open_task.id, decision, "@alex")

    ctx = workspace.context(conn, project="checkout")
    assert [t.id for t in ctx.tasks] == [open_task.id]
    assert [d.id for d in ctx.docs] == [doc.id]
    assert [r.category for r in ctx.resources] == ["Pinterest"]
    assert [d.id for d in ctx.decisions] == [decision]


def test_system_context_shows_its_items_and_decisions_in_force(conn):
    decision = accepted_decision(conn, "Billing owns money writes", "one writer", systems_=["billing"])
    workspace.create(conn, "task", "Refund flow", "@alex", system_id="billing")
    workspace.create(conn, "task", "Landing page", "@alex", system_id="web")
    ctx = workspace.context(conn, system="billing")
    assert [t.title for t in ctx.tasks] == ["Refund flow"]
    assert [d.id for d in ctx.decisions] == [decision]


def test_search_finds_items_by_meaning(conn):
    workspace.create(conn, "resource", "Colour palette moodboard", "@alex", url="https://pinterest.com/x", body="brand colours for the app")
    workspace.create(conn, "task", "Fix invoice rounding", "@alex")
    hits = workspace.search(conn, "brand colour moodboard")
    assert [i.title for i, _ in hits][:1] == ["Colour palette moodboard"]


# --- Notion sync -------------------------------------------------------------


def seed_notion(fake: FakeNotion, decision_id: str) -> dict[str, str]:
    project = fake.add("ds-projects", Name=title("Checkout"), Status=status("Active"), **{"Hippomem System": text("billing")})
    doc = fake.add("ds-docs", Name=title("Cart spec"), Project=relation(project), **{"Decisions": text(decision_id)})
    board = fake.add("ds-resources", Name=title("Moodboard"), URL={"type": "url", "url": "https://pinterest.com/b"}, Project=relation(project))
    task = fake.add(
        "ds-tasks",
        Name=title("Build cart"),
        Status=status("In progress"),
        Project=relation(project),
        Docs=relation(doc),
        Resources=relation(board),
        **{"Hippomem System": text("billing"), "Decisions": text(f"implements {decision_id}"), "Hippomem ID": text("")},
    )
    return {"project": project, "doc": doc, "board": board, "task": task}


def test_pull_brings_in_every_database_with_its_links(conn, fake):
    decision = accepted_decision(conn, "Carts live in Postgres", "one database")
    pages = seed_notion(fake, decision)

    report = notion.pull(conn, fake.client(), NOTION)

    assert len(report.created) == 4 and not report.errors
    task = workspace.find_by_notion(conn, pages["task"])
    assert task is not None and (task.status, task.system_id) == ("In progress", "billing")
    shapes = {
        (lk.type, workspace.get(conn, lk.to_id).kind if workspace.ITEM_ID.match(lk.to_id) else lk.to_id) for lk in workspace.links_of(conn, task.id)
    }
    assert shapes == {("part-of", "project"), ("uses", "doc"), ("uses", "resource"), ("cites", decision)}
    doc = workspace.find_by_notion(conn, pages["doc"])
    assert doc is not None and [i.id for i in workspace.for_decision(conn, decision)] == sorted([task.id, doc.id])
    # The page that has a Hippomem ID property now shows the item's id.
    assert fake.prop(pages["task"], "Hippomem ID") == task.id


def test_a_second_pull_changes_nothing_and_a_deleted_page_is_archived(conn, fake):
    decision = accepted_decision(conn, "Carts live in Postgres", "one database")
    pages = seed_notion(fake, decision)
    notion.pull(conn, fake.client(), NOTION)

    again = notion.pull(conn, fake.client(), NOTION)
    assert not again.created and not again.updated

    del fake.pages[pages["board"]]
    gone = notion.pull(conn, fake.client(), NOTION)
    board = workspace.get(conn, gone.archived[0])
    assert board.title == "Moodboard" and board.archived


def test_an_edit_in_notion_reaches_the_workspace(conn, fake):
    pages = seed_notion(fake, "D-0001")
    notion.pull(conn, fake.client(), NOTION)
    fake.pages[pages["task"]]["properties"]["Status"] = status("Done")
    report = notion.pull(conn, fake.client(), NOTION)
    task = workspace.find_by_notion(conn, pages["task"])
    assert task is not None and task.status == "Done" and task.id in report.updated


def test_agent_changes_are_pushed_with_relations_and_win_over_a_stale_pull(conn, fake):
    pages = seed_notion(fake, "D-0001")
    notion.pull(conn, fake.client(), NOTION)
    project = workspace.find_by_notion(conn, pages["project"])
    assert project is not None

    task = workspace.create(conn, "task", "Write cart tests", "@alex", status="To do", assignee="Praise")
    workspace.connect(conn, task.id, project.id, "@alex")
    # A pull before the push must not drop the local task's link or fields.
    notion.pull(conn, fake.client(), NOTION)
    assert workspace.get(conn, task.id).notion_pending

    report = notion.sync(conn, fake.client(), NOTION)
    assert task.id in report.pushed and not report.errors
    pushed = workspace.get(conn, task.id)
    assert pushed.notion_page_id and not pushed.notion_pending
    page_id = next(pid for pid in fake.pages if pid.replace("-", "") == pushed.notion_page_id)
    assert [p.replace("-", "") for p in fake.prop(page_id, "Project")] == [pages["project"].replace("-", "")]
    assert fake.prop(page_id, "Assignee") == "Praise"
    assert fake.prop(page_id, "Hippomem ID") == task.id
    # The link now lives in Notion, and survives the next pull.
    notion.pull(conn, fake.client(), NOTION)
    assert [lk.source for lk in workspace.links_of(conn, task.id) if lk.to_id == project.id] == ["notion"]


def test_unknown_assignee_never_clears_notion_people(conn, fake):
    task = workspace.create(conn, "task", "Mystery", "@alex", assignee="Nobody Known")
    props, _ = notion.item_properties(conn, task, {"praise": "user-praise"})
    assert "Assignee" not in props


# --- service and MCP surface ------------------------------------------------


def test_save_resource_detects_kind_pushes_to_notion_and_never_duplicates(mem, fake):
    first = service.save_resource(mem, ALEX, url="https://www.pinterest.com/team/brand/", title="Brand board", tags=["design"])
    assert first.startswith("Saved R-0001  Brand board  [resource · Pinterest")
    assert "Notion: https://notion.so/" in first
    second = service.save_resource(mem, ALEX, url="https://www.pinterest.com/team/brand/", tags=["inspiration"])
    assert second.startswith("Already saved; updated R-0001")
    assert workspace.get(mem.conn, "R-0001").tags == ("design", "inspiration")
    assert len([p for p in fake.source_of.values() if p == "ds-resources"]) == 1


def test_a_failed_notion_write_keeps_the_item_for_the_next_sync(mem, fake):
    fake.fail_writes = True
    text_ = service.save_task(mem, ALEX, title="Ship refunds", system="billing")
    assert "writing to Notion failed" in text_
    assert workspace.get(mem.conn, "T-0001").notion_pending
    fake.fail_writes = False
    assert "1 pushed to Notion" in service.workspace_sync(mem)


def test_save_task_links_project_by_title_and_reports_bad_links(mem):
    service.save_project(mem, ALEX, title="Checkout", system="billing")
    out = service.save_task(mem, ALEX, title="Build cart", project="checkout", links=["DOC-0042"])
    assert "! not linked to DOC-0042" in out
    assert workspace.context(mem.conn, project="Checkout").tasks[0].title == "Build cart"
    assert workspace.get(mem.conn, "T-0001").agent == "claude-code"


def test_a_decision_shows_the_work_that_cites_it(mem):
    decision = accepted_decision(mem.conn, "Carts live in Postgres", "one database")
    service.save_task(mem, ALEX, title="Build cart", links=[decision])
    assert "Work citing it: T-0001  Build cart" in service.get_decision(mem, ALEX, decision)


def test_session_digest_lists_open_tasks_for_the_repo_systems(conn):
    workspace.create(conn, "task", "Refund flow", "@alex", system_id="billing", priority="Low")
    workspace.create(conn, "task", "Fix double charge", "@alex", system_id="billing", priority="Urgent")
    workspace.create(conn, "task", "Old work", "@alex", system_id="billing", status="Done")
    workspace.create(conn, "task", "Landing page", "@alex", system_id="web")
    session = sessions.start(conn, ALEX, "s1", "product-worker", "main")
    text_ = sessions.digest(conn, config.Config().policy, ALEX, session, 2)
    block = text_.split("Open tasks for these systems (2;")[1]
    assert block.index("Fix double charge") < block.index("Refund flow")
    assert "Old work" not in text_ and "Landing page" not in text_


def test_digest_is_unchanged_when_the_workspace_is_empty(conn):
    session = sessions.start(conn, ALEX, "s1", "product-worker", "main")
    assert "Open tasks" not in sessions.digest(conn, config.Config().policy, ALEX, session, 2)


def test_without_notion_writes_stay_local(conn):
    mem = service.Memory(conn=conn, config=config.Config())
    out = service.save_task(mem, ALEX, title="Local only")
    assert "Notion" not in out.split("]")[1]
    assert service.workspace_sync(mem).startswith("Notion isn't configured")


def test_notion_config_reads_data_sources_and_refuses_a_token_in_the_file():
    parsed = config.parse({"notion": {"tasks": "abc", "sync_minutes": 2}})
    assert parsed.notion.enabled and parsed.notion.source("task") == "abc" and parsed.notion.sync_minutes == 2
    with pytest.raises(ValueError, match="environment variable"):
        config.parse({"notion": {"token": "secret_x"}})
    with pytest.raises(ValueError, match="unknown"):
        config.parse({"notion": {"taks": "abc"}})
