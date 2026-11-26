"""The team's workspace: projects, tasks, docs and resources, and how they connect.

Workspace items are the context work happens in, mirrored from Notion
(`notion.py`) so every agent connected to Hippomem can find them. They are
never decisions: nothing here is reviewed or put in force. An item can cite a
decision (a task implements D-0003, a doc explains it), which is how the two
halves of the memory meet.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

import numpy as np

from hippomem import db, decisions, embedding

KINDS = ("project", "task", "doc", "resource")
PREFIX = {"project": "PRJ", "task": "T", "doc": "DOC", "resource": "R"}
ITEM_ID = re.compile(r"^(PRJ|T|DOC|R)-\d{4,}$")
DECISION_ID = re.compile(r"\bD-\d{4,}\b")

# Option names match the Notion databases. Agents' writes are checked against
# them; values pulled from Notion are taken as they are, so a team can add
# options in Notion without breaking the sync.
STATUSES = {
    "project": ("Planned", "Active", "Paused", "Done"),
    "task": ("Backlog", "To do", "In progress", "In review", "Blocked", "Done"),
    "doc": ("Draft", "In review", "Published", "Outdated"),
    "resource": (),
}
DEFAULT_STATUS = {"project": "Planned", "task": "To do", "doc": "Draft", "resource": ""}
CLOSED_STATUSES = frozenset({"done", "outdated"})
PRIORITIES = ("Urgent", "High", "Medium", "Low")
DOC_TYPES = ("Spec", "Guide", "Runbook", "Notes", "Research", "ADR")
RESOURCE_KINDS = ("Link", "Pinterest", "Figma", "Canva", "Video", "Document", "Repo", "Image", "Notion")

# Domain suffix -> resource kind, checked in order.
_KIND_BY_HOST = (
    (("pinterest.com", "pin.it"), "Pinterest"),
    (("figma.com",), "Figma"),
    (("canva.com", "canva.link"), "Canva"),
    (("youtube.com", "youtu.be", "vimeo.com", "loom.com"), "Video"),
    (("github.com", "gitlab.com", "bitbucket.org"), "Repo"),
    (("notion.so", "notion.site", "notion.com"), "Notion"),
    (("docs.google.com", "drive.google.com", "dropbox.com", "onedrive.live.com", "sharepoint.com"), "Document"),
)
_KIND_BY_EXTENSION = (
    ((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".md", ".txt"), "Document"),
    ((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"), "Image"),
    ((".mp4", ".mov", ".webm"), "Video"),
)

MIN_SCORE = 0.30  # same floor as decision search
OPEN_TASKS_IN_DIGEST = 5

# Ordering decides which end of a link is "from": a task uses a doc, a doc is
# part of a project, anything cites a decision.
_RANK = {"task": 0, "doc": 1, "resource": 2, "project": 3, "decision": 4}
EDITABLE = ("title", "status", "category", "url", "body", "system_id", "priority", "assignee", "agent", "due", "branch", "tags")


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    title: str
    status: str = ""
    category: str = ""
    url: str = ""
    body: str = ""
    system_id: str = ""
    priority: str = ""
    assignee: str = ""
    agent: str = ""
    due: str = ""
    branch: str = ""
    tags: tuple[str, ...] = ()
    archived: bool = False
    notion_page_id: str | None = None
    notion_url: str = ""
    notion_pending: bool = False
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_open(self) -> bool:
        return self.status.lower() not in CLOSED_STATUSES


@dataclass(frozen=True)
class Link:
    from_id: str
    type: str
    to_id: str
    source: str = "hippomem"


@dataclass(frozen=True)
class Context:
    """Everything connected to a project or a system."""

    project: Item | None
    system: str
    tasks: tuple[Item, ...] = ()
    docs: tuple[Item, ...] = ()
    resources: tuple[Item, ...] = ()
    projects: tuple[Item, ...] = ()
    decisions: tuple[decisions.Decision, ...] = ()
    links: tuple[Link, ...] = field(default=())

    @property
    def empty(self) -> bool:
        return not (self.project or self.tasks or self.docs or self.resources or self.projects or self.decisions)


# --- values -------------------------------------------------------------------


def detect_resource_kind(url: str) -> str:
    """Pinterest, Figma, Video, ... from a URL; "Link" when nothing more specific fits."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    for suffixes, kind in _KIND_BY_HOST:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return kind
    path = parsed.path.lower()
    for extensions, kind in _KIND_BY_EXTENSION:
        if path.endswith(extensions):
            return kind
    return "Link"


