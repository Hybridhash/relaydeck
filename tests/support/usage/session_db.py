"""Shared session-database builder test helpers (moved out of test modules).

Synthetic databases; real table/column names, invented rows only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from relaydeck.usage.contracts import Batch, Budget, Cursor
from relaydeck.usage.readers.sqlite_session_usage import READER
from tests.support.usage.store import NOW

NOW_S = NOW / 1000

SCHEMA = """
CREATE TABLE sessions(id TEXT PRIMARY KEY, model TEXT, cwd TEXT,
  parent_session_id TEXT, started_at REAL NOT NULL, last_activity_at REAL,
  ended_at REAL, api_call_count INTEGER, input_tokens INTEGER,
  output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  reasoning_tokens INTEGER);
CREATE TABLE session_model_usage(session_id TEXT, model TEXT,
  billing_provider TEXT, billing_base_url TEXT,
  billing_mode TEXT, task TEXT,
  api_call_count INTEGER, input_tokens INTEGER,
  output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  estimated_cost_usd REAL, actual_cost_usd REAL,
  cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL);
"""

# Real column names with relaxed constraints, so the guards for NULL and
# mistyped cells can be exercised (the sessions table is nullable in reality).
USAGE_ONLY_SCHEMA = (
    "CREATE TABLE session_model_usage" + SCHEMA.split("CREATE TABLE session_model_usage", 1)[1]
)
SESSIONS_ONLY_SCHEMA = SCHEMA.split("CREATE TABLE session_model_usage", 1)[0]


def usage_row(session="s1", model="model-a", counters=(10, 5, 2, 1, 0), **over):
    row = {
        "session_id": session,
        "model": model,
        "billing_provider": "",
        "billing_base_url": "",
        "billing_mode": "",
        "task": "",
        "api_call_count": 1,
        "input_tokens": counters[0],
        "output_tokens": counters[1],
        "cache_read_tokens": counters[2],
        "cache_write_tokens": counters[3],
        "reasoning_tokens": counters[4],
        "estimated_cost_usd": 0.0,
        "actual_cost_usd": 0.0,
        "cost_status": None,
        "cost_source": None,
        "first_seen": NOW_S - 3600,
        "last_seen": NOW_S + 100,
    }
    row.update(over)
    return row


def session_row(sid="s1", counters=(10, 5, 2, 1, 0), **over):
    row = {
        "id": sid,
        "model": "model-a",
        "cwd": "/synthetic/work/alpha",
        "parent_session_id": None,
        "started_at": NOW_S - 3600,
        "last_activity_at": None,
        "ended_at": None,
        "api_call_count": 1,
        "input_tokens": counters[0],
        "output_tokens": counters[1],
        "cache_read_tokens": counters[2],
        "cache_write_tokens": counters[3],
        "reasoning_tokens": counters[4],
    }
    row.update(over)
    return row


def make_db(path: Path, *, usage=(), sessions=(), schema: str = SCHEMA) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.executescript(schema)
        for row in sessions:
            keys = sorted(row)
            db.execute(
                f"INSERT INTO sessions({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
                [row[k] for k in keys],
            )
        for row in usage:
            keys = sorted(row)
            db.execute(
                "INSERT INTO session_model_usage("
                + ",".join(keys)
                + ") VALUES("
                + ",".join("?" * len(keys))
                + ")",
                [row[k] for k in keys],
            )
        db.commit()
    finally:
        db.close()
    return path


def make_home(tmp_path: Path, *, main=(), main_sessions=(), profiles=None) -> Path:
    root = tmp_path / "home"
    if main or main_sessions:
        make_db(root / "state.db", usage=main, sessions=main_sessions)
    for name, spec in (profiles or {}).items():
        rows, srows = spec
        make_db(root / "profiles" / name / "state.db", usage=rows, sessions=srows)
    return root


def run(root: Path, cursor: Cursor | None = None, **budget_kw) -> Batch:
    return READER.read(root, cursor or Cursor(), Budget(**budget_kw))
