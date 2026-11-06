"""Numbers for each experiment, from what the runs left on disk.

Pure functions over the sandbox's files and memory, so a report can be
rebuilt at any time without running anything.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from research.team import Sandbox

SUPERSESSIONS = [("M03", "M21"), ("M07", "M15"), ("M09", "M19"), ("M18", "M26")]


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rate(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


# --- E1, E2, E6: the timeline ----------------------------------------------------------


def review_log(box: Sandbox) -> list[dict]:
    """The weekly review log, with regraded entries (`review.regraded.jsonl`) when they exist."""
    regraded = box.root / "e1" / "review.regraded.jsonl"
    return jsonl(regraded if regraded.exists() else box.root / "e1" / "review.jsonl")


def capture(box: Sandbox, timeline: dict) -> dict:
    review = review_log(box)
    rescued = {s["step"] for s in jsonl(box.root / "e1" / "steps.jsonl") if s.get("rescued")}
    rescued_records = {oid for step in timeline["steps"] if step["id"] in rescued for oid in step["expects"]}
    graded = [r for r in review if r.get("grade") and "proposal" in r]
    decided = [r for r in graded if r.get("decision") in ("accept", "accepted", "reject", "rejected")]
    matched: dict[str, str] = json.loads((box.root / "e1" / "matched.json").read_text()) if (box.root / "e1" / "matched.json").exists() else {}
    oracle = {d["id"]: d for d in timeline["decisions"] if not d.get("excluded")}

    # before review: what the agents and the judge proposed
    pre = {
        "proposals": len(graded),
        "matched_a_record": sum(1 for r in graded if r["grade"].get("match")),
        "statement_correct": sum(1 for r in graded if r["grade"].get("statement_correct")),
        "why": dict(Counter(str(r["grade"].get("why")) for r in graded)),
        "contradicting_claims": sum(len(_contradicts(r["grade"])) for r in graded),
        "proposals_contradicting_the_key": sum(1 for r in graded if _contradicts(r["grade"])),
        "proposals_as_supersessions": sum(1 for r in graded if r.get("kind") == "supersede"),
        "unmatched": [r["proposal"] for r in graded if not r["grade"].get("match")],
        "by_source": dict(Counter(("inferred" if r.get("inferred") else r.get("source", "?")) for r in graded)),
    }
    # alternatives: of each record's rejected options, how many the matched proposal named
    alt_found = alt_total = 0
    for r in graded:
        match = r["grade"].get("match")
        if match in oracle and matched.get(match) == r["proposal"]:
            alt_total += len(oracle[match].get("rejected", []))
            alt_found += min(len(r["grade"].get("alternatives_found") or []), len(oracle[match].get("rejected", [])))

    # after review: what got in
    accepted = [
        r for r in decided if r.get("decision") in ("accept", "accepted") and str(r.get("outcome", "")).startswith(("Accepted", "by reviewer"))
    ]
    not_in_key = [r["proposal"] for r in accepted if not (r["grade"].get("match") and r["grade"].get("statement_correct"))]
    contradicting = [r["proposal"] for r in accepted if _contradicts(r["grade"])]
    reviewer_accepts = [r for r in accepted if r.get("outcome") == "by reviewer agent"]
    recall = {
        "heavy": rate(
            sum(1 for i, d in oracle.items() if d["weight"] == "heavy" and i in matched), sum(1 for d in oracle.values() if d["weight"] == "heavy")
        ),
        "standard": rate(
            sum(1 for i, d in oracle.items() if d["weight"] == "standard" and i in matched),
            sum(1 for d in oracle.values() if d["weight"] == "standard"),
        ),
        "all": rate(sum(1 for i in oracle if i in matched), len(oracle)),
        "all, leaving out steps the engineer had to unblock": rate(
            sum(1 for i in oracle if i in matched and i not in rescued_records), sum(1 for i in oracle if i not in rescued_records)
        ),
        "missing": sorted(i for i in oracle if i not in matched),
        "rescued_steps": sorted(rescued),
    }
    memory_driven = {}
    for step in timeline["steps"]:
        if step.get("memory_driven"):
            for oid in step["expects"]:
                memory_driven[oid] = (
                    "captured as the team's choice" if oid in matched else "not captured (check the run: did the agent choose differently?)"
                )

    explored = jsonl(box.root / "e1" / "explored.jsonl")
    explored_matched = {str(x["grade"].get("match")) for x in explored if x.get("grade", {}).get("match")}
    return {
        "before_review": pre,
        "alternatives_named": rate(alt_found, alt_total),
        "after_review": {
            "accepted": len(accepted),
            "accepted_not_a_team_decision": not_in_key,
            "accepted_contradicting_the_key": contradicting,
            "accepted_by_reviewer_agent": len(reviewer_accepts),
            "reviewer_agent_accepted_not_in_key": [r["proposal"] for r in reviewer_accepts if r["proposal"] in not_in_key],
            "recall": recall,
        },
        "memory_driven_steps": memory_driven,
        "explored": {
            "records": len(explored),
            "matched_answer_key": sorted(i for i in explored_matched if i.startswith("X")),
            "expected": [x["id"] for x in timeline["explored"]],
            "unmatched_records": [x["explored"] for x in explored if not x.get("grade", {}).get("match")],
        },
    }


def _contradicts(grade: dict) -> list:
    """Claims contradicting the answer key. R1 grades predate the split and only have `invented`, which mixes
    contradictions with extra detail; they are regraded rather than read from that field."""
    return list(grade.get("contradicts") or [])


def memory_id(box: Sandbox, matched: dict[str, str], oracle_id: str) -> str | None:
    pid = matched.get(oracle_id)
    if not pid:
        return None
    rows = box.rows("SELECT decision_id FROM decision_version WHERE proposal_id = ?", pid)
    return rows[0]["decision_id"] if rows else None


def staleness(box: Sandbox) -> dict:
    """E2: did each supersession in the timeline reach the memory, as a supersession?"""
    matched: dict[str, str] = json.loads((box.root / "e1" / "matched.json").read_text()) if (box.root / "e1" / "matched.json").exists() else {}
    out = {}
    for old, new in SUPERSESSIONS:
        old_id, new_id = memory_id(box, matched, old), memory_id(box, matched, new)
        if not old_id or not new_id:
            out[f"{old}→{new}"] = f"n/a (old {old_id or 'not captured'}, new {new_id or 'not captured'})"
            continue
        status = box.rows("SELECT status FROM decision WHERE id = ?", old_id)[0]["status"]
        linked = bool(box.rows("SELECT 1 FROM link WHERE from_id = ? AND type = 'supersedes' AND to_id = ?", new_id, old_id))
        out[f"{old}→{new}"] = f"{old_id} {status}; {new_id} supersedes it: {'yes' if linked else 'no'}"
    # The other direction: decisions taken out of force that the team never replaced.
    by_decision = {memory_id(box, matched, oid): oid for oid in matched}
    still_true = {d for d, oid in by_decision.items() if d and oid not in {o for o, _ in SUPERSESSIONS}}
    wrongly = [r["id"] for r in box.rows("SELECT id FROM decision WHERE status = 'superseded'") if r["id"] in still_true]
    out["taken out of force but still true"] = [f"{d} ({by_decision[d]})" for d in wrongly]
    return out


def burden(box: Sandbox) -> dict:
    """E6: how much the memory asked of engineers."""
    stops = box.rows("SELECT detail FROM usage WHERE action = 'turn_stop'")
    nudged = sum(1 for r in stops if json.loads(r["detail"]).get("nudged"))
    merges = box.rows("SELECT COUNT(*) AS n FROM branch WHERE state = 'merged'")[0]["n"]
    proposals = box.rows("SELECT status, source FROM proposal")
    statuses = Counter(r["status"] for r in proposals)
    return {
        "proposals": len(proposals),
        "proposals_per_merged_branch": round(len(proposals) / merges, 2) if merges else None,
        "acceptance": rate(statuses["accepted"], statuses["accepted"] + statuses["rejected"]),
        "end_of_turn_nudges": rate(nudged, len(stops)),
        "author_questions": box.rows("SELECT COUNT(*) AS n FROM question")[0]["n"],
        "drafts": box.rows("SELECT COUNT(*) AS n FROM draft")[0]["n"],
    }


# --- E4, E5: probes --------------------------------------------------------------------


def probes(results: list[dict], probe_defs: list[dict]) -> dict:
    by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        by[(r["probe"], r["arm"])].append(r)
    stale_probes = {p["id"] for p in probe_defs if p.get("stale")}
    table = []
    totals: dict[str, Counter] = defaultdict(Counter)
    for p in probe_defs:
        row = {"probe": p["id"]}
        for arm in ("memory", "none"):
            runs = by.get((p["id"], arm), [])
            outcomes = Counter(r["outcome"] for r in runs)
            row[arm] = f"{outcomes['followed']}F {outcomes['asked']}A {outcomes['violated']}V {outcomes['other']}O of {len(runs)}"
            totals[arm].update(outcomes)
            totals[arm]["n"] += len(runs)
            if p["id"] in stale_probes:
                totals[arm]["stale_runs"] += len(runs)
                totals[arm]["stale_violations"] += outcomes["violated"]
        table.append(row)
    memory_runs = [r for r in results if r["arm"] == "memory" and r.get("timely") is not None]
    summary: dict[str, object] = {}
    for arm in ("memory", "none"):
        t = totals[arm]
        summary[arm] = {
            "conformed (followed or asked)": rate(t["followed"] + t["asked"], t["n"]),
            "violated": rate(t["violated"], t["n"]),
            "stale-fact failures": rate(t["stale_violations"], t["stale_runs"]),
            "mean cost": round(sum(r["cost_usd"] for r in results if r["arm"] == arm) / max(t["n"], 1), 3),
        }
    summary["timely (memory arm)"] = rate(sum(1 for r in memory_runs if r["timely"]), len(memory_runs))
    return {"table": table, "summary": summary}


def decoys(results: list[dict], decoy_defs: list[dict]) -> dict:
    allowed = {d["id"]: d["allowed_files"] for d in decoy_defs}
    out = {}
    for arm in ("memory", "none"):
        runs = [r for r in results if r["arm"] == arm]
        strays = [r["run_id"] for r in runs if any(not any(f.startswith(a) for a in allowed[r["probe"]]) for f in r["files_touched"])]
        out[arm] = {
            "runs": len(runs),
            "touched files outside the task": strays,
            "mean cost": round(sum(r["cost_usd"] for r in runs) / max(len(runs), 1), 3),
            "mean turns": round(sum(r["turns"] for r in runs) / max(len(runs), 1), 1),
            "mean memory calls": round(sum(r["memory_calls"] for r in runs) / max(len(runs), 1), 1),
        }
    return out


# --- E7, E3: cold read -----------------------------------------------------------------


def coldread(records: list[dict], queries: list[dict]) -> dict:
    qdefs = {q["id"]: q for q in queries}
    by_arm: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    wrong: Counter = Counter()
    for rec in records:
        grades = rec.get("grades") or {}
        for qid, q in qdefs.items():
            g = grades.get(qid) or {}
            points = g.get("points") or []
            score = sum(1 for p in points if p) / len(q["must"]) if q["must"] else 0.0
            group = "held_out" if q.get("held_out") else "visible"
            by_arm[rec["arm"]][group].append(score)
            by_arm[rec["arm"]][q["kind"]].append(score)
            by_arm[rec["arm"]]["all"].append(score)
            wrong[rec["arm"]] += sum(1 for w in (g.get("wrong") or []) if w)
    out: dict[str, dict[str, object]] = {}
    for arm, groups in by_arm.items():
        out[arm] = {k: f"{100 * sum(v) / len(v):.0f}%" for k, v in sorted(groups.items())}
        out[arm]["wrong claims"] = wrong[arm]
    return out
