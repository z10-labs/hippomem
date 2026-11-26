"""Two-way sync between the workspace and the team's Notion databases.

Notion is where people see and edit the work; Hippomem is where agents read
it. A sync pushes what agents changed here (items marked pending) and then
pulls every page from the four databases, so after a sync both sides agree.

Talks to the Notion REST API directly (data sources, API version 2025-09-03)
with httpx. The token comes from an environment variable, never the config
file. Tests pass a client built on `httpx.MockTransport`.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from hippomem import workspace

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
HIPPOMEM_ID = "Hippomem ID"
MAX_TEXT = 2000  # Notion's limit for one rich text segment


@dataclass(frozen=True)
class NotionConfig:
    """The `[notion]` block of hippomem.toml: one data source id per database."""

    projects: str = ""
    tasks: str = ""
    docs: str = ""
    resources: str = ""
    token_env: str = "NOTION_TOKEN"
    sync_minutes: int = 5

    def source(self, kind: str) -> str:
        return {"project": self.projects, "task": self.tasks, "doc": self.docs, "resource": self.resources}[kind]

    @property
    def enabled(self) -> bool:
        return any((self.projects, self.tasks, self.docs, self.resources))

    def token(self) -> str:
        return os.environ.get(self.token_env, "")


@dataclass(frozen=True)
class Field:
    attr: str  # workspace.Item field
    prop: str  # Notion property name
    type: str  # Notion property type


# The four databases created for the team's workspace. Property names match
# the databases exactly; renaming one in Notion means changing it here.
FIELDS: dict[str, tuple[Field, ...]] = {
    "project": (
        Field("title", "Name", "title"),
        Field("status", "Status", "status"),
        Field("body", "Summary", "rich_text"),
        Field("system_id", "Hippomem System", "rich_text"),
        Field("due", "Target Date", "date"),
        Field("assignee", "Owner", "people"),
    ),
    "task": (
        Field("title", "Name", "title"),
        Field("status", "Status", "status"),
        Field("priority", "Priority", "select"),
        Field("assignee", "Assignee", "people"),
        Field("agent", "Agent", "rich_text"),
        Field("due", "Due", "date"),
        Field("system_id", "Hippomem System", "rich_text"),
        Field("branch", "Branch", "rich_text"),
    ),
    "doc": (
        Field("title", "Name", "title"),
        Field("category", "Type", "select"),
        Field("status", "Status", "select"),
        Field("system_id", "Hippomem System", "rich_text"),
        Field("url", "External URL", "url"),
        Field("assignee", "Owner", "people"),
    ),
    "resource": (
        Field("title", "Name", "title"),
        Field("url", "URL", "url"),
        Field("category", "Kind", "select"),
        Field("tags", "Tags", "multi_select"),
        Field("body", "Notes", "rich_text"),
        Field("system_id", "Hippomem System", "rich_text"),
        Field("agent", "Added By", "rich_text"),
    ),
}

# Relation properties, read from the side that holds them (the "from" end of
# a workspace link). Notion keeps the other side in step (two-way relations).
RELATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "task": (("Project", "project"), ("Docs", "doc"), ("Resources", "resource")),
    "doc": (("Project", "project"),),
    "resource": (("Project", "project"),),
    "project": (),
}
# A text property listing cited decision ids ("D-0003, D-0007").
DECISIONS_PROP = {"task": "Decisions", "doc": "Decisions"}


class NotionError(Exception):
    pass


class Client:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, retry_wait: float = 1.0) -> None:
        if not token:
            raise NotionError("no Notion token: set NOTION_TOKEN (or [notion] token_env) to an integration token")
        self.http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"},
            transport=transport,
            timeout=30,
        )
        self.retry_wait = retry_wait
        self._users: list[dict] | None = None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        for attempt in range(3):
            response = self.http.request(method, path, **kwargs)
            if response.status_code == 429 and attempt < 2:
                time.sleep(float(response.headers.get("retry-after", self.retry_wait)))
                continue
            if response.status_code >= 400:
                try:
                    message = response.json().get("message", response.text)
                except ValueError:
                    message = response.text
                raise NotionError(f"Notion {method} {path}: {response.status_code} {message}")
            return response.json()
        raise NotionError(f"Notion {method} {path}: rate limited")

    def query(self, data_source_id: str) -> Iterator[dict]:
        body: dict[str, Any] = {"page_size": 100}
        while True:
            page = self._request("POST", f"/data_sources/{data_source_id}/query", json=body)
            yield from page.get("results", [])
            if not page.get("has_more"):
                return
            body["start_cursor"] = page["next_cursor"]

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        return self._request(
            "POST", "/pages", json={"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties}
        )

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def users(self) -> list[dict]:
        if self._users is None:
            found: list[dict] = []
            params: dict[str, Any] = {"page_size": 100}
            while True:
                page = self._request("GET", "/users", params=params)
                found.extend(page.get("results", []))
                if not page.get("has_more"):
                    break
                params["start_cursor"] = page["next_cursor"]
            self._users = found
        return self._users


def client_for(config: NotionConfig, transport: httpx.BaseTransport | None = None) -> Client | None:
    """A client when the config names databases and a token is set; else None."""
    if not config.enabled or not config.token():
        return None
    return Client(config.token(), transport)


# --- properties ---------------------------------------------------------------


def _plain(rich: list[dict]) -> str:
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in rich or ())


def decode(prop: dict) -> Any:
    """One Notion property value as plain data."""
    kind = prop.get("type", "")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return _plain(value or [])
    if kind in ("status", "select"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return [o["name"] for o in value or ()]
    if kind == "url":
        return value or ""
    if kind == "date":
        return (value or {}).get("start", "") or ""
    if kind == "people":
        return ", ".join(p.get("name") or p.get("person", {}).get("email", "") or p.get("id", "") for p in value or ())
    if kind == "relation":
        return [r["id"] for r in value or ()]
    if kind == "unique_id":
        v = value or {}
        prefix = v.get("prefix") or ""
        return f"{prefix}-{v.get('number', '')}" if prefix else str(v.get("number", ""))
    return ""


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value[:MAX_TEXT]}}] if value else []


def encode(type_: str, value: Any, people: dict[str, str] | None = None) -> dict | None:
    """A Notion property value; None means "leave this property alone"."""
    if type_ == "title":
        return {"title": _text(value)}
    if type_ == "rich_text":
        return {"rich_text": _text(value)}
    if type_ == "status":
        return {"status": {"name": value}} if value else None  # a status can't be cleared
    if type_ == "select":
        return {"select": {"name": value} if value else None}
    if type_ == "multi_select":
        return {"multi_select": [{"name": v} for v in value]}
    if type_ == "url":
        return {"url": value or None}
    if type_ == "date":
        return {"date": {"start": value} if value else None}
    if type_ == "people":
        names = [n.strip() for n in str(value).split(",") if n.strip()]
        ids = [people[n.lower()] for n in names if people and n.lower() in people]
        # Only write people we could find; an unknown name never wipes Notion's.
        return {"people": [{"id": i} for i in ids]} if len(ids) == len(names) else None
    raise ValueError(f"unknown Notion property type {type_}")


def _people_index(client: Client) -> dict[str, str]:
    index = {}
    for user in client.users():
        if user.get("name"):
            index[user["name"].lower()] = user["id"]
        email = user.get("person", {}).get("email")
        if email:
            index[email.lower()] = user["id"]
    return index


def page_fields(kind: str, page: dict) -> dict:
    props = page.get("properties", {})
    out: dict[str, Any] = {}
    for f in FIELDS[kind]:
        if f.prop in props:
            out[f.attr] = decode(props[f.prop])
    return out


def page_targets(kind: str, page: dict) -> tuple[list[str], list[str]]:
    """(related Notion page ids, cited decision ids) held on a page."""
    props = page.get("properties", {})
    pages = [pid for prop, _ in RELATIONS[kind] if prop in props for pid in decode(props[prop])]
    cited: list[str] = []
    if kind in DECISIONS_PROP and DECISIONS_PROP[kind] in props:
        cited = workspace.DECISION_ID.findall(decode(props[DECISIONS_PROP[kind]]))
    return pages, cited


def item_properties(conn: sqlite3.Connection, item: workspace.Item, people: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """The Notion properties for an item, and the link targets they carry."""
    props: dict[str, Any] = {}
    for f in FIELDS[item.kind]:
        if f.type == "people" and not item.assignee:
            continue
        value = encode(f.type, getattr(item, f.attr), people)
        if value is not None:
            props[f.prop] = value
    props[HIPPOMEM_ID] = encode("rich_text", item.id)

    carried: list[str] = []
    outgoing = [lk for lk in workspace.links_of(conn, item.id) if lk.from_id == item.id]
    for prop, to_kind in RELATIONS[item.kind]:
        targets = []
        for lk in outgoing:
            if not workspace.ITEM_ID.match(lk.to_id):
                continue
            other = workspace.get(conn, lk.to_id)
            if other.kind == to_kind and other.notion_page_id and not other.archived:
                targets.append(other)
        props[prop] = {"relation": [{"id": t.notion_page_id} for t in targets]}
        carried.extend(t.id for t in targets)
    if item.kind in DECISIONS_PROP:
        cited = [lk.to_id for lk in outgoing if lk.type == "cites"]
        props[DECISIONS_PROP[item.kind]] = encode("rich_text", ", ".join(cited))
        carried.extend(cited)
    return props, carried


# --- sync ---------------------------------------------------------------------


@dataclass
class Report:
    pushed: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{len(self.pushed)} pushed to Notion",
            f"{len(self.created)} new from Notion",
            f"{len(self.updated)} updated",
            f"{len(self.archived)} archived",
        ]
        if self.kept:
            parts.append(f"{len(self.kept)} kept (local changes not yet in Notion)")
        text = "Workspace sync: " + ", ".join(parts) + "."
        if self.errors:
            text += "\n" + "\n".join(f"  ! {e}" for e in self.errors)
        return text


def push(conn: sqlite3.Connection, client: Client, config: NotionConfig, item_id: str, people: dict[str, str] | None = None) -> workspace.Item:
    """Write one item to Notion, creating its page if it has none."""
    item = workspace.get(conn, item_id)
    source = config.source(item.kind)
    if not source:
        raise NotionError(f"no Notion database configured for {item.kind}s ([notion] {item.kind}s = ...)")
    if people is None and item.assignee:
        people = _people_index(client)
    props, carried = item_properties(conn, item, people)
    if item.notion_page_id:
        page = client.update_page(item.notion_page_id, props)
    else:
        page = client.create_page(source, props)
    workspace.mark_pushed(conn, item.id, page["id"], page.get("url", ""))
    workspace.mark_links_synced(conn, item.id, carried)
    return workspace.get(conn, item.id)


def push_pending(conn: sqlite3.Connection, client: Client, config: NotionConfig, report: Report | None = None) -> Report:
    report = report or Report()
    pending = [i for i in workspace.items(conn, include_archived=False) if i.notion_pending and config.source(i.kind)]
    # Projects first, then docs and resources, then tasks: a task's relations
    # can only point at pages that already exist.
    order = {"project": 0, "doc": 1, "resource": 1, "task": 2}
    people = _people_index(client) if any(i.assignee for i in pending) else None
    for item in sorted(pending, key=lambda i: order[i.kind]):
        try:
            push(conn, client, config, item.id, people)
            report.pushed.append(item.id)
        except NotionError as err:
            report.errors.append(f"{item.id}: {err}")
    return report


def pull(conn: sqlite3.Connection, client: Client, config: NotionConfig, report: Report | None = None) -> Report:
    """Read every page of every configured database into the workspace."""
    report = report or Report()
    fetched: dict[str, list[dict]] = {}
    for kind in workspace.KINDS:
        source = config.source(kind)
        if not source:
            continue
        try:
            fetched[kind] = [p for p in client.query(source) if not p.get("in_trash") and not p.get("archived")]
        except NotionError as err:
            report.errors.append(f"{kind}s: {err}")

    item_for_page: dict[str, workspace.Item] = {}
    for kind, pages in fetched.items():
        for page in pages:
            item, outcome = workspace.upsert_from_notion(conn, kind, page["id"], page.get("url", ""), page_fields(kind, page))
            item_for_page[workspace.page_key(page["id"])] = item
            {"created": report.created, "updated": report.updated, "kept": report.kept}.get(outcome, []).append(item.id)

    for kind, pages in fetched.items():
        for page in pages:
            item = item_for_page[workspace.page_key(page["id"])]
            if item.notion_pending:
                continue
            related, cited = page_targets(kind, page)
            targets = [item_for_page[workspace.page_key(p)].id for p in related if workspace.page_key(p) in item_for_page]
            targets += [d for d in cited if workspace.decision_exists(conn, d)]
            workspace.replace_notion_links(conn, item.id, targets)

    # Pages gone from a database that was read in full are archived here.
    seen = set(item_for_page)
    for kind in fetched:
        for item in workspace.items(conn, kind=kind):
            if item.notion_page_id and item.notion_page_id not in seen:
                workspace.archive(conn, item.id)
                report.archived.append(item.id)

    # Show each new item's Hippomem id in Notion, so people can quote it.
    for page in (p for pages in fetched.values() for p in pages):
        item = item_for_page[workspace.page_key(page["id"])]
        props = page.get("properties", {})
        if HIPPOMEM_ID in props and decode(props[HIPPOMEM_ID]) != item.id:
            try:
                client.update_page(page["id"], {HIPPOMEM_ID: encode("rich_text", item.id)})
            except NotionError as err:
                report.errors.append(f"{item.id}: {err}")
    return report


def sync(conn: sqlite3.Connection, client: Client, config: NotionConfig) -> Report:
    """Push local changes, then pull everything."""
    report = push_pending(conn, client, config)
    return pull(conn, client, config, report)
