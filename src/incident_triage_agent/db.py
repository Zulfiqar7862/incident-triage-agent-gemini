"""SQLite persistence: runs (checkpoints), events (trace), approvals, long-term memory.

Why SQLite: zero setup, transactional, survives restarts, good enough for one
process. Swap for Postgres when running several workers (same schema).

Rule used everywhere: status changes are *conditional* updates
(`UPDATE ... WHERE status IN (...)`). That prevents races such as an operator
pressing "pause" while the agent thread is finishing a step.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    scenario    TEXT NOT NULL,
    title       TEXT NOT NULL,
    alert       TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending|running|paused|waiting_approval|completed|escalated|failed|cancelled
    llm_mode    TEXT NOT NULL,          -- mock|gemini
    plan        TEXT NOT NULL DEFAULT '[]',   -- JSON list of plan steps
    state       TEXT NOT NULL,          -- JSON checkpoint: world, steps, notes, guidance, pending call
    step        INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    report      TEXT,                   -- JSON final report
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    ts      REAL NOT NULL,
    step    INTEGER NOT NULL,
    type    TEXT NOT NULL,              -- see EVENT TYPES in docs/LLD.md
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    step        INTEGER NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending|approved|rejected|executed|cancelled
    comment     TEXT,
    created_at  REAL NOT NULL,
    decided_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id);
CREATE TABLE IF NOT EXISTS memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT NOT NULL,
    symptom     TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    resolution  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""

TERMINAL = ("completed", "escalated", "failed", "cancelled")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("plan", "state", "report", "args", "data"):
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


def _first(rows: list[sqlite3.Row]) -> dict | None:
    return _row(rows[0]) if rows else None


def _marks(values: list) -> str:
    """'?,?,?' placeholders for an IN (...) clause."""
    return ",".join("?" * len(values))


class DB:
    """Thread-safe SQLite access. One connection behind a lock; writes run in short transactions."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic transaction (all-or-nothing)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ---------------- runs ----------------
    def create_run(self, scenario: str, title: str, alert: str, llm_mode: str, state: dict) -> dict:
        rid = uuid.uuid4().hex[:10]
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO runs (id, scenario, title, alert, status, llm_mode, state, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (rid, scenario, title, alert, "pending", llm_mode, json.dumps(state), now, now))
        return self.get_run(rid)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> dict | None:
        rows = self._q("SELECT * FROM runs WHERE id=?", (run_id,))
        return _first(rows)

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT id, scenario, title, status, step, tokens_in, tokens_out, cost_usd, llm_mode,"
                       " created_at, updated_at FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def runs_with_status(self, statuses: Iterable[str]) -> list[str]:
        st = list(statuses)
        rows = self._q(f"SELECT id FROM runs WHERE status IN ({_marks(st)})", st)
        return [r["id"] for r in rows]

    def save_checkpoint(self, run_id: str, *, state: dict, plan: list, step: int, tokens_in: int,
                        tokens_out: int, cost_usd: float, executed_approval: str | None = None) -> None:
        """Persist the agent's full working state. Never touches `status`.
        If an approved action was just executed we mark it in the SAME transaction,
        so after a crash we never run the same production change twice."""
        with self.tx() as c:
            c.execute("UPDATE runs SET state=?, plan=?, step=?, tokens_in=?, tokens_out=?, cost_usd=?, updated_at=?"
                      " WHERE id=?", (json.dumps(state), json.dumps(plan), step, tokens_in, tokens_out,
                                      cost_usd, time.time(), run_id))
            if executed_approval:
                c.execute("UPDATE approvals SET status='executed' WHERE id=? AND status='approved'",
                          (executed_approval,))

    def set_status(self, run_id: str, new: str, expected: Iterable[str] | None = None, *,
                   error: str | None = None, report: dict | None = None) -> bool:
        """Conditional status transition. Returns False if the run was not in an expected state."""
        sets, params = ["status=?", "updated_at=?"], [new, time.time()]
        if error is not None:
            sets.append("error=?")
            params.append(error)
        if report is not None:
            sets.append("report=?")
            params.append(json.dumps(report))
        sql = f"UPDATE runs SET {', '.join(sets)} WHERE id=?"
        params.append(run_id)
        if expected is not None:
            exp = list(expected)
            sql += f" AND status IN ({_marks(exp)})"
            params.extend(exp)
        with self.tx() as c:
            return c.execute(sql, params).rowcount == 1

    # ---------------- events (the trace) ----------------
    def add_event(self, run_id: str, step: int, type_: str, data: dict) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO events (run_id, ts, step, type, data) VALUES (?,?,?,?,?)",
                            (run_id, time.time(), step, type_, json.dumps(data, default=str)))
            return int(cur.lastrowid)

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        rows = self._q("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))
        return [_row(r) for r in rows]  # type: ignore[misc]

    # ---------------- approvals ----------------
    def create_approval(self, run_id: str, step: int, tool: str, args: dict, reason: str) -> dict:
        aid = uuid.uuid4().hex[:10]
        with self.tx() as c:
            c.execute("INSERT INTO approvals (id, run_id, step, tool, args, reason, status, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (aid, run_id, step, tool, json.dumps(args), reason, "pending", time.time()))
        return self.get_approval(aid)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> dict | None:
        rows = self._q("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return _first(rows)

    def pending_approval(self, run_id: str) -> dict | None:
        rows = self._q("SELECT * FROM approvals WHERE run_id=? AND status='pending' ORDER BY created_at DESC"
                       " LIMIT 1", (run_id,))
        return _first(rows)

    def list_approvals(self, run_id: str) -> list[dict]:
        return [_row(r) for r in self._q("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at",
                                         (run_id,))]  # type: ignore[misc]

    def decide_approval(self, approval_id: str, status: str, comment: str = "") -> bool:
        with self.tx() as c:
            return c.execute("UPDATE approvals SET status=?, comment=?, decided_at=? WHERE id=? AND status='pending'",
                             (status, comment, time.time(), approval_id)).rowcount == 1

    # ---------------- long-term memory ----------------
    def add_memory(self, service: str, symptom: str, root_cause: str, resolution: str, run_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO memory (service, symptom, root_cause, resolution, run_id, created_at)"
                      " VALUES (?,?,?,?,?,?)", (service, symptom, root_cause, resolution, run_id, time.time()))

    def search_memory(self, services: Iterable[str], limit: int = 3, max_age_days: float = 90) -> list[dict]:
        svcs = list(services)
        if not svcs:
            return []
        cutoff = time.time() - max_age_days * 86400  # staleness: old memories are ignored
        rows = self._q(f"SELECT * FROM memory WHERE service IN ({_marks(svcs)}) AND created_at>?"
                       " ORDER BY created_at DESC LIMIT ?", (*svcs, cutoff, limit))
        return [dict(r) for r in rows]

    def list_memory(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM memory ORDER BY created_at DESC LIMIT 100")]
