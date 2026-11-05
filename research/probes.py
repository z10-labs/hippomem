"""Probe runs (E4, E5) and decoys: the same task on the same snapshot, with
and without Hippomem, repeated.

Every run gets its own full copy of the end-of-timeline sandbox, so no run
sees another's drafts or code. Conformance is scored by code on the lines the
run added; the grader is used only when the diff is inconclusive. Timeliness
comes from the observer: was the decision the right answer rests on shown to
the agent before its first edit?
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from research import grader as grading
from research.team import Sandbox

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


@dataclass
class ProbeResult:
    probe: str
    arm: str  # memory | none
    rep: int
    run_id: str
    cost_usd: float
    turns: int
    error: str
    outcome: str = ""  # followed | violated | asked | other
    scored_by: str = ""  # code | grader
    violating_lines: list[str] = field(default_factory=list)
    conforming_lines: list[str] = field(default_factory=list)
    cited_decision: bool | None = None
    cited_stale: bool | None = None
    truth_ids: list[str] = field(default_factory=list)  # memory ids of the truth records
    shown_before_first_edit: list[str] = field(default_factory=list)
    shown_at_all: list[str] = field(default_factory=list)
    first_edit_seq: int = -1
    timely: bool | None = None
    memory_calls: int = 0
    files_touched: list[str] = field(default_factory=list)


def added_lines(box: Sandbox, repo: str) -> list[str]:
    box.git(repo, "add", "-A")
    diff = box.git(repo, "diff", "--cached", "main")
    committed = box.git(repo, "diff", "main...HEAD") if box.git(repo, "rev-parse", "--abbrev-ref", "HEAD") != "main" else ""
    lines = []
    for text in (diff, committed):
        lines += [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    return lines


def touched_files(box: Sandbox, repo: str) -> list[str]:
    box.git(repo, "add", "-A")
    names = set(box.git(repo, "diff", "--cached", "--name-only", "main").splitlines())
    return sorted(n for n in names if n)


def matching(lines: list[str], patterns: list[str]) -> list[str]:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    return [line.strip()[:200] for line in lines if any(c.search(line) for c in compiled)]


def memory_ids(box: Sandbox, matched: dict[str, str], oracle_ids: list[str]) -> list[str]:
    """Memory ids (D-/X-) for answer-key ids, through the proposals the timeline review accepted."""
    found = []
    for oid in oracle_ids:
        proposal_id = matched.get(oid)
        if proposal_id:
            row = box.rows("SELECT decision_id FROM decision_version WHERE proposal_id = ? ORDER BY version DESC LIMIT 1", proposal_id)
            if row:
                found.append(row[0]["decision_id"])
    return found


def shown_ids(box: Sandbox, after_seq: int, up_to_seq: int | None = None, skip_lookup_for: str = "", skip_after: int = -1) -> list[str]:
    """Every record id shown to the agent in usage rows after `after_seq` (and up
    to `up_to_seq`). A before-edit lookup for `skip_lookup_for` written after
    `skip_after` is the lookup for the edit being measured, so it doesn't count."""
    rows = box.rows("SELECT id, action, detail FROM usage WHERE id > ? ORDER BY id", after_seq)
    seen: list[str] = []
    for row in rows:
        if up_to_seq is not None and row["id"] > up_to_seq:
            break
        detail = json.loads(row["detail"] or "{}")
        if skip_lookup_for and row["action"] == "lookup" and row["id"] > skip_after and skip_lookup_for.endswith(str(detail.get("path", "\0"))):
            continue
        ids = list(detail.get("shown", []))
        ids += [h["id"] if isinstance(h, dict) else h for h in detail.get("decisions", [])]
        ids += [h["id"] if isinstance(h, dict) else h for h in detail.get("explored", [])]
        if isinstance(detail.get("decision"), str):
            ids.append(detail["decision"])
        seen += [i for i in ids if i not in seen]
    return seen


def run_one(base: Sandbox, probe: dict, arm: str, rep: int, at: str, matched: dict[str, str], grader: grading.Grader, out: Path) -> ProbeResult:
    root = base.root.parent / "probes" / f"{probe['id']}-{arm}-{rep}"
    box = base.clone(root, experiment=base.experiment)
    box.set_time(at)
    repo, engineer = probe["repo"], probe["engineer"]
    with_memory = arm == "memory"
    seq0 = int(box.rows("SELECT COALESCE(MAX(id), 0) AS m FROM usage")[0]["m"])
    if with_memory:
        box.start_server()
    try:
        box.checkout(repo, f"probe/{probe['id'].lower()}", engineer)
        run = box.run_agent(engineer, box.repo(repo), probe["prompt"], with_memory=with_memory, label=f"{probe['id']}-{arm}-{rep}")
    finally:
        box.stop_server()
    result = ProbeResult(probe=probe["id"], arm=arm, rep=rep, run_id=run.run_id, cost_usd=run.cost_usd, turns=run.turns, error=run.error)
    lines = added_lines(box, repo)
    result.files_touched = touched_files(box, repo)
    result.violating_lines = matching(lines, probe.get("violates", []))
    result.conforming_lines = matching(lines, probe.get("conforms", []))
    events = box.events(run.run_id)
    result.memory_calls = sum(1 for e in events if e["event"] == "PreToolUse" and e["tool"].startswith("mcp__hippomem"))
    _score_outcome(result, probe, run.result, grader)
    if with_memory:
        _score_timeliness(result, box, probe, matched, events, seq0)
    with out.open("a") as f:
        f.write(json.dumps(asdict(result)) + "\n")
    return result


def _score_outcome(result: ProbeResult, probe: dict, message: str, grader: grading.Grader) -> None:
    if result.violating_lines:
        result.outcome, result.scored_by = "violated", "code"
        return
    if result.conforming_lines and probe.get("conforms"):
        result.outcome, result.scored_by = "followed", "code"
        return
    if "truth" not in probe:
        return
    try:
        verdict = grading.classify_probe(grader, probe, ", ".join(probe["truth"]), message, label=f"probe-{result.run_id}")
    except grading.GraderError as err:
        result.outcome, result.scored_by = "other", f"grader error: {err}"
        return
    result.outcome, result.scored_by = str(verdict.get("outcome", "other")), "grader"
    result.cited_decision = verdict.get("cited_decision")
    result.cited_stale = verdict.get("cited_stale")


def _score_timeliness(result: ProbeResult, box: Sandbox, probe: dict, matched: dict[str, str], events: list[dict], seq0: int) -> None:
    truth = memory_ids(box, matched, [t for t in probe.get("truth", []) if t.startswith(("M", "X"))])
    result.truth_ids = truth
    result.shown_at_all = shown_ids(box, seq0)
    index = next((i for i, e in enumerate(events) if e["event"] == "PreToolUse" and e["tool"] in EDIT_TOOLS), None)
    if index is not None:
        first_edit = events[index]
        before = int(events[index - 1]["usage_seq"]) if index > 0 else seq0
        result.first_edit_seq = int(first_edit["usage_seq"])
        path = str(first_edit["input"].get("file_path") or first_edit["input"].get("notebook_path") or "")
        result.shown_before_first_edit = shown_ids(box, seq0, result.first_edit_seq, skip_lookup_for=path, skip_after=before)
        seen = result.shown_before_first_edit
    else:
        seen = result.shown_at_all
    result.timely = bool(truth) and any(t in seen for t in truth) if truth else None
