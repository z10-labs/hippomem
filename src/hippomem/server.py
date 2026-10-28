"""The Hippomem server: MCP over streamable HTTP at /mcp, JSON API at /api.

Every request carries `Authorization: Bearer <token>`. A coding agent sends
its engineer's token and names itself in `X-Hippomem-Agent`.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from hippomem import actors, render, service
from hippomem.policy import Actor

INSTRUCTIONS = """\
Hippomem is this team's engineering memory: the decisions behind the software, the options rejected,
and the dead ends already explored.

- Before a non-trivial choice (a library, a schema, an interface, where some logic lives, how services
  talk), call hippomem_query. Follow decisions in force; if you think one is wrong, say so to your
  engineer instead of quietly working around it.
- Explored records are dead ends, not decisions: check their "Revisit if" before trying the same thing.
- When you decide or reverse something, call hippomem_draft right away with the reason. Drafts are
  cheap and unreviewed; the merge decides which ones become proposals.
- A decision that covers a whole module, service or team and won't show up in a diff: ask your
  engineer, then hippomem_propose with scope "system" or "team".
- Only call hippomem_decide after your engineer has said yes or no to that proposal. Never accept on
  your own.
"""


class Unauthorized(Exception):
    pass


def actor_from_headers(mem: service.Memory, headers: Any) -> Actor:
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    with mem.lock:
        actor = actors.from_token(mem.conn, token, headers.get("x-hippomem-agent", ""))
    if actor is None:
        raise Unauthorized("missing or unknown token: set Authorization: Bearer <token>")
    return actor


def _ctx_actor(mem: service.Memory, ctx: Context) -> Actor:
    request = ctx.request_context.request
    if request is None:
        raise Unauthorized("Hippomem tools need an HTTP request with a token")
    return actor_from_headers(mem, request.headers)


def create(mem: service.Memory, allowed_hosts: tuple[str, ...] = ()) -> FastMCP:
    hosts = [f"{mem.config.host}:*", "127.0.0.1:*", "localhost:*", *allowed_hosts]
    mcp = FastMCP(
        "hippomem",
        instructions=INSTRUCTIONS,
        host=mem.config.host,
        port=mem.config.port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]),
    )
    _register_tools(mcp, mem)
    _register_api(mcp, mem)
    return mcp


def _register_tools(mcp: FastMCP, mem: service.Memory) -> None:
    @mcp.tool()
    def hippomem_query(text: str, ctx: Context, repo: str = "", path: str = "", branch: str = "") -> str:
        """Search the team's decisions, explored dead ends and in-progress drafts.

        Use before any non-trivial choice. `repo` and `branch` come from the
        session digest; pass `path` when the question is about one file.
        """
        return service.query(mem, _ctx_actor(mem, ctx), text, repo, path, branch)

    @mcp.tool()
    def hippomem_get(decision_id: str, ctx: Context) -> str:
        """Full record of one decision: why, rejected options, constraints, links, evidence, provenance."""
        return service.get_decision(mem, _ctx_actor(mem, ctx), decision_id)

    @mcp.tool()
    def hippomem_chain(decision_id: str, ctx: Context) -> str:
        """What a decision depends on, and everything that would be affected if it changed."""
        return service.chain(mem, _ctx_actor(mem, ctx), decision_id)

    @mcp.tool()
    def hippomem_list(ctx: Context, system: str = "") -> str:
        """Decisions in force, optionally for one system."""
        return service.list_decisions(mem, _ctx_actor(mem, ctx), system)

    @mcp.tool()
    def hippomem_systems(ctx: Context) -> str:
        """The team's systems, their owners and the repos and folders mapped to them."""
        return service.list_systems(mem, _ctx_actor(mem, ctx))

    @mcp.tool()
    def hippomem_propose(
        title: str,
        why: str,
        ctx: Context,
        statement: str = "",
        scope: str = "system",
        systems: list[str] | None = None,
        alternatives: list[dict] | None = None,
        constraints: list[dict] | None = None,
        links: list[dict] | None = None,
        weight: str = "standard",
        supersedes: str = "",
        repo: str = "",
        branch: str = "",
    ) -> str:
        """Propose a decision directly, for one that covers a module, service or
        team (scope "system" or "team") and won't show up in a diff. Ask your
        engineer first. Code-level decisions go through hippomem_draft instead.

        alternatives: [{"option": "...", "reason": "why rejected"}]
        constraints:  [{"text": "...", "kind": "technical|org|external", "revisit_if": "..."}]
        links:        [{"type": "depends-on|conflicts-with", "to": "D-0001"}]
        """
        actor = _ctx_actor(mem, ctx)
        try:
            p = service.propose_decision(
                mem,
                actor,
                title=title,
                why=why,
                statement=statement,
                scope=scope,
                systems_=systems,
                alternatives=alternatives,
                constraints=constraints,
                links=links,
                weight=weight,
                supersedes=supersedes,
                repo=repo,
                branch=branch,
            )
        except ValueError as err:
            return f"Not proposed: {err}"
        return f"Proposed {p.id}. It needs acceptance before it is in force.\n" + render.proposal_full(p)

    @mcp.tool()
    def hippomem_propose_mapping(repo: str, path_glob: str, system: str, ctx: Context) -> str:
        """Propose that a repo folder (glob, `**` for the whole repo) belongs to a system. An owner confirms it."""
        try:
            p = service.propose_mapping(mem, _ctx_actor(mem, ctx), repo, path_glob, system)
        except ValueError as err:
            return f"Not proposed: {err}"
        return f"Proposed {p.id}: {p.title}"

    @mcp.tool()
    def hippomem_review(ctx: Context) -> str:
        """Open proposals waiting for a decision."""
        actor = _ctx_actor(mem, ctx)
        pending = service.open_proposals(mem, actor)
        if not pending:
            return "No proposals waiting."
        return "\n\n".join(render.proposal_full(p) for p in pending)

    @mcp.tool()
    def hippomem_decide(proposal_id: str, accept: bool, ctx: Context, note: str = "") -> str:
        """Record your engineer's answer on a proposal. Only call this after they said yes or no."""
        return service.decide(mem, _ctx_actor(mem, ctx), proposal_id, accept, note)

    @mcp.tool()
    def hippomem_draft(
        title: str,
        why: str,
        ctx: Context,
        kind: str = "decided",
        statement: str = "",
        scope: str = "code",
        system: str = "",
        paths: list[str] | None = None,
        replaces: int | None = None,
        replaced_because: str = "",
        repo: str = "",
        branch: str = "",
        session: str = "",
    ) -> str:
        """Record a decision the moment you make it, with the reason. Cheap and unreviewed.

        kind: "decided" or "considering". To reverse an earlier draft, draft the new
        choice with replaces=<old draft id> and replaced_because=<why the old one was dropped>.
        scope: "code" (default), or "system"/"team" for a rule covering a whole module,
        service or team. paths: repo-relative files or folders the decision is about.
        """
        try:
            return service.draft(
                mem,
                _ctx_actor(mem, ctx),
                title=title,
                why=why,
                kind=kind,
                statement=statement,
                scope=scope,
                system=system,
                paths=paths,
                replaces=replaces,
                replaced_because=replaced_because,
                repo=repo,
                branch=branch,
                session_id=session,
            )
        except ValueError as err:
            return f"Not drafted: {err}"

    @mcp.tool()
    def hippomem_drop(draft_id: int, why: str, ctx: Context) -> str:
        """Drop an earlier draft without replacing it. Say why: that reason is what's worth keeping."""
        try:
            return service.drop(mem, _ctx_actor(mem, ctx), draft_id, why)
        except ValueError as err:
            return f"Not dropped: {err}"

    @mcp.tool()
    def hippomem_trail(ctx: Context, repo: str = "", branch: str = "", session: str = "") -> str:
        """The drafts on this branch so far, including what was dropped and why."""
        return service.trail(mem, _ctx_actor(mem, ctx), repo, branch, session)

    @mcp.tool()
    def hippomem_ask_author(repo: str, path: str, question: str, ctx: Context, author_email: str = "") -> str:
        """Ask the engineer who wrote a file why it's built the way it is, before changing how it works.

        Use when the before-edit lookup says no decision covers the file. Their answer
        comes back as a proposal anchored to the file.
        """
        history = {"top_author_email": author_email} if author_email else {}
        try:
            return service.ask_author(mem, _ctx_actor(mem, ctx), repo, path, question, history)
        except ValueError as err:
            return f"Not asked: {err}"

    @mcp.tool()
    def hippomem_answer(question_id: str, title: str, why: str, ctx: Context, alternatives: list[dict] | None = None) -> str:
        """Record your engineer's answer to a teammate's question about code they wrote.

        title: the decision in a few words. why: their reason. alternatives:
        [{"option": "...", "reason": "why rejected"}] if they mentioned any.
        """
        try:
            return service.answer_question(mem, _ctx_actor(mem, ctx), question_id, title, why, alternatives)
        except (ValueError, KeyError) as err:
            return f"Not recorded: {err}"


