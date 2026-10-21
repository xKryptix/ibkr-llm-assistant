"""SQLite connection and schema migrations.

Each migration runs once; `PRAGMA user_version` records how many have run.
Later phases append migrations (journal, positions, kill switch).
"""
import sqlite3
from pathlib import Path

MIGRATIONS = [
  """
  CREATE TABLE price_survey (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL,
    live_data INTEGER NOT NULL,
    audusd REAL,
    cap_usd REAL,
    symbol TEXT NOT NULL,
    spot REAL NOT NULL,
    expiry TEXT NOT NULL,
    dte INTEGER NOT NULL,
    option_right TEXT NOT NULL,
    structure TEXT NOT NULL,
    width REAL,
    long_strike REAL NOT NULL,
    long_delta REAL,
    long_bid REAL NOT NULL,
    long_ask REAL NOT NULL,
    short_strike REAL,
    short_delta REAL,
    short_bid REAL,
    short_ask REAL,
    debit_mid REAL NOT NULL,
    debit_natural REAL NOT NULL,
    commission_usd REAL NOT NULL,
    max_loss_mid_usd REAL NOT NULL,
    max_loss_natural_usd REAL NOT NULL,
    max_leg_spread_pct REAL NOT NULL,
    fits_cap INTEGER
  );
  CREATE INDEX price_survey_run_at ON price_survey (run_at);
  """,
  """
  CREATE TABLE kill_switch (
    id INTEGER PRIMARY KEY,
    tripped_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    reset_at TEXT
  );
  CREATE TABLE guard_decisions (
    id INTEGER PRIMARY KEY,
    decided_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    approved INTEGER NOT NULL,
    intent_json TEXT NOT NULL,
    results_json TEXT NOT NULL
  );
  """,
]

def connect(path: Path) -> sqlite3.Connection:
  """Open (creating if needed) the database at `path` and apply migrations."""
  path.parent.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(path)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA foreign_keys=ON")
  migrate(conn)
  return conn

def migrate(conn: sqlite3.Connection) -> None:
  """Apply any migrations not yet recorded in `user_version`."""
  version = conn.execute("PRAGMA user_version").fetchone()[0]
  for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
    conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {i};\nCOMMIT;")
