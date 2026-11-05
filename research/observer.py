"""A Claude Code hook that records every tool call an agent makes.

Harness only, never part of the product. Each line in the events file holds
the hook event, the tool, its input, and the highest `usage` row id in the
memory at that moment. A usage row records which records a response showed
the agent (BD-42), so "decision D was in context before edit E" becomes:
some usage row of this session that showed D has an id at or below E's
`usage_seq`. The before-edit lookup for E itself runs alongside this hook,
so it may or may not be counted; metrics treat a decision first shown by the
lookup for the edit it bears on as late.

Never blocks the agent: any failure is swallowed and the hook exits 0.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

EVENTS_ENV = "HIPPOMEM_RESEARCH_EVENTS"
DB_ENV = "HIPPOMEM_RESEARCH_DB"
RUN_ENV = "HIPPOMEM_RESEARCH_RUN"
MAX_FIELD_CHARS = 20_000


def usage_seq(db_path: str) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM usage").fetchone()[0])
    finally:
        conn.close()


def trim(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + "…"
    if isinstance(value, dict):
        return {k: trim(v) for k, v in value.items()}
    return value


def event_line(payload: dict, seq: int, run: str) -> dict:
    return {
        "t": round(time.time(), 3),
        "run": run,
        "session": payload.get("session_id", ""),
        "event": payload.get("hook_event_name", ""),
        "tool": payload.get("tool_name", ""),
        "input": trim(payload.get("tool_input", {})),
        "usage_seq": seq,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        events = os.environ.get(EVENTS_ENV)
        if not events:
            return 0
        db_path = os.environ.get(DB_ENV, "")
        seq = usage_seq(db_path) if db_path and Path(db_path).exists() else -1
        line = event_line(payload, seq, os.environ.get(RUN_ENV, ""))
        with open(events, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 - an observer must never break the agent it observes
        pass
    return 0


def settings(python: str) -> dict:
    """Hook entries that run this observer before and after every tool call."""
    root = Path(__file__).resolve().parent.parent
    command = f'PYTHONPATH="{root}" "{python}" -m research.observer'
    entry = [{"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 10}]}]
    return {"hooks": {"PreToolUse": entry, "PostToolUse": entry}}


if __name__ == "__main__":
    sys.exit(main())
