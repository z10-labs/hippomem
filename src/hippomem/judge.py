"""Asking a model to judge drafts against a diff, or sort a closed branch.

Everything that needs a model's judgement goes through `run`, so tests can
replace it. The default calls headless Claude Code (`claude -p`), isolated
from the engineer's own settings, hooks and MCP servers (BD-08).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

DEFAULT_MODEL = "sonnet"
TIMEOUT_SECONDS = 300
MAX_BUDGET_USD = "0.75"


class JudgeError(Exception):
    """The judge couldn't run or didn't return usable JSON."""


def run(prompt: str) -> dict:
    """Send `prompt` and return the JSON object in the reply."""
    claude = shutil.which(os.environ.get("HIPPOMEM_JUDGE_CLAUDE", "claude"))
    if claude is None:
        raise JudgeError("the `claude` CLI is not on PATH; set HIPPOMEM_JUDGE_CLAUDE")
    command = [
        claude,
        "-p",
        "--output-format",
        "json",
        "--model",
        os.environ.get("HIPPOMEM_JUDGE_MODEL", DEFAULT_MODEL),
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--max-budget-usd",
        MAX_BUDGET_USD,
        "--disallowedTools",
        "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
    ]
    with tempfile.TemporaryDirectory() as empty_dir:
        try:
            done = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=empty_dir)
        except subprocess.TimeoutExpired as err:
            raise JudgeError(f"judge timed out after {TIMEOUT_SECONDS}s") from err
    if done.returncode != 0:
        raise JudgeError(f"judge exited {done.returncode}: {done.stderr.strip()[:500]}")
    try:
        envelope = json.loads(done.stdout)
        return extract_json(envelope.get("result", ""))
    except (json.JSONDecodeError, AttributeError) as err:
        raise JudgeError(f"judge output was not JSON: {done.stdout[:500]}") from err


def extract_json(text: str) -> dict:
    """The first JSON object in a reply, with or without a ```json fence."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise JudgeError(f"no JSON object in judge reply: {text[:300]}")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as err:
        raise JudgeError(f"judge reply had malformed JSON: {err}") from err
    if not isinstance(value, dict):
        raise JudgeError("judge reply was not a JSON object")
    return value
