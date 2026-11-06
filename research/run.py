"""Run the research. See docs/research-plan.md and research/README.md.

    python -m research.run build     --sandbox ~/hm-research/r1
    python -m research.run timeline  --sandbox ~/hm-research/r1 [--weeks 2]
    python -m research.run probes    --sandbox ~/hm-research/r1 --reps 3 [--only P01 P02] [--arms memory none]
    python -m research.run decoys    --sandbox ~/hm-research/r1 --reps 2
    python -m research.run coldread  --sandbox ~/hm-research/r1 --reps 3 [--arms memory repos both]
    python -m research.run report    --sandbox ~/hm-research/r1 --out docs/research/r1.md
    python -m research.run spent

Every command that calls models checks the ledger first and refuses to start
if its estimate would pass the budget, or pass the point where the owner is
asked first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from research import coldread, grader, ledger, metrics, probes
from research.meridian import simulate, world
from research.team import Sandbox

PROBE_TIME = "2026-03-30T09:00:00Z"  # the Monday after week 12
COLD_READ_DAY = "30 March 2026"
ESTIMATE_PER_SESSION = 0.6
ESTIMATE_GRADE = 0.08


def timeline_box(args: argparse.Namespace, book: ledger.Ledger, experiment: str) -> Sandbox:
    box = Sandbox.load(Path(args.sandbox).expanduser() / "meridian", book, phase=args.phase, experiment=experiment)
    box.approved = args.approved
    return box


def make_grader(box: Sandbox) -> grader.Grader:
    return grader.Grader(launcher=box.root / "bin" / "claude", book=box.book, phase=box.phase, experiment=box.experiment)


def cmd_build(args: argparse.Namespace, book: ledger.Ledger) -> None:
    box = Sandbox(root=Path(args.sandbox).expanduser() / "meridian", book=book, phase=args.phase, experiment="E1")
    simulate.build(box, world.load("timeline.toml"))
    print(f"built {box.root}")


def cmd_timeline(args: argparse.Namespace, book: ledger.Ledger) -> None:
    timeline = world.load("timeline.toml")
    weeks = args.weeks or timeline["world"]["weeks"]
    steps = [s for s in timeline["steps"] if s["week"] <= weeks]
    estimate = sum(len(s["sessions"]) for s in steps) * ESTIMATE_PER_SESSION + weeks * 0.5 + len(steps) * ESTIMATE_GRADE * 2
    book.check(estimate, allow_past_ask=args.approved)
    box = timeline_box(args, book, "E1")
    simulate.run(box, timeline, make_grader(box), only_weeks=weeks)
    print(f"timeline done to week {weeks}; spent so far ${book.total():.2f}")


def cmd_probes(args: argparse.Namespace, book: ledger.Ledger, decoy: bool = False) -> None:
    defs = world.load("probes.toml")["decoys" if decoy else "probes"]
    chosen = [p for p in defs if not args.only or p["id"] in args.only]
    book.check(len(chosen) * len(args.arms) * args.reps * (ESTIMATE_PER_SESSION + ESTIMATE_GRADE), allow_past_ask=args.approved)
    box = timeline_box(args, book, "E4-decoys" if decoy else "E4")
    matched = json.loads((box.root / "e1" / "matched.json").read_text())
    out = box.root.parent / ("decoys.jsonl" if decoy else "probes.jsonl")
    g = make_grader(box)
    for rep in range(1, args.reps + 1):
        for p in chosen:
            for arm in args.arms:
                r = probes.run_one(box, p, arm, rep, PROBE_TIME, matched, g, out)
                print(f"{p['id']} {arm} #{rep}: {r.outcome or '-'} timely={r.timely} ${r.cost_usd:.2f} {r.error}")


def cmd_coldread(args: argparse.Namespace, book: ledger.Ledger) -> None:
    queries = world.load("queries.toml")["queries"]
    book.check(len(args.arms) * args.reps * (1.5 + 0.3), allow_past_ask=args.approved)
    box = timeline_box(args, book, "E7")
    out = box.root.parent / "coldread.jsonl"
    g = make_grader(box)
    for rep in range(1, args.reps + 1):
        for arm in args.arms:
            rec = coldread.run_one(box, queries, arm, rep, COLD_READ_DAY, g, out)
            print(f"cold read {arm} #{rep}: {len(rec['answers'])} answers, ${rec['cost_usd']:.2f} {rec.get('error', '')}")


def cmd_regrade(args: argparse.Namespace, book: ledger.Ledger) -> None:
    """Grade every reviewed proposal again with the current grader, into review.regraded.jsonl (decisions unchanged)."""
    timeline = world.load("timeline.toml")
    box = timeline_box(args, book, "regrade")
    entries = metrics.jsonl(box.root / "e1" / "review.jsonl")
    graded = [e for e in entries if e.get("grade") and "proposal" in e]
    book.check(len(graded) * ESTIMATE_GRADE, allow_past_ask=args.approved)
    g = make_grader(box)
    out = box.root / "e1" / "review.regraded.jsonl"
    out.unlink(missing_ok=True)
    regraded: dict[tuple[str, int], dict] = {}
    for e in entries:
        if e.get("grade") and "proposal" in e:
            key = (e["proposal"], e["week"])
            if key not in regraded:
                row = box.rows("SELECT * FROM proposal WHERE id = ?", e["proposal"])[0]
                regraded[key] = grader.match_proposal(
                    g, simulate.proposal_text(row), simulate.known_records(timeline, e["week"]), f"regrade-{e['proposal']}"
                )
            e = {**e, "grade": regraded[key], "grade_r1": e["grade"]}
        with out.open("a") as f:
            f.write(json.dumps(e) + "\n")
    print(f"regraded {len(regraded)} proposals into {out}")


def cmd_report(args: argparse.Namespace, book: ledger.Ledger) -> None:
    timeline = world.load("timeline.toml")
    pdefs = world.load("probes.toml")
    queries = world.load("queries.toml")["queries"]
    box = timeline_box(args, book, "report")
    base = box.root.parent
    data = {
        "E1 capture": metrics.capture(box, timeline),
        "E2 supersessions": metrics.staleness(box),
        "E6 burden": metrics.burden(box),
        "E4/E5 probes": metrics.probes(metrics.jsonl(base / "probes.jsonl"), pdefs["probes"]),
        "E4 decoys": metrics.decoys(metrics.jsonl(base / "decoys.jsonl"), pdefs["decoys"]),
        "E7/E3 cold read": metrics.coldread(metrics.jsonl(base / "coldread.jsonl"), queries),
        "spend": {"phase": book.total(phase=args.phase), "all research": book.total()},
    }
    text = json.dumps(data, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research.run")
    parser.add_argument("--ledger", default=str(ledger.default_path()))
    parser.add_argument("--phase", default="R1")
    parser.add_argument("--approved", action="store_true", help="the owner approved spending past the ask point")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "timeline", "probes", "decoys", "coldread", "report", "regrade"):
        p = sub.add_parser(name)
        p.add_argument("--sandbox", required=True)
        if name == "timeline":
            p.add_argument("--weeks", type=int, default=0)
        if name in ("probes", "decoys", "coldread"):
            p.add_argument("--reps", type=int, default=3)
        if name in ("probes", "decoys"):
            p.add_argument("--only", nargs="*", default=[])
            p.add_argument("--arms", nargs="*", default=["memory", "none"])
        if name == "coldread":
            p.add_argument("--arms", nargs="*", default=list(coldread.ARMS))
        if name == "report":
            p.add_argument("--out", default="")
    sub.add_parser("spent")
    args = parser.parse_args(argv)
    book = ledger.Ledger(Path(args.ledger).expanduser())
    if args.command == "spent":
        rows = book.rows()
        by_phase: dict[str, float] = {}
        for r in rows:
            by_phase[r["phase"]] = by_phase.get(r["phase"], 0.0) + float(r["cost_usd"])
        print(json.dumps({"total": book.total(), "by_phase": {k: round(v, 2) for k, v in by_phase.items()}, "budget": book.budget}, indent=2))
        return 0
    commands = {
        "build": cmd_build,
        "timeline": cmd_timeline,
        "probes": cmd_probes,
        "decoys": lambda a, b: cmd_probes(a, b, decoy=True),
        "coldread": cmd_coldread,
        "report": cmd_report,
        "regrade": cmd_regrade,
    }
    try:
        commands[args.command](args, book)
    except ledger.BudgetExceeded as stop:
        print(f"stopped: {stop}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
