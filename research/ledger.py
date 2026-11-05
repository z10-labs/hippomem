"""The cost ledger: every model call the research makes, and the budget guard.

One CSV for the whole research, kept outside the sandboxes so it survives
them. Every agent, judge, reviewer and grader call appends a row. Nothing
starts a run that the remaining budget can't cover.
"""

from __future__ import annotations

import csv
import fcntl
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BUDGET_USD = 150.0
ASK_AT_USD = 100.0
COLUMNS = ("at", "phase", "experiment", "kind", "label", "cost_usd")

LEDGER_ENV = "HIPPOMEM_RESEARCH_LEDGER"
PHASE_ENV = "HIPPOMEM_RESEARCH_PHASE"
EXPERIMENT_ENV = "HIPPOMEM_RESEARCH_EXPERIMENT"
KIND_ENV = "HIPPOMEM_RESEARCH_KIND"
LABEL_ENV = "HIPPOMEM_RESEARCH_LABEL"


class BudgetExceeded(Exception):
    """Starting this would take the research past its budget or past the point where Claude must ask."""


@dataclass(frozen=True)
class Ledger:
    path: Path
    budget: float = BUDGET_USD
    ask_at: float = ASK_AT_USD

    def record(self, *, phase: str, experiment: str, kind: str, label: str, cost_usd: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self.path.open("a", newline="") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # judge calls run inside the server, alongside agent runs
            writer = csv.writer(f)
            if new:
                writer.writerow(COLUMNS)
            at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            writer.writerow((at, phase, experiment, kind, label, f"{cost_usd:.4f}"))

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="") as f:
            return list(csv.DictReader(f))

    def total(self, **match: str) -> float:
        return round(sum(float(r["cost_usd"]) for r in self.rows() if all(r.get(k) == v for k, v in match.items())), 4)

    def check(self, estimate: float, *, allow_past_ask: bool = False) -> None:
        """Raise unless `estimate` more dollars fit. Past `ask_at`, the owner must approve first."""
        spent = self.total()
        if spent + estimate > self.budget:
            raise BudgetExceeded(f"${spent:.2f} spent; ${estimate:.2f} more would pass the ${self.budget:.0f} budget")
        if spent + estimate > self.ask_at and not allow_past_ask:
            raise BudgetExceeded(f"${spent:.2f} spent; ${estimate:.2f} more would pass ${self.ask_at:.0f}, where the owner is asked first")


def from_env() -> Ledger | None:
    path = os.environ.get(LEDGER_ENV)
    return Ledger(Path(path)) if path else None


def default_path() -> Path:
    return Path.home() / ".hippomem-research" / "ledger.csv"
