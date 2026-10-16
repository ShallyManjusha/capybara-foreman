"""
Run history and audit cache.

Note what this is *not*: the source of truth. Per-ticket state lives in the Jira
issue property, because the board is the thing every human and every other
system already looks at. Delete `var/foreman.db` and Foreman keeps working —
every ticket resumes from exactly where the board says it is.

What lives here is the history the board cannot hold: what each stage cost, how
long it took, what was rejected, which revision a ticket is on, and enough of
each run to answer "why did Foreman do that?" three weeks later.
"""
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  key TEXT PRIMARY KEY, project TEXT, summary TEXT, state TEXT,
  revision INTEGER DEFAULT 0, first_seen REAL, last_seen REAL,
  branch TEXT, pr_url TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS stage_runs (
  run_id TEXT, ticket TEXT, stage TEXT, mode TEXT, status TEXT,
  started REAL, duration REAL, revision INTEGER,
  error TEXT, error_kind TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS transitions (
  ticket TEXT, at REAL, from_state TEXT, to_state TEXT, reason TEXT,
  actor TEXT, run_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started REAL, duration REAL, live INTEGER,
  tickets INTEGER, advanced INTEGER, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage_ticket ON stage_runs(ticket);
CREATE INDEX IF NOT EXISTS idx_trans_ticket ON transitions(ticket);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ─────────────────────────────────────────────────────────────── tickets ─
    def seen(self, key: str, project: str, summary: str, state: str,
             revision: int = 0):
        now = time.time()
        row = self.db.execute("SELECT key FROM tickets WHERE key=?",
                              (key,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO tickets (key,project,summary,state,revision,"
                "first_seen,last_seen) VALUES (?,?,?,?,?,?,?)",
                (key, project, summary, state, revision, now, now))
        else:
            self.db.execute(
                "UPDATE tickets SET summary=?, state=?, revision=?, last_seen=? "
                "WHERE key=?", (summary, state, revision, now, key))
        self.db.commit()

    def set_ticket(self, key: str, **fields):
        if not fields:
            return
        cols = ", ".join("%s=?" % k for k in fields)
        self.db.execute("UPDATE tickets SET %s WHERE key=?" % cols,
                        tuple(fields.values()) + (key,))
        self.db.commit()

    def ticket(self, key: str) -> Optional[Dict[str, Any]]:
        cur = self.db.execute("SELECT * FROM tickets WHERE key=?", (key,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([c[0] for c in cur.description], row))

    # ──────────────────────────────────────────────────────────────── stages ─
    def record_stage(self, run_id: str, ticket: str, stage: str, mode: str,
                     status: str, duration: float, revision: int,
                     error: str = "", error_kind: str = "",
                     payload: Optional[Dict[str, Any]] = None):
        self.db.execute(
            "INSERT INTO stage_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ticket, stage, mode, status, time.time(), duration,
             revision, error, error_kind, json.dumps(payload or {})[:500000]))
        self.db.commit()

    def record_transition(self, ticket: str, frm: str, to: str, reason: str,
                          actor: str = "foreman", run_id: str = ""):
        self.db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?)",
                        (ticket, time.time(), frm, to, reason[:500], actor,
                         run_id))
        self.db.commit()

    def history(self, ticket: str) -> List[Dict[str, Any]]:
        cur = self.db.execute(
            "SELECT at, from_state, to_state, reason, actor FROM transitions "
            "WHERE ticket=? ORDER BY at", (ticket,))
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur]

    def stage_history(self, ticket: str) -> List[Dict[str, Any]]:
        cur = self.db.execute(
            "SELECT stage, mode, status, duration, revision, error, error_kind "
            "FROM stage_runs WHERE ticket=? ORDER BY started", (ticket,))
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur]

    # ─────────────────────────────────────────────────────────────────── runs ─
    def record_run(self, run_id: str, started: float, live: bool,
                   tickets: int, advanced: int, payload: Dict[str, Any]):
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
            (run_id, started, time.time() - started, 1 if live else 0,
             tickets, advanced, json.dumps(payload)[:500000]))
        self.db.commit()

    def summary(self) -> Dict[str, int]:
        rows = self.db.execute(
            "SELECT state, COUNT(*) FROM tickets GROUP BY state").fetchall()
        return {state or "unknown": n for state, n in rows}

    def recent_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        cur = self.db.execute(
            "SELECT run_id, started, duration, live, tickets, advanced "
            "FROM runs ORDER BY started DESC LIMIT ?", (limit,))
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur]
