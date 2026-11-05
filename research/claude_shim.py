"""Stands in for the `claude` CLI so every call's cost reaches the ledger.

Hippomem's judge runs inside the server, out of the harness's sight. The
harness points HIPPOMEM_JUDGE_CLAUDE at a script that runs this module; it
runs the real CLI with the same arguments and stdin, passes the output
through unchanged, and records `total_cost_usd` from the JSON envelope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from research import ledger

REAL_CLAUDE_ENV = "HIPPOMEM_RESEARCH_REAL_CLAUDE"


def cost_from_output(stdout: str) -> float:
    """total_cost_usd from a `--output-format json` envelope, or from the
    final `result` line of `stream-json`. 0 if there is none."""
    for line in reversed(stdout.strip().splitlines() or [""]):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "total_cost_usd" in data:
            return float(data["total_cost_usd"])
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return 0.0
    return float(data.get("total_cost_usd", 0.0)) if isinstance(data, dict) else 0.0


def write_launcher(directory: Path) -> Path:
    """An executable `claude` script that runs this shim with this interpreter."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    root = Path(__file__).resolve().parent.parent
    path.write_text(f'#!/bin/sh\nPYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" -m research.claude_shim "$@"\n')
    path.chmod(0o755)
    return path


def main() -> int:
    real = os.environ.get(REAL_CLAUDE_ENV) or shutil.which("claude")
    if not real:
        print("research shim: the real claude CLI was not found", file=sys.stderr)
        return 127
    done = subprocess.run([real, *sys.argv[1:]], stdin=sys.stdin, capture_output=True, text=True)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    book = ledger.from_env()
    if book is not None:
        book.record(
            phase=os.environ.get(ledger.PHASE_ENV, ""),
            experiment=os.environ.get(ledger.EXPERIMENT_ENV, ""),
            kind=os.environ.get(ledger.KIND_ENV, "judge"),
            label=os.environ.get(ledger.LABEL_ENV, ""),
            cost_usd=cost_from_output(done.stdout),
        )
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
