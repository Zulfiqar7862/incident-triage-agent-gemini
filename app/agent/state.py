"""Run state: what one run knows, and how it is checkpointed.

The durable state lives in the `runs` row (see db.py). `RunState` is the typed,
in-memory view the step loop works on; `Checkpointer` writes it back after
every step, which is what makes a run resumable after a restart.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db import DB


@dataclass
class RunState:
    run: dict        # the runs row (id, alert, status, ...)
    state: dict      # working memory: world, steps, notes, pending approval
    plan: list       # current plan, one string per item
    step: int        # number of completed steps
    tokens_in: int
    tokens_out: int

    @classmethod
    def load(cls, run: dict) -> RunState:
        return cls(run, run["state"], run["plan"], run["step"], run["tokens_in"], run["tokens_out"])

    @property
    def run_id(self) -> str:
        return self.run["id"]

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def cost_usd(settings: Settings, tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * settings.price_input_per_m + tokens_out / 1e6 * settings.price_output_per_m


class Checkpointer:
    def __init__(self, db: DB, settings: Settings):
        self.db, self.s = db, settings

    def save(self, rs: RunState, executed_approval: str | None = None) -> None:
        """Persist the run state. `executed_approval` marks an approved action as done in the same write."""
        cost = round(cost_usd(self.s, rs.tokens_in, rs.tokens_out), 6)
        self.db.save_checkpoint(rs.run_id, state=rs.state, plan=rs.plan, step=rs.step, tokens_in=rs.tokens_in,
                                tokens_out=rs.tokens_out, cost_usd=cost,
                                executed_approval=executed_approval)