def _canonical(value: str, allowed: tuple[str, ...], what: str) -> str:
    """Match an option case-insensitively ("in progress" -> "In progress")."""
    if not value or not allowed:
        return value
    for option in allowed:
        if option.lower() == value.strip().lower():
            return option
    raise ValueError(f"{what} must be one of: {', '.join(allowed)} (got {value!r})")


def _validate(kind: str, fields: dict) -> dict:
    out = dict(fields)
    if "status" in out:
        out["status"] = _canonical(out["status"], STATUSES[kind], f"{kind} status")
    if "priority" in out:
        out["priority"] = _canonical(out["priority"], PRIORITIES, "priority")
    if "category" in out and kind == "doc":
        out["category"] = _canonical(out["category"], DOC_TYPES, "doc type")
    if "category" in out and kind == "resource":
        out["category"] = _canonical(out["category"], RESOURCE_KINDS, "resource kind")
    if "title" in out and not str(out["title"]).strip():
        raise ValueError("title can't be empty")
    return out


def _embedding_text(item: Item) -> str:
    return ". ".join(p for p in (item.title, item.category, item.body, " ".join(item.tags), item.system_id) if p)


# --- reading ------------------------------------------------------------------

_COLUMNS = (
    "id, kind, title, status, category, url, body, system_id, priority, assignee, agent, due, branch, tags, "
    "archived, notion_page_id, notion_url, notion_pending, created_by, created_at, updated_at"
)


def _from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        status=row["status"],
        category=row["category"],
        url=row["url"],
        body=row["body"],
        system_id=row["system_id"],
        priority=row["priority"],
        assignee=row["assignee"],
        agent=row["agent"],
        due=row["due"],
        branch=row["branch"],
        tags=tuple(json.loads(row["tags"] or "[]")),
        archived=bool(row["archived"]),
        notion_page_id=row["notion_page_id"],
        notion_url=row["notion_url"],
        notion_pending=bool(row["notion_pending"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get(conn: sqlite3.Connection, item_id: str) -> Item:
    row = conn.execute(f"SELECT {_COLUMNS} FROM workspace_item WHERE id = ?", (item_id.strip().upper(),)).fetchone()
    if row is None:
        raise KeyError(f"no workspace item {item_id}")
    return _from_row(row)


def find_by_notion(conn: sqlite3.Connection, page_id: str) -> Item | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM workspace_item WHERE notion_page_id = ?", (page_key(page_id),)).fetchone()
    return _from_row(row) if row else None


def find_by_url(conn: sqlite3.Connection, url: str, kind: str = "resource") -> Item | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM workspace_item WHERE kind = ? AND url = ? AND archived = 0 ORDER BY created_at LIMIT 1", (kind, url.strip())
    ).fetchone()
    return _from_row(row) if row else None


def find_project(conn: sqlite3.Connection, ref: str) -> Item:
    """A project by id (PRJ-0001) or by title, case-insensitive."""
    if ITEM_ID.match(ref.strip().upper()):
        item = get(conn, ref)
        if item.kind != "project":
            raise ValueError(f"{item.id} is a {item.kind}, not a project")
        return item
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM workspace_item WHERE kind = 'project' AND archived = 0 AND lower(title) = lower(?)", (ref.strip(),)
    ).fetchone()
    if row is None:
        raise KeyError(f"no project named {ref!r}")
    return _from_row(row)


def items(
    conn: sqlite3.Connection,
    kind: str = "",
    systems: tuple[str, ...] | list[str] = (),
    open_only: bool = False,
    include_archived: bool = False,
) -> list[Item]:
    sql = f"SELECT {_COLUMNS} FROM workspace_item WHERE 1 = 1"
    args: list[object] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if systems:
        sql += " AND system_id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(list(systems)))
    if not include_archived:
        sql += " AND archived = 0"
    found = [_from_row(r) for r in conn.execute(sql + " ORDER BY id", args)]
    return [i for i in found if i.is_open] if open_only else found


