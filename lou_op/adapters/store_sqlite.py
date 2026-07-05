"""SqliteStore: the default durable Store adapter (P3).

WAL mode: concurrent task-runs append safely from threads; a crashed
process loses nothing that was appended. One file per jobs dir.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from ..ports.store import Event, RunState, Store, now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    data TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    state TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (run_id, last_seq)
);
"""


class SqliteStore(Store):
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def append(self, run_id: str, kind: str, data: dict, *, version: int = 1) -> Event:
        ts = now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO events (run_id, kind, version, data, ts)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, kind, version, json.dumps(data), ts),
            )
            return Event(cur.lastrowid, run_id, kind, version, data, ts)

    def events(self, run_id: str, *, after_seq: int = 0) -> List[Event]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, run_id, kind, version, data, ts FROM events"
                " WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [Event(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) for r in rows]

    def save_snapshot(self, run_id: str, state: RunState) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (run_id, last_seq, state, ts)"
                " VALUES (?, ?, ?, ?)",
                (run_id, state.last_seq, json.dumps(asdict(state)), now()),
            )

    def latest_snapshot(self, run_id: str) -> Optional[RunState]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM snapshots WHERE run_id = ?"
                " ORDER BY last_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunState(**json.loads(row[0]))

    def run_ids(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT run_id FROM events ORDER BY run_id"
            ).fetchall()
        return [r[0] for r in rows]
