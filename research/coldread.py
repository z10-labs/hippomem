"""Cold read (E7, with the as-of questions for E3): can a new agent explain
the system?

A fresh agent answers the whole query set in one session, three ways: from
the memory only (an empty folder and Hippomem's tools), from the repos only
(clones of all five, with git history), and from both. All answers are graded
against the answer key in one grader call per run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hippomem.judge import JudgeError, extract_json
from research import grader as grading
from research.team import Sandbox

ARMS = ("memory", "repos", "both")

PROMPT = """You've just joined Meridian's engineering team. Today is {today}. Answer the questions below about how Meridian's systems are built and why, using only what you can find {where}.

Answer each in one to three sentences. Say when something used to be different, and when you can't find out, say so rather than guessing.

{questions}

Finish with one JSON object mapping each question id to your answer, and nothing after it, like:
{{"Q01": "...", "Q02": "..."}}"""

WHERE = {
    "memory": "in the team's engineering memory (the hippomem tools)",
    "repos": "in the repositories in this folder, including their git history",
    "both": "in the team's engineering memory (the hippomem tools) and in the repositories in this folder, including their git history",
}


def workspace(base: Sandbox, root: Path, arm: str) -> Path:
    path = root / f"workspace-{arm}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    if arm != "memory":
        for repo in (base.root / "repos").iterdir():
            shutil.copytree(repo, path / repo.name, ignore=shutil.ignore_patterns("hooks"))
    return path


def run_one(base: Sandbox, queries: list[dict], arm: str, rep: int, today: str, grader: grading.Grader, out: Path) -> dict:
    root = base.root.parent / "coldread" / f"{arm}-{rep}"
    box = base.clone(root, experiment=base.experiment)
    cwd = workspace(box, root, arm)
    questions = "\n".join(f"{q['id']}. {q['question']}" for q in queries)
    prompt = PROMPT.format(today=today, where=WHERE[arm], questions=questions)
    with_memory = arm != "repos"
    if with_memory:
        box.start_server()
    try:
        run = box.run_agent("@alex", cwd, prompt, with_memory=with_memory, with_code=arm != "memory", label=f"cold-{arm}-{rep}")
    finally:
        box.stop_server()
    try:
        answers = {k: str(v) for k, v in extract_json(run.result).items()}
    except JudgeError:
        answers = {}
    record: dict = {"arm": arm, "rep": rep, "run_id": run.run_id, "cost_usd": run.cost_usd, "error": run.error, "answers": answers}
    try:
        record["grades"] = grading.grade_answers(grader, queries, answers, label=f"cold-{arm}-{rep}")
    except grading.GraderError as err:
        record["grade_error"] = str(err)
    with out.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record