def links_of(conn: sqlite3.Connection, item_id: str) -> list[Link]:
    """Every link touching an item or decision, in either direction."""
    rows = conn.execute(
        "SELECT from_id, type, to_id, source FROM workspace_link WHERE from_id = ? OR to_id = ? ORDER BY from_id, to_id", (item_id, item_id)
    )
    return [Link(r["from_id"], r["type"], r["to_id"], r["source"]) for r in rows]


def neighbours(conn: sqlite3.Connection, item_id: str) -> list[str]:
    return [lk.to_id if lk.from_id == item_id else lk.from_id for lk in links_of(conn, item_id)]


def for_decision(conn: sqlite3.Connection, decision_id: str) -> list[Item]:
    """Tasks, docs and resources that cite a decision."""
    ids = [r[0] for r in conn.execute("SELECT from_id FROM workspace_link WHERE to_id = ? AND type = 'cites' ORDER BY from_id", (decision_id,))]
    return [i for i in (get(conn, x) for x in ids) if not i.archived]


def search(conn: sqlite3.Connection, text: str, kind: str = "", top_n: int = 8) -> list[tuple[Item, float]]:
    """Workspace items by meaning, above the relevance floor."""
    sql = "SELECT id, embedding FROM workspace_item WHERE archived = 0 AND embedding IS NOT NULL"
    args: list[object] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    q = embedding.normalized(text)
    scores = np.vstack([embedding.from_blob(r["embedding"]) for r in rows]) @ q
    ranked = sorted(zip((r["id"] for r in rows), scores.tolist()), key=lambda t: t[1], reverse=True)
    return [(get(conn, item_id), score) for item_id, score in ranked if score >= MIN_SCORE][:top_n]


def context(conn: sqlite3.Connection, project: str = "", system: str = "") -> Context:
    """What's connected to a project (by id or title) or to a system.

    For a project: its tasks, docs and resources, and the decisions they cite.
    For a system: every item tagged with it, and its decisions in force.
    Closed tasks are left out.
    """
    if project:
        p = find_project(conn, project)
        members = [get(conn, x) for x in neighbours(conn, p.id) if ITEM_ID.match(x)]
        members = [m for m in members if not m.archived]
        cited = {lk.to_id for m in [p, *members] for lk in links_of(conn, m.id) if lk.type == "cites"}
        return Context(
            project=p,
            system=p.system_id,
            tasks=tuple(m for m in members if m.kind == "task" and m.is_open),
            docs=tuple(m for m in members if m.kind == "doc"),
            resources=tuple(m for m in members if m.kind == "resource"),
            decisions=tuple(_decisions(conn, sorted(cited))),
            links=tuple(lk for m in [p, *members] for lk in links_of(conn, m.id)),
        )
    found = items(conn, systems=(system,)) if system else []
    in_force = [d for d in decisions.all_current(conn, ("accepted",)) if system and system in d.systems]
    return Context(
        project=None,
        system=system,
        tasks=tuple(i for i in found if i.kind == "task" and i.is_open),
        docs=tuple(i for i in found if i.kind == "doc"),
        resources=tuple(i for i in found if i.kind == "resource"),
        projects=tuple(i for i in found if i.kind == "project"),
        decisions=tuple(in_force),
    )


def _decisions(conn: sqlite3.Connection, ids: list[str]) -> list[decisions.Decision]:
    out = []
    for decision_id in ids:
        try:
            out.append(decisions.get(conn, decision_id))
        except KeyError:
            continue
    return out


def decision_exists(conn: sqlite3.Connection, decision_id: str) -> bool:
    return conn.execute("SELECT 1 FROM decision WHERE id = ?", (decision_id,)).fetchone() is not None


# --- writing ------------------------------------------------------------------


