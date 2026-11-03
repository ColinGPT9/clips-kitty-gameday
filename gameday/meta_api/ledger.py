"""A ledger for scripts and checks: prints every Meta call and refuses calls past a budget.

The app's own ledger is the `api_calls` table; this one keeps calls in memory.
"""

from __future__ import annotations

import sys
import threading
from typing import TextIO

from gameday.types import ApiCall, BudgetExceeded


class ConsoleLedger:
    def __init__(self, budget_usd: float, out: TextIO | None = None):
        self.budget_usd = budget_usd
        self.calls: list[ApiCall] = []
        self._out = out or sys.stdout
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return round(sum(c.cost_usd for c in self.calls), 6)

    def check_budget(self, estimate_usd: float) -> None:
        if self.spent_usd + estimate_usd > self.budget_usd:
            raise BudgetExceeded(f"This call (about ${estimate_usd:.4f}) would pass the ${self.budget_usd:.2f} budget "
                                 f"(${self.spent_usd:.4f} spent)")

    def record(self, call: ApiCall) -> None:
        with self._lock:
            self.calls.append(call)
        units = " ".join(f"{k}={v}" for k, v in call.units.items())
        state = "ok" if call.ok else f"FAILED {call.error}"
        print(f"  [meta] {call.model} {call.purpose}: {state} (HTTP {call.status}, {call.ms / 1000:.1f} s, "
              f"~${call.cost_usd:.4f}) {units}", file=self._out, flush=True)

    def by_model(self) -> dict[str, dict]:
        with self._lock:
            calls = list(self.calls)
        out: dict[str, dict] = {}
        for c in calls:
            row = out.setdefault(c.model, {"calls": 0, "failed": 0, "usd": 0.0, "seconds": 0.0})
            row["calls"] += 1
            row["failed"] += 0 if c.ok else 1
            row["usd"] = round(row["usd"] + c.cost_usd, 6)
            row["seconds"] = round(row["seconds"] + c.ms / 1000, 2)
        return out
