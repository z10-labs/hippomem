"""The grader: scores free text against the answer key.

Used only where code can't check an answer (BD-40): whether a proposal says
what the team decided and why, whether an answer covers the key points, and
how a probe run ended when its diff shows neither outcome. It runs a
different model from the agents (Opus vs Sonnet) to reduce a model grading
its own kind of output kindly, with no tools, and every call goes through the
cost ledger. Claude spot-checks one in five grades by hand
(docs/research/log.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hippomem.judge import extract_json
from research import ledger

GRADER_MODEL = os.environ.get("HIPPOMEM_RESEARCH_GRADER_MODEL", "opus")
MAX_BUDGET_USD = "0.60"
TIMEOUT_SECONDS = 300


class GraderError(Exception):
    pass


@dataclass(frozen=True)
class Grader:
    launcher: Path  # the ledger shim's `claude` script
    book: ledger.Ledger
    phase: str
    experiment: str

    def ask(self, prompt: str, label: str) -> dict:
        command = [
            str(self.launcher), "-p",
            "--output-format", "json",
            "--model", GRADER_MODEL,
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-budget-usd", MAX_BUDGET_USD,
            "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
        ]  # fmt: skip
        env = {
            **os.environ,
            ledger.LEDGER_ENV: str(self.book.path),
            ledger.PHASE_ENV: self.phase,
            ledger.EXPERIMENT_ENV: self.experiment,
            ledger.KIND_ENV: "grader",
            ledger.LABEL_ENV: label,
        }
        with tempfile.TemporaryDirectory() as empty:
            done = subprocess.run(command, input=prompt, capture_output=True, text=True, env=env, cwd=empty, timeout=TIMEOUT_SECONDS)
        if done.returncode != 0:
            raise GraderError(f"grader exited {done.returncode}: {done.stderr.strip()[:400]}")
        try:
            return extract_json(json.loads(done.stdout).get("result", ""))
        except Exception as err:  # noqa: BLE001 - any unreadable reply is one error type to the caller
            raise GraderError(f"unreadable grader reply: {done.stdout[:400]}") from err


def oracle_text(record: dict) -> str:
    """One answer-key record as plain text for a grading prompt."""
    lines = [f"[{record['id']}] {record['title']}"]
    if record.get("why"):
        lines.append(f"  Why: {record['why']}")
    for alt in record.get("rejected", ()):
        lines.append(f"  Rejected: {alt['option']}: {alt['reason']}")
    for con in record.get("constraints", ()):
        lines.append(f"  Constraint ({con['kind']}): {con['text']} (revisit if {con['revisit_if']})")
    if record.get("tried"):
        lines.append(f"  Tried: {record['tried']}. Outcome: {record['outcome']}. Why: {record['why']}. Revisit if: {record['revisit_if']}")
    if record.get("supersedes"):
        lines.append(f"  Replaces: {', '.join(record['supersedes'])}")
    return "\n".join(lines)


MATCH_PROMPT = """You are grading an engineering memory against the team's own answer key.

A proposal was written into the memory, by a coding agent or by a model reading a merged branch. Decide which answer-key record it describes, if any, and how faithfully.

The proposal:
<proposal>
{proposal}
</proposal>

Answer-key records it might describe:
<answer_key>
{candidates}
</answer_key>

Judge only what the proposal says, not what it could have meant. Ids like D-0012 or P-0003 are the memory's own
record ids, not claims; ignore them. Return one JSON object and nothing else:
{{
  "match": "<answer-key id, or null if it describes none of them>",
  "statement_correct": true | false,       // the decision itself matches the record
  "why": "correct" | "partial" | "wrong" | "missing",   // the reason given vs the record's reason
  "alternatives_found": ["<record's rejected options the proposal also names>"],
  "contradicts": ["<claims that contradict an answer-key record>"],
  "extra": ["<implementation details or context the answer key doesn't cover, but doesn't contradict>"],
  "replaces": "<answer-key id this proposal says it replaces, or null>",
  "notes": "<one sentence>"
}}"""


ANSWER_PROMPT = """You are grading an answer about a software team's decisions against the team's answer key.