def _register_api(mcp: FastMCP, mem: service.Memory) -> None:
    def authed(handler: Any) -> Any:
        async def wrapped(request: Request) -> JSONResponse:
            try:
                actor = actor_from_headers(mem, request.headers)
            except Unauthorized as err:
                return JSONResponse({"error": str(err)}, status_code=401)
            body = await request.json() if request.method == "POST" else dict(request.query_params)
            try:
                return JSONResponse(handler(actor, body, request))
            except (ValueError, KeyError) as err:
                return JSONResponse({"error": str(err)}, status_code=400)

        return wrapped

    @mcp.custom_route("/api/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    def whoami(actor: Actor, body: dict, request: Request) -> dict:
        return {"id": actor.id, "kind": actor.kind, "agent": actor.agent}

    def query(actor: Actor, body: dict, request: Request) -> dict:
        return {"text": service.query(mem, actor, body["text"], body.get("repo", ""), body.get("path", ""), body.get("branch", ""))}

    def proposals_list(actor: Actor, body: dict, request: Request) -> dict:
        return {"proposals": [{"id": p.id, "text": render.proposal_full(p)} for p in service.open_proposals(mem, actor)]}

    def decide(actor: Actor, body: dict, request: Request) -> dict:
        return {"text": service.decide(mem, actor, body["proposal_id"], bool(body["accept"]), body.get("note", ""))}

    def get(actor: Actor, body: dict, request: Request) -> dict:
        return {"text": service.get_decision(mem, actor, body["id"])}

    def listing(actor: Actor, body: dict, request: Request) -> dict:
        return {"text": service.list_decisions(mem, actor, body.get("system", ""), bool(body.get("all", False)))}

    mcp.custom_route("/api/whoami", methods=["GET"])(authed(whoami))
    mcp.custom_route("/api/query", methods=["POST"])(authed(query))
    mcp.custom_route("/api/proposals", methods=["GET"])(authed(proposals_list))
    mcp.custom_route("/api/decide", methods=["POST"])(authed(decide))
    mcp.custom_route("/api/decision", methods=["GET"])(authed(get))
    mcp.custom_route("/api/decisions", methods=["GET"])(authed(listing))

    def session_start(actor: Actor, body: dict, request: Request) -> dict:
        return {"text": service.session_start(mem, actor, body["session_id"], body["repo"], body.get("branch", ""))}

    def lookup(actor: Actor, body: dict, request: Request) -> dict:
        return {"text": service.lookup(mem, actor, body.get("session_id", ""), body["repo"], body["path"], body.get("history"))}

    def turn_start(actor: Actor, body: dict, request: Request) -> dict:
        service.turn_start(mem, actor, body["session_id"])
        return {"ok": True}

    def turn_stop(actor: Actor, body: dict, request: Request) -> dict:
        return {"reason": service.turn_stop(mem, actor, body["session_id"], list(body.get("changed", [])))}

    def event(actor: Actor, body: dict, request: Request) -> dict:
        return service.git_event(mem, actor, body)

    mcp.custom_route("/api/session/start", methods=["POST"])(authed(session_start))
    mcp.custom_route("/api/lookup", methods=["POST"])(authed(lookup))
    mcp.custom_route("/api/turn/start", methods=["POST"])(authed(turn_start))
    mcp.custom_route("/api/turn/stop", methods=["POST"])(authed(turn_stop))
    mcp.custom_route("/api/event", methods=["POST"])(authed(event))
