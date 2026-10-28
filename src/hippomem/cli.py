"""The `hippomem` command.

Admin commands (init, serve, actor, map, config) work on the memory file
directly. Everything else talks to the running server over HTTP with the
caller's token, from $HIPPOMEM_TOKEN or the `token` file in the Hippomem home.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from hippomem import __version__, actors, db, hooks, systems
from hippomem import config as cfg


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home) if args.home else cfg.home()


def _open(args: argparse.Namespace) -> tuple[Any, cfg.Config]:
    home = _home(args)
    config = cfg.load(cfg.config_path(home))
    return db.connect(cfg.db_path(home)), config


def _token(args: argparse.Namespace) -> str:
    token = os.environ.get("HIPPOMEM_TOKEN", "")
    token_file = _home(args) / "token"
    if not token and token_file.exists():
        token = token_file.read_text().strip()
    if not token:
        raise SystemExit("No token. Set HIPPOMEM_TOKEN or run `hippomem login <token>`.")
    return token


def _client(args: argparse.Namespace) -> httpx.Client:
    config = cfg.load(cfg.config_path(_home(args)))
    url = os.environ.get("HIPPOMEM_URL", config.url)
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {_token(args)}"}, timeout=60)


def _call(args: argparse.Namespace, method: str, path: str, **kwargs: Any) -> dict:
    try:
        with _client(args) as client:
            response = client.request(method, path, **kwargs)
    except httpx.ConnectError:
        raise SystemExit("Can't reach the Hippomem server. Is `hippomem serve` running?")
    data = response.json()
    if response.status_code >= 400:
        raise SystemExit(f"Error: {data.get('error', response.text)}")
    return data


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


# --- admin commands -------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    home = _home(args)
    home.mkdir(parents=True, exist_ok=True)
    if not cfg.config_path(home).exists():
        cfg.config_path(home).write_text(cfg.TEMPLATE)
    conn = db.connect(cfg.db_path(home))
    print(f"Memory at {cfg.db_path(home)}, config at {cfg.config_path(home)}")
    if args.admin and actors.get(conn, args.admin) is None:
        token = actors.add(conn, args.admin, "admin")
        _write_private(home / "token", token)
        print(f"Created admin {args.admin}; token saved to {home / 'token'}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hippomem import server, service

    from hippomem import lifecycle

    conn, config = _open(args)
    cfg.apply(conn, config)
    closed = lifecycle.close_stale(conn, config.stale_after_days)
    if closed:
        print(f"Closed {len(closed)} stale branches: {', '.join(closed)}")
    mem = service.Memory(conn=conn, config=config)
    app = server.create(mem).streamable_http_app()
    print(f"Hippomem serving on {config.url} (MCP at {config.url}/mcp)")
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")
    return 0


def cmd_actor_add(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    token = actors.add(conn, args.id, args.kind, args.name or "", tuple(args.email or ()))
    print(token)
    print(f"Created {args.kind} {args.id}. This token is shown once.", file=sys.stderr)
    return 0


def cmd_actor_list(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    for row in actors.list_all(conn):
        print(f"{row['id']}\t{row['kind']}\t{row['name']}")
    return 0


def cmd_config_apply(args: argparse.Namespace) -> int:
    conn, config = _open(args)
    cfg.apply(conn, config)
    print(f"Applied {len(config.systems)} systems from {cfg.config_path(_home(args))}")
    return 0


def cmd_map_add(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    if not systems.system_exists(conn, args.system):
        raise SystemExit(f"Unknown system {args.system!r}. Declare it in hippomem.toml and run `hippomem config apply`.")
    systems.add_mapping(conn, args.repo, args.glob, args.system, created_by="cli")
    print(f"Mapped {args.repo}:{args.glob} -> {args.system}")
    return 0


def cmd_map_list(args: argparse.Namespace) -> int:
    conn, _ = _open(args)
    for m in systems.mappings(conn, repo=args.repo):
        print(f"{m.repo}:{m.path_glob}\t{m.system_id}\t{m.status}")
    return 0


# --- client commands ------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    _write_private(_home(args) / "token", args.token.strip())
    print(f"Token saved to {_home(args) / 'token'}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    data = _call(args, "GET", "/api/whoami")
    print(f"{data['id']} ({data['kind']})")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    body = {"text": " ".join(args.text), "repo": args.repo or "", "path": args.path or "", "branch": args.branch or ""}
    print(_call(args, "POST", "/api/query", json=body)["text"])
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    print(_call(args, "GET", "/api/decision", params={"id": args.id})["text"])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    print(_call(args, "GET", "/api/decisions", params={"system": args.system or "", "all": "1" if args.all else ""})["text"])
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    pending = _call(args, "GET", "/api/proposals")["proposals"]
    print("\n\n".join(p["text"] for p in pending) if pending else "No proposals waiting.")
    return 0


def _decide(args: argparse.Namespace, accept: bool) -> int:
    print(_call(args, "POST", "/api/decide", json={"proposal_id": args.id, "accept": accept, "note": args.note or ""})["text"])
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    return _decide(args, True)


def cmd_reject(args: argparse.Namespace) -> int:
    return _decide(args, False)


# --- hooks and installation -------------------------------------------------


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.run_claude_hook(args.name, _home(args))


def cmd_git_event(args: argparse.Namespace) -> int:
    return hooks.run_git_event(args.kind, _home(args), args.args)


def cmd_install_git_hooks(args: argparse.Namespace) -> int:
    for path in hooks.install_git_hooks(args.repo, _home(args), force=args.force):
        print(f"wrote {path}")
    return 0


def cmd_install_claude_code(args: argparse.Namespace) -> int:
    if args.print:
        url = cfg.load(cfg.config_path(_home(args))).url
        print(json.dumps({".mcp.json": hooks.mcp_config(url), ".claude/settings.local.json": hooks.claude_settings(_home(args))}, indent=2))
        return 0
    for path in hooks.install_claude_code(args.project, _home(args)):
        print(f"wrote {path}")
    print("Set HIPPOMEM_TOKEN in your shell (or run `hippomem login`) before starting Claude Code.")
    return 0


def cmd_branch_close(args: argparse.Namespace) -> int:
    from hippomem import gitinfo

    repo = args.repo or gitinfo.repo_name(os.getcwd())
    print(json.dumps(_call(args, "POST", "/api/event", json={"kind": "close", "repo": repo, "branch": args.branch}), indent=2))
    return 0


def cmd_branch_close_stale(args: argparse.Namespace) -> int:
    print(json.dumps(_call(args, "POST", "/api/event", json={"kind": "close_stale"}), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hippomem", description="A team's engineering memory for AI coding agents.")
    parser.add_argument("--version", action="version", version=f"hippomem {__version__}")
    parser.add_argument("--home", help="Hippomem home folder (default: $HIPPOMEM_HOME or ~/.hippomem)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create the memory file and config")
    p.add_argument("--admin", help="create this admin actor (e.g. @you) and save its token")
    p.set_defaults(func=cmd_init)

    sub.add_parser("serve", help="run the server").set_defaults(func=cmd_serve)

    actor = sub.add_parser("actor", help="manage engineers, reviewer agents and admins").add_subparsers(dest="actor_command", required=True)
    p = actor.add_parser("add", help="create an actor and print its token")
    p.add_argument("id")
    p.add_argument("--kind", choices=actors.KINDS, default="engineer")
    p.add_argument("--name")
    p.add_argument("--email", action="append", help="git author email (repeatable), so questions about their code reach them")
    p.set_defaults(func=cmd_actor_add)
    actor.add_parser("list").set_defaults(func=cmd_actor_list)

    config = sub.add_parser("config", help="team config").add_subparsers(dest="config_command", required=True)
    config.add_parser("apply", help="create the systems, owners and mappings in hippomem.toml").set_defaults(func=cmd_config_apply)

    mapping = sub.add_parser("map", help="repo-to-system mappings").add_subparsers(dest="map_command", required=True)
    p = mapping.add_parser("add", help="map a repo folder to a system (confirmed)")
    p.add_argument("repo")
    p.add_argument("glob", help="path glob, ** for the whole repo")
    p.add_argument("system")
    p.set_defaults(func=cmd_map_add)
    p = mapping.add_parser("list")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_map_list)

    p = sub.add_parser("login", help="save your token")
    p.add_argument("token")
    p.set_defaults(func=cmd_login)
    sub.add_parser("whoami").set_defaults(func=cmd_whoami)

    p = sub.add_parser("query", help="search decisions")
    p.add_argument("text", nargs="+")
    p.add_argument("--repo")
    p.add_argument("--path")
    p.add_argument("--branch")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("show", help="one decision in full")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="decisions in force")
    p.add_argument("--system")
    p.add_argument("--all", action="store_true", help="include superseded and retired")
    p.set_defaults(func=cmd_list)

    sub.add_parser("review", help="open proposals").set_defaults(func=cmd_review)
    for name, func in (("accept", cmd_accept), ("reject", cmd_reject)):
        p = sub.add_parser(name, help=f"{name} a proposal")
        p.add_argument("id")
        p.add_argument("--note")
        p.set_defaults(func=func)
    p = sub.add_parser("hook", help="run a Claude Code hook (reads the hook JSON on stdin)")
    p.add_argument("name", choices=sorted(hooks.CLAUDE_HOOKS))
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("git-event", help="send a git event (called by the git hooks)")
    p.add_argument("kind", choices=("checkout", "commit", "merge"))
    p.add_argument("args", nargs="*")
    p.set_defaults(func=cmd_git_event)

    install = sub.add_parser("install", help="install hooks").add_subparsers(dest="install_command", required=True)
    p = install.add_parser("git-hooks", help="post-commit, post-merge and post-checkout hooks in a repo")
    p.add_argument("--repo", default=".")
    p.add_argument("--force", action="store_true", help="overwrite existing hooks that aren't Hippomem's")
    p.set_defaults(func=cmd_install_git_hooks)
    p = install.add_parser("claude-code", help="MCP server and hooks for Claude Code in a project")
    p.add_argument("--project", default=".")
    p.add_argument("--print", action="store_true", help="print the configuration instead of writing it")
    p.set_defaults(func=cmd_install_claude_code)

    branch = sub.add_parser("branch", help="branch lifecycle").add_subparsers(dest="branch_command", required=True)
    p = branch.add_parser("close", help="a branch was abandoned: sort its drafts into explored records")
    p.add_argument("branch")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_branch_close)
    branch.add_parser("close-stale", help="close branches inactive longer than branches.stale_after_days").set_defaults(func=cmd_branch_close_stale)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
