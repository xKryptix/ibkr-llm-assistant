"""Persistent kill switch and HALT file (PLAN.md §5.3).

Once tripped, the kill switch stays on until reset by hand
(`main.py --reset-kill-switch`); restarts and settings edits don't reset it.
"""
import datetime as dt
import sqlite3
from pathlib import Path

from loguru import logger

from src.utilities.strategy_settings import StrategySettings

def kill_reason(
  equity_usd: float,
  capital_usd: float,
  consecutive_losses: int,
  settings: StrategySettings,
) -> str | None:
  """Why the kill switch should trip now, or None."""
  floor = capital_usd * (1 - settings.risk.max_drawdown_pct / 100)
  if not equity_usd >= floor:  # also trips on NaN
    return (f"equity ${equity_usd:.2f} below drawdown floor ${floor:.2f} "
            f"({settings.risk.max_drawdown_pct:g}%)")
  if consecutive_losses >= settings.risk.max_consecutive_losses:
    return f"{consecutive_losses} losing trades in a row"
  return None

class KillSwitch:
  """Kill switch state stored in SQLite."""

  def __init__(self, conn: sqlite3.Connection) -> None:
    """Use the bot database."""
    self.conn = conn

  def _latest(self) -> sqlite3.Row | None:
    return self.conn.execute(
      "SELECT * FROM kill_switch ORDER BY id DESC LIMIT 1",
    ).fetchone()

  @property
  def tripped(self) -> bool:
    """True from a trip until a manual reset."""
    row = self._latest()
    return row is not None and row["reset_at"] is None

  @property
  def reason(self) -> str | None:
    """Why it is tripped, if it is."""
    row = self._latest()
    return row["reason"] if row is not None and row["reset_at"] is None else None

  def trip(self, reason: str) -> bool:
    """Trip it. Returns False if it was already tripped."""
    if self.tripped:
      return False
    with self.conn:
      self.conn.execute(
        "INSERT INTO kill_switch (tripped_at, reason) VALUES (?, ?)",
        (_now(), reason),
      )
    logger.critical("KILL SWITCH TRIPPED: {}", reason)
    return True

  def reset(self) -> bool:
    """Manual reset. Returns False if it wasn't tripped."""
    row = self._latest()
    if row is None or row["reset_at"] is not None:
      return False
    with self.conn:
      self.conn.execute(
        "UPDATE kill_switch SET reset_at = ? WHERE id = ?", (_now(), row["id"]),
      )
    logger.warning("Kill switch reset by hand (was: {})", row["reason"])
    return True

def halt_file_present(data_dir: Path) -> bool:
  """Check for a HALT file, which stops new entries (exits continue)."""
  return (data_dir / "HALT").exists()

def _now() -> str:
  return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