def _store(conn: sqlite3.Connection, item: Item) -> Item:
    conn.execute(
        """INSERT INTO workspace_item (id, kind, title, status, category, url, body, system_id, priority, assignee, agent, due, branch, tags,
                                       archived, notion_page_id, notion_url, notion_pending, created_by, created_at, updated_at, embedding)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               title = excluded.title, status = excluded.status, category = excluded.category, url = excluded.url, body = excluded.body,
               system_id = excluded.system_id, priority = excluded.priority, assignee = excluded.assignee, agent = excluded.agent,
               due = excluded.due, branch = excluded.branch, tags = excluded.tags, archived = excluded.archived,
               notion_page_id = excluded.notion_page_id, notion_url = excluded.notion_url, notion_pending = excluded.notion_pending,
               updated_at = excluded.updated_at, embedding = excluded.embedding""",
        (
            item.id,
            item.kind,
            item.title,
            item.status,
            item.category,
            item.url,
            item.body,
            item.system_id,
            item.priority,
            item.assignee,
            item.agent,
            item.due,
            item.branch,
            json.dumps(list(item.tags)),
            int(item.archived),
            item.notion_page_id,
            item.notion_url,
            int(item.notion_pending),
            item.created_by,
            item.created_at,
            item.updated_at,
            embedding.to_blob(embedding.normalized(_embedding_text(item))),
        ),
    )
    return item


def _clean(fields: dict) -> dict:
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValueError(f"unknown workspace fields: {', '.join(sorted(unknown))}")
    out = {k: v for k, v in fields.items() if v is not None}
    if "tags" in out:
        out["tags"] = tuple(str(t).strip() for t in out["tags"] if str(t).strip())
    for key, value in out.items():
        if key != "tags":
            out[key] = str(value).strip()
    return out


