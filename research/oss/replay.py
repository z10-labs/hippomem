"""R4: replay a real open-source project's history through Hippomem (E8).

GOV.UK's `alphagov/govuk-aws` recorded its architecture decisions as ADRs in
`docs/architecture/decisions/`. The replay walks every merge in a window,
oldest first, through Hippomem's real merge reconciliation and the real
judge, with the ADR files cut out of every diff and every history, so the
memory has to learn the decisions from the work, as a team without ADRs
would. There are no agents and no drafts: this measures the hardest path,
decisions recovered from merges alone. The ADRs, used as written, are the
answer key; nobody writes one by hand.

    python -m research.oss.replay --repo ~/.hippomem-research/r4/govuk-aws --out ~/.hippomem-research/r4/run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hippomem import db, drafts, policy, reconcile
from research import claude_shim, grader, ledger

ADR_DIR = "docs/architecture/decisions"
EXCLUDE = f":!{ADR_DIR}"
WINDOW = ("2017-07-01", "2017-09-15")
REPO_NAME = "govuk-aws"
MERGE_SUBJECT = re.compile(r"Merge pull request #(\d+) from [^/]+/(\S+)")
ADR_TEXT_CHARS = 500


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def merges(repo: Path) -> list[dict]:
    log = git(
        repo,
        "log",
        "--first-parent",
        "--merges",
        "--reverse",
        f"--since={WINDOW[0]}",
        f"--until={WINDOW[1]}",
        "--format=%H%x09%P%x09%ae%x09%aI%x09%s",
        "main",
    )
    out = []
    for line in log.splitlines():
        sha, parents, email, date, subject = line.split("\t", 4)
        p = parents.split()
        if len(p) != 2:
            continue
        m = MERGE_SUBJECT.match(subject)
        out.append(
            {
                "sha": sha,
                "base": p[0],
                "head": p[1],
                "email": email,
                "date": date,
                "pr": m.group(1) if m else "",
                "branch": m.group(2) if m else sha[:8],
            }
        )
    return out


def adrs(repo: Path) -> list[dict]:
    """The answer key: each ADR's title, date, status and decision text, as written."""
    records = []
    for path in sorted((repo / ADR_DIR).glob("[0-9][0-9][0-9][0-9]-*.md")):
        text = path.read_text()
        title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
        date = re.search(r"Date:\s*(\d{4}-\d{2}-\d{2})", text)
        status = re.search(r"##\s*Status\s*\n+\s*(.+)", text)
        decision = re.search(r"##\s*Decision\s*\n(.*?)(\n##\s|\Z)", text, re.S)
        context = re.search(r"##\s*Context\s*\n(.*?)(\n##\s|\Z)", text, re.S)
        records.append(
            {
                "id": f"ADR-{path.name[:4]}",
                "title": title,
                "date": date.group(1) if date else "",
                "status": status.group(1).strip() if status else "",
                "why": " ".join(((decision.group(1) if decision else "") + " Context: " + (context.group(1) if context else "")).split())[
                    :ADR_TEXT_CHARS
                ],
            }
        )
    return records


def judge_env(out: Path, book: ledger.Ledger) -> dict:
    launcher = claude_shim.write_launcher(out / "bin")
    import os
    import shutil

    return {
        "HIPPOMEM_JUDGE_CLAUDE": str(launcher),
        claude_shim.REAL_CLAUDE_ENV: shutil.which("claude") or "claude",
        ledger.LEDGER_ENV: str(book.path),
        ledger.PHASE_ENV: "R4",
        ledger.EXPERIMENT_ENV: "E8",
        ledger.KIND_ENV: "judge",
        **{k: v for k, v in os.environ.items() if k not in ()},
    }


def replay(repo: Path, out: Path, book: ledger.Ledger, limit: int = 0) -> None:
    import os

    os.environ.update(judge_env(out, book))
    conn = db.connect(out / "memory.db")
    log = out / "merges.jsonl"
    done = {json.loads(line)["sha"] for line in log.read_text().splitlines()} if log.exists() else set()
    rules = policy.Policy()
    todo = [m for m in merges(repo) if m["sha"] not in done]
    for i, m in enumerate(todo[: limit or None]):
        book.check(0.25, allow_past_ask=True)  # the owner approved going past $100 on 2026-11-06
        diff = git(repo, "diff", m["base"], m["sha"], "--", ".", EXCLUDE)
        record = {"sha": m["sha"], "pr": m["pr"], "date": m["date"], "proposals": [], "skipped": ""}
        if not diff.strip():
            record["skipped"] = "only ADR files"
        else:
            history = git(
                repo, "log", "--reverse", "--no-merges", "-p", "--format=--- commit %h %an%n%s%n%b", f"{m['base']}..{m['head']}", "--", ".", EXCLUDE
            )
            commits = [c for c in git(repo, "rev-list", f"{m['base']}..{m['head']}").split() if c]
            when = datetime.fromisoformat(m["date"]).astimezone(timezone.utc).strftime(db.TIME_FORMAT)
            db.now = lambda when=when: when  # type: ignore[assignment]  # the memory's clock follows the history
            branch_id = drafts.ensure_branch(conn, REPO_NAME, m["branch"])
            outcome = reconcile.merge(conn, rules, branch_id, diff, commits, history=history, merged_by=m["email"])
            record.update(proposals=list(outcome.proposals), judged=outcome.judged, error=outcome.error)
        with log.open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(f"[{i + 1}/{len(todo)}] PR {m['pr']} {m['date'][:10]}: {len(record['proposals'])} proposals {record['skipped']}", flush=True)


def grade(repo: Path, out: Path, book: ledger.Ledger) -> None:
    """Grade every proposal against the ADRs (the 'before review' measure)."""
    conn = db.connect(out / "memory.db")
    key = adrs(repo)
    g = grader.Grader(launcher=claude_shim.write_launcher(out / "bin"), book=book, phase="R4", experiment="E8")
    path = out / "grades.jsonl"
    done = {json.loads(line)["proposal"] for line in path.read_text().splitlines()} if path.exists() else set()
    for row in conn.execute("SELECT * FROM proposal ORDER BY id"):
        if row["id"] in done:
            continue
        book.check(0.1, allow_past_ask=True)
        payload = json.loads(row["payload"])
        text = "\n".join(
            [f"Title: {payload.get('title', '')}", f"Statement: {payload.get('statement', '')}", f"Why: {payload.get('why', '')}"]
            + [f"Rejected: {a['option']}: {a['reason']}" for a in payload.get("alternatives", [])]
        )
        try:
            result = grader.match_proposal(g, text, key, label=f"adr-{row['id']}")
        except grader.GraderError as err:
            result = {"error": str(err)}
        with path.open("a") as f:
            f.write(json.dumps({"proposal": row["id"], "kind": row["kind"], "inferred": bool(payload.get("inferred")), "grade": result}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research.oss.replay")
    parser.add_argument("command", choices=("replay", "grade", "adrs"))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ledger", default=str(ledger.default_path()))
    args = parser.parse_args(argv)
    book = ledger.Ledger(Path(args.ledger).expanduser())
    repo, out = args.repo.expanduser(), args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if args.command == "adrs":
        for a in adrs(repo):
            print(a["id"], a["date"], a["status"][:30], "|", a["title"])
        return 0
    try:
        (replay if args.command == "replay" else grade)(repo, out, book, *([args.limit] if args.command == "replay" else []))
    except ledger.BudgetExceeded as stop:
        print(f"stopped: {stop}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
