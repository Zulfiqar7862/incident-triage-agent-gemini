"""Live token deltas: model text shown in the UI while it is being written.

Deltas live in memory only and are never written to SQLite. The final text is saved
once, in the normal trace event (thought, plan or answer), so a restart loses nothing.
"""
from __future__ import annotations

import threading
from collections import deque


class TokenStream:
    """Per-run buffer of recent deltas: {seq, step, kind, text}; kind is plan, thought or answer."""

    def __init__(self, keep: int = 2000):
        self._lock = threading.Lock()
        self._seq = 0
        self._keep = keep
        self._runs: dict[str, deque] = {}

    def publish(self, run_id: str, step: int, kind: str, text: str) -> None:
        with self._lock:
            self._seq += 1
            buf = self._runs.setdefault(run_id, deque(maxlen=self._keep))
            buf.append({"seq": self._seq, "step": step, "kind": kind, "text": text})

    def since(self, run_id: str, seq: int = 0) -> list[dict]:
        with self._lock:
            return [d for d in self._runs.get(run_id, ()) if d["seq"] > seq]
