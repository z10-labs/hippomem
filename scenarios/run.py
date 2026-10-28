"""Run the scenario harness.

    python -m scenarios.run --sandbox /tmp/hippomem-scenarios            # all scenarios
    python -m scenarios.run --sandbox /tmp/hm --only S1 S4                # some of them

Writes results.json and results.md into the sandbox. Stops starting new
scenarios once the spend cap is reached.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

from scenarios import fixtures
from scenarios.flow import s4_reversal_and_merge, s5_other_branch, s6_closed_branch, s7_turn_nudge, s8_ask_author, s9_review
from scenarios.harness import Team
from scenarios.scenarios import Outcome, s1_settled_argument, s2_dead_end, s3_team_rule

SPEND_CAP_USD = 30.0

ORDER = [
    ("S9", s9_review),
    ("S1", s1_settled_argument),
    ("S2", s2_dead_end),
    ("S3", s3_team_rule),
    ("S4", s4_reversal_and_merge),
    ("S5", s5_other_branch),
    ("S6", s6_closed_branch),
    ("S7", s7_turn_nudge),
    ("S8", s8_ask_author),
]


def build_team(root: Path) -> Team:
    if root.exists():
        shutil.rmtree(root)
    team = Team(root=root)
    team.create(fixtures.TEAM_CONFIG, fixtures.ENGINEERS)
    fixtures.seed(str(team.home / "memory.db"))
    team.add_repo("ledger-api", fixtures.LEDGER_API)
    team.add_repo("ledger-worker", fixtures.LEDGER_WORKER)
    team.add_repo("storefront", fixtures.STOREFRONT, author=("Sam", "sam@example.com"))
    api = team.repo("ledger-api")
    team.set_author(api, "Sam", "sam@example.com")
    for i, text in enumerate((fixtures.PAYOUTS_V1, fixtures.PAYOUTS_V2, fixtures.PAYOUTS_V3), start=1):
        team.commit(api, f"payouts: step {i}", {"src/ledger_api/payouts.py": text})
    team.set_author(api, "Alex", "alex@example.com")
    return team


def report(team: Team, outcomes: list[Outcome]) -> str:
    lines = ["# Hippomem scenario results", ""]
    total = sum(len(o.checks) for o in outcomes)
    passed = sum(c.passed for o in outcomes for c in o.checks)
    lines.append(f"{passed}/{total} checks passed across {len(outcomes)} scenarios. Agent spend: ${team.spent:.2f}.")
    lines.append("")
    for o in outcomes:
        lines.append(f"## {o.scenario}: {o.title}")
        for c in o.checks:
            mark = "PASS" if c.passed else "FAIL"
            detail = f" ({c.detail[:300]})" if c.detail and not c.passed else ""
            lines.append(f"- {mark} {c.name}{detail}")
        for note in o.notes:
            lines.append(f"- note: {note[:600]}")
        runs = [r for r in team.runs if r.scenario == o.scenario]
        for r in runs:
            lines.append(f"- run '{r.label}': {r.turns} turns, ${r.cost_usd:.2f}, {r.seconds}s" + (f", error: {r.error}" if r.error else ""))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", required=True, type=Path)
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args(argv)

    team = build_team(args.sandbox)
    team.start_server()
    outcomes: list[Outcome] = []
    try:
        for key, scenario in ORDER:
            if args.only and key not in args.only:
                continue
            if team.spent >= SPEND_CAP_USD:
                outcomes.append(Outcome(key, f"skipped: spend cap ${SPEND_CAP_USD} reached"))
                continue
            print(f"[{key}] running (spent so far ${team.spent:.2f})", flush=True)
            try:
                outcomes.append(scenario(team))
            except Exception:
                failed = Outcome(key, "crashed")
                failed.check("scenario ran", False, traceback.format_exc()[-1500:])
                outcomes.append(failed)
            print(f"[{key}] {'passed' if outcomes[-1].passed else 'had failures'}", flush=True)
    finally:
        team.stop_server()
    text = report(team, outcomes)
    (args.sandbox / "results.md").write_text(text)
    (args.sandbox / "results.json").write_text(
        json.dumps({"outcomes": [asdict(o) for o in outcomes], "runs": [asdict(r) for r in team.runs], "spent": team.spent}, indent=2)
    )
    print(text)
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
