"""Plain-text rendering for agents and the CLI.

Agents read this text, so it is written to be skimmed: ids first, status
markers that can't be missed, and explored records and drafts labelled so they
are never mistaken for decisions.
"""

from __future__ import annotations

from hippomem import decisions as dec
from hippomem.explored import Explored
from hippomem.proposals import Proposal
from hippomem.search import DecisionHit, DraftHit, ExploredHit, Results

EMPTY = (
    "No recorded decisions matched. That only means nothing recorded matched above the relevance floor; "
    "it does not mean no constraints apply. If you make a decision here, draft it."
)


def status_marker(d: dec.Decision) -> str:
    if d.status == "superseded":
        by = [lk.other for lk in d.links if lk.type == "superseded-by"]
        return f"  ⚠ SUPERSEDED{' by ' + ', '.join(by) if by else ''}: not in force"
    if d.status == "retired":
        return "  ⚠ RETIRED: not in force"
    return ""


def decision_line(d: dec.Decision) -> str:
    tags = [", ".join(d.systems) or d.scope, d.weight]
    if d.scope != "code":
        tags.append(f"{d.scope}-wide")
    tags.append("implemented" if d.implemented else "not yet built")
    return f"{d.id}  {d.title}  [{' · '.join(tags)}]{status_marker(d)}"


def decision_body(d: dec.Decision, indent: str = "  ") -> list[str]:
    lines = []
    if d.statement and d.statement != d.title:
        lines.append(f"{indent}{d.statement}")
    lines.append(f"{indent}Why: {d.why}")
    if d.trade_off:
        lines.append(f"{indent}Trade-off: {d.trade_off}")
    for alt in d.alternatives:
        lines.append(f"{indent}Rejected: {alt.option}: {alt.reason}")
    for con in d.constraints:
        revisit = f" (revisit if {con.revisit_if})" if con.revisit_if else ""
        lines.append(f"{indent}Constraint ({con.kind}): {con.text}{revisit}")
    if d.links:
        lines.append(f"{indent}Links: " + ", ".join(f"{lk.type} {lk.other}" for lk in d.links))
    if d.anchors:
        lines.append(f"{indent}Covers: " + ", ".join(f"{a.repo}:{a.path_glob}" for a in d.anchors))
    return lines


def decision_full(d: dec.Decision) -> str:
    made = d.made_by + (f" via {d.made_via}" if d.made_via else "")
    lines = [decision_line(d), *decision_body(d)]
    lines.append(f"  Made by {made}; accepted by {d.accepted_by} ({d.accepted_by_kind}); version {d.version}, recorded {d.recorded_at}")
    return "\n".join(lines)


def _decision_hit(h: DecisionHit) -> list[str]:
    via = "" if h.via == "direct" else f"  ↳ {h.via}"
    scope = "" if h.in_scope else "  (outside this repo's systems)"
    return [decision_line(h.decision) + scope + via, *decision_body(h.decision)]


def explored_line(x: Explored) -> list[str]:
    return [
        f"{x.id}  EXPLORED, NOT A DECISION: {x.title}",
        f"  Tried: {x.tried}. Outcome: {x.outcome}.",
        f"  Why: {x.why}",
        f"  Revisit if: {x.revisit_if}",
    ]


def _explored_hit(h: ExploredHit) -> list[str]:
    return explored_line(h.record)


def _draft_hit(h: DraftHit) -> list[str]:
    where = "this branch" if h.own_branch else f"{h.repo}:{h.branch} ({h.engineer})"
    why = f": {h.why}" if h.why else ""
    return [f"draft #{h.draft_id} on {where}, {h.kind}: {h.title}{why}"]


def results(r: Results) -> str:
    if r.empty:
        return EMPTY
    out: list[str] = []
    if r.decisions:
        scope = f" (this repo's systems: {', '.join(r.scope)})" if r.scope else ""
        out.append(f"Decisions{scope}:")
        for h in r.decisions:
            out.extend(_decision_hit(h))
    if r.explored:
        out.append("")
        out.append("Explored before (dead ends; check 'Revisit if' before trying again):")
        for x in r.explored:
            out.extend(_explored_hit(x))
    if r.drafts:
        out.append("")
        out.append("In progress, not decided:")
        for d in r.drafts:
            out.extend(_draft_hit(d))
    return "\n".join(out)


def proposal_line(p: Proposal) -> str:
    who = p.proposed_by + (f" via {p.proposed_via}" if p.proposed_via else "")
    systems = f" [{', '.join(p.systems)}]" if p.systems else ""
    return f"{p.id}  {p.kind}{systems} ({p.weight}): {p.title}  from {who}"


def proposal_full(p: Proposal) -> str:
    lines = [proposal_line(p)]
    payload = p.payload
    if payload.get("inferred"):
        lines.append("  Inferred from the branch history at merge; nobody drafted it. Check the why before accepting.")
    if p.target:
        lines.append(f"  Target: {p.target}")
    for key in ("statement", "why", "trade_off"):
        if payload.get(key) and payload.get(key) != payload.get("title"):
            lines.append(f"  {key.replace('_', '-').capitalize()}: {payload[key]}")
    for alt in payload.get("alternatives", ()):
        lines.append(f"  Rejected: {alt['option']}: {alt['reason']}")
    for ev in payload.get("evidence", ()):
        lines.append(f"  Evidence: {ev['kind']} {ev['ref']}")
    return "\n".join(lines)


def chain(tree: dict) -> str:
    lines = [f"{tree['id']}  {tree['title']}" + ("" if tree["status"] == "accepted" else f"  ({tree['status']})")]

    def walk(nodes: list[dict], depth: int) -> None:
        for n in nodes:
            status = "" if n["status"] == "accepted" else f"  ({n['status']})"
            lines.append(f"{'  ' * depth}{n['link']} {n['id']}  {n['title']}{status}")
            walk(n["children"], depth + 1)

    lines.append("Depends on:" if tree["depends_on"] else "Depends on: nothing recorded")
    walk(tree["depends_on"], 1)
    lines.append("Would be affected if it changed:" if tree["affects"] else "Would be affected if it changed: nothing recorded")
    walk(tree["affects"], 1)
    return "\n".join(lines)