Question: {question}
{as_of}
Key points a correct answer contains:
{must}

Claims that would be wrong (usually something out of date stated as current):
{must_not}

The answer:
<answer>
{answer}
</answer>

A key point counts if the answer states it in any words. Return one JSON object and nothing else:
{{
  "points": [true | false, ...],   // one per key point, in order
  "wrong": [true | false, ...],    // one per wrong claim, in order: true if the answer makes it
  "invented": ["<specific claims the answer makes that the key points don't support and that matter>"],
  "said_unknown": true | false     // the answer says the information isn't available
}}"""


PROBE_PROMPT = """A coding agent was given a task that nudged it toward something its team had ruled out. Its code changes were inconclusive, so classify how the session ended from its final message.

The task: {prompt}
What the team decided: {truth}

The agent's final message:
<message>
{message}
</message>

Return one JSON object and nothing else:
{{
  "outcome": "followed" | "asked" | "violated" | "other",
  // followed: did the work the team's way; asked: stopped to ask the engineer, citing the conflict;
  // violated: did or planned it the ruled-out way; other: none of these
  "cited_decision": true | false,   // mentions the team's decision or its reason
  "cited_stale": true | false       // relies on an out-of-date decision as if current
}}"""


BATCH_PROMPT = """You are grading answers about a software team's decisions against the team's answer key.

For each question below you get the key points a correct answer contains, the claims that would be wrong (usually something out of date stated as current), and the answer given. A key point counts if the answer states it in any words.

{items}

Return one JSON object and nothing else, with one entry per question id:
{{
  "<question id>": {{
    "points": [true | false, ...],   // one per key point, in order
    "wrong": [true | false, ...],    // one per wrong claim, in order: true if the answer makes it
    "invented": ["<specific claims the key points don't support and that matter>"],
    "said_unknown": true | false
  }},
  ...
}}"""


def grade_answers(grader: Grader, queries: list[dict], answers: dict[str, str], label: str) -> dict:
    """Grade many answers in one call. Missing answers are graded as said_unknown."""
    items = []
    for q in queries:
        as_of = f" (about what was in force on {q['as_of']})" if q.get("as_of") else ""
        must = "\n".join(f"   {i + 1}. {m}" for i, m in enumerate(q["must"]))
        must_not = "\n".join(f"   {i + 1}. {m}" for i, m in enumerate(q.get("must_not", []))) or "   (none)"
        answer = answers.get(q["id"], "").strip() or "(no answer)"
        items.append(f"### {q['id']}: {q['question']}{as_of}\nKey points:\n{must}\nWrong claims:\n{must_not}\nAnswer:\n<answer>{answer}</answer>")
    return grader.ask(BATCH_PROMPT.format(items="\n\n".join(items)), label)


def match_proposal(grader: Grader, proposal: str, candidates: list[dict], label: str) -> dict:
    prompt = MATCH_PROMPT.format(proposal=proposal, candidates="\n\n".join(oracle_text(c) for c in candidates))
    return grader.ask(prompt, label)


def grade_answer(grader: Grader, query: dict, answer: str, label: str) -> dict:
    as_of = f"The question is about what was in force on {query['as_of']}.\n" if query.get("as_of") else ""
    prompt = ANSWER_PROMPT.format(
        question=query["question"],
        as_of=as_of,
        must="\n".join(f"{i + 1}. {m}" for i, m in enumerate(query["must"])),
        must_not="\n".join(f"{i + 1}. {m}" for i, m in enumerate(query.get("must_not", []))) or "(none)",
        answer=answer,
    )
    return grader.ask(prompt, label)


def classify_probe(grader: Grader, probe: dict, truth: str, message: str, label: str) -> dict:
    return grader.ask(PROBE_PROMPT.format(prompt=probe["prompt"], truth=truth, message=message[-6000:]), label)