def create(conn: sqlite3.Connection, kind: str, title: str, created_by: str, pending: bool = True, **fields: object) -> Item:
    """A new item, made here (by an agent or engineer). `pending` marks it for
    writing to Notion on the next push."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    values = _validate(kind, _clean({"title": title, **fields}))
    values.setdefault("status", DEFAULT_STATUS[kind])
    if kind == "resource" and values.get("url") and not values.get("category"):
        values["category"] = detect_resource_kind(str(values["url"]))
    now = db.now()
    with db.transaction(conn):
        item = Item(
            id=db.next_id(conn, PREFIX[kind]), kind=kind, notion_pending=pending, created_by=created_by, created_at=now, updated_at=now, **values
        )
        return _store(conn, item)


def update(conn: sqlite3.Connection, item_id: str, pending: bool = True, **fields: object) -> Item:
    """Change the given fields; None leaves a field as it is."""
    current = get(conn, item_id)
    values = _validate(current.kind, _clean(fields))
    if not values:
        return current
    with db.transaction(conn):
        changed = replace(current, **values, updated_at=db.now(), notion_pending=current.notion_pending or pending)
        return _store(conn, changed)


def archive(conn: sqlite3.Connection, item_id: str) -> None:
    with db.transaction(conn):
        conn.execute("UPDATE workspace_item SET archived = 1, updated_at = ? WHERE id = ?", (db.now(), item_id))


def mark_pushed(conn: sqlite3.Connection, item_id: str, page_id: str, page_url: str) -> None:
    with db.transaction(conn):
        conn.execute(
            "UPDATE workspace_item SET notion_page_id = ?, notion_url = ?, notion_pending = 0 WHERE id = ?",
            (page_key(page_id), page_url, item_id),
        )


def upsert_from_notion(conn: sqlite3.Connection, kind: str, page_id: str, page_url: str, fields: dict) -> tuple[Item, str]:
    """Bring one Notion page in. Returns the item and what happened:
    "created", "updated", "unchanged", or "kept" (changed here and not yet
    pushed, so the local copy wins until the next push)."""
    values = _clean(fields)
    values.setdefault("title", "Untitled")
    if not values["title"]:
        values["title"] = "Untitled"
    existing = find_by_notion(conn, page_id)
    with db.transaction(conn):
        if existing is None:
            now = db.now()
            item = Item(
                id=db.next_id(conn, PREFIX[kind]),
                kind=kind,
                notion_page_id=page_key(page_id),
                notion_url=page_url,
                created_by="notion",
                created_at=now,
                updated_at=now,
                **values,
            )
            return _store(conn, item), "created"
        if existing.notion_pending:
            return existing, "kept"
        merged = replace(existing, **values, notion_url=page_url or existing.notion_url, archived=False)
        if merged == existing:
            return existing, "unchanged"
        return _store(conn, replace(merged, updated_at=db.now())), "updated"


def page_key(page_id: str) -> str:
    """Notion ids come with or without dashes; store one form."""
    return page_id.replace("-", "").lower()


def _kind_of(conn: sqlite3.Connection, ref: str) -> str:
    if DECISION_ID.fullmatch(ref):
        if not decision_exists(conn, ref):
            raise KeyError(f"no decision {ref}")
        return "decision"
    return get(conn, ref).kind


def link_shape(conn: sqlite3.Connection, a: str, b: str) -> tuple[str, str, str]:
    """(from, type, to) for two ids, in the one direction the store keeps.

    Callers name two things to connect; the shape follows from their kinds: a
    task *uses* a doc or resource, anything is *part-of* a project, anything
    *cites* a decision, and two things of the same kind *relate*.
    """
    a, b = a.strip().upper(), b.strip().upper()
    if a == b:
        raise ValueError("can't link an item to itself")
    ka, kb = _kind_of(conn, a), _kind_of(conn, b)
    if ka == kb == "decision":
        raise ValueError("decisions link to each other through proposals (hippomem_propose links=...)")
    if (_RANK[ka], a) > (_RANK[kb], b):
        a, b, ka, kb = b, a, kb, ka
    if ka == kb:
        return a, "relates", b
    if kb == "decision":
        return a, "cites", b
    if kb == "project":
        return a, "part-of", b
    return a, "uses", b


def connect(conn: sqlite3.Connection, a: str, b: str, created_by: str, source: str = "hippomem") -> Link:
    from_id, type_, to_id = link_shape(conn, a, b)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO workspace_link (from_id, type, to_id, source, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(from_id, to_id) DO NOTHING",
            (from_id, type_, to_id, source, created_by, db.now()),
        )
        if source == "hippomem":
            conn.execute("UPDATE workspace_item SET notion_pending = 1 WHERE id = ? AND notion_page_id IS NOT NULL", (from_id,))
    return Link(from_id, type_, to_id, source)


def disconnect(conn: sqlite3.Connection, a: str, b: str) -> bool:
    from_id, _, to_id = link_shape(conn, a, b)
    with db.transaction(conn):
        gone = conn.execute("DELETE FROM workspace_link WHERE from_id = ? AND to_id = ?", (from_id, to_id)).rowcount
        if gone:
            conn.execute("UPDATE workspace_item SET notion_pending = 1 WHERE id = ? AND notion_page_id IS NOT NULL", (from_id,))
    return bool(gone)


def replace_notion_links(conn: sqlite3.Connection, item_id: str, targets: list[str]) -> None:
    """Make the links Notion holds for an item exactly `targets`. Links that
    exist only in Hippomem (no Notion field holds them) are left alone."""
    with db.transaction(conn):
        conn.execute("DELETE FROM workspace_link WHERE source = 'notion' AND from_id = ?", (item_id,))
        for target in targets:
            try:
                from_id, type_, to_id = link_shape(conn, item_id, target)
            except (KeyError, ValueError):
                continue
            conn.execute(
                "INSERT INTO workspace_link (from_id, type, to_id, source, created_by, created_at) VALUES (?, ?, ?, 'notion', 'notion', ?) "
                "ON CONFLICT(from_id, to_id) DO UPDATE SET source = 'notion'",
                (from_id, type_, to_id, db.now()),
            )


def mark_links_synced(conn: sqlite3.Connection, item_id: str, to_ids: list[str]) -> None:
    with db.transaction(conn):
        for to_id in to_ids:
            conn.execute("UPDATE workspace_link SET source = 'notion' WHERE from_id = ? AND to_id = ?", (item_id, to_id))
