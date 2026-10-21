"""Tests for survey storage and summary (no IBKR)."""
import datetime as dt
from pathlib import Path

from src.storage import connect
from src.storage.db import MIGRATIONS
from src.survey.price_survey import build_candidates, save, summarize
from src.utilities.strategy_settings import StrategySettings
from tests.test_candidates import CALLS, PUTS, TODAY

def test_build_candidates_per_expiry_and_right() -> None:
  """Each (expiry, right) gives a long option plus one vertical per width that fits."""
  candidates = build_candidates(CALLS + PUTS, TODAY, StrategySettings())
  kinds = sorted((c.right, c.structure, c.width or 0) for c in candidates)
  assert kinds == [
    ("C", "debit_vertical", 1.0), ("C", "debit_vertical", 2.0), ("C", "long_option", 0),
    ("P", "debit_vertical", 1.0), ("P", "debit_vertical", 2.0), ("P", "long_option", 0),
  ]

def test_save_and_summarize(tmp_path: Path) -> None:
  """Rows land in SQLite; summary counts what fits the cap."""
  conn = connect(tmp_path / "bot.sqlite")
  assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
  candidates = build_candidates(CALLS, TODAY, StrategySettings())
  save(conn, dt.datetime(2026, 10, 21, tzinfo=dt.UTC).isoformat(), live_data=True,
       audusd=0.65, cap_usd=100.0, spots={"SPY": 602.5}, candidates=candidates)
  assert conn.execute("SELECT COUNT(*) FROM price_survey").fetchone()[0] == 3
  fits = dict(conn.execute("SELECT structure || width, fits_cap FROM price_survey"
                           " WHERE structure = 'debit_vertical'").fetchall())
  assert fits == {"debit_vertical1.0": 1, "debit_vertical2.0": 0}

  rows = summarize(candidates, 100.0)
  long_row = next(r for r in rows if r["structure"] == "long_option")
  assert long_row["fit_cap"] == 0
  assert summarize(candidates, None)[0]["fit_cap"] is None

def test_migrations_are_idempotent(tmp_path: Path) -> None:
  """Reconnecting does not re-run migrations."""
  connect(tmp_path / "bot.sqlite").close()
  conn = connect(tmp_path / "bot.sqlite")
  assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
