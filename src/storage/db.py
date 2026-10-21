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
  """
  CREATE TABLE bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  CREATE TABLE positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    option_right TEXT NOT NULL,
    structure TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    long_con_id INTEGER NOT NULL,
    long_strike REAL NOT NULL,
    short_con_id INTEGER,
    short_strike REAL,
    entry_price REAL NOT NULL,
    entry_commission REAL NOT NULL,
    max_loss_usd REAL NOT NULL,
    opened_at TEXT NOT NULL,
    opened_on TEXT NOT NULL,
    tp_order_id INTEGER,
    tp_perm_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    realized_pnl_usd REAL NOT NULL DEFAULT 0
  );
  CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    reason TEXT,
    position_id TEXT,
    fingerprint TEXT,
    intent_json TEXT NOT NULL,
    start_price REAL,
    limit_price REAL,
    status TEXT NOT NULL,
    order_id INTEGER,
    perm_id INTEGER,
    filled_qty REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    dry_run INTEGER NOT NULL DEFAULT 0
  );
  CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    decided_at TEXT NOT NULL,
    model TEXT,
    underlying TEXT,
    direction TEXT,
    structure TEXT,
    confidence REAL,
    rationale TEXT,
    outcome TEXT,
    error TEXT
  );
  """,
  """
  CREATE TABLE fills (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    position_id TEXT,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL,
    multiplier INTEGER NOT NULL,
    commission_usd REAL NOT NULL,
    cash_usd REAL NOT NULL
  );
  CREATE INDEX fills_position ON fills (position_id);
  CREATE TABLE equity (
    day TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    capital_usd REAL NOT NULL,
    realized_usd REAL NOT NULL,
    unrealized_usd REAL NOT NULL,
    equity_usd REAL NOT NULL
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
