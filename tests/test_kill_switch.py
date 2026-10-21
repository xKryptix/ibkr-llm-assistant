"""Kill switch: trips on drawdown or losing streak and stays tripped."""
import math
from pathlib import Path

from src.guard import KillSwitch, halt_file_present, kill_reason
from src.storage import connect
from src.utilities.strategy_settings import StrategySettings

S = StrategySettings()  # 30% drawdown, 4 losses in a row

def test_kill_reason() -> None:
  """Drawdown floor is 70% of capital; NaN equity also trips."""
  assert kill_reason(231.0, 330.0, 0, S) is None
  assert "drawdown" in kill_reason(230.9, 330.0, 0, S)
  assert "drawdown" in kill_reason(math.nan, 330.0, 0, S)
  assert "losing trades" in kill_reason(330.0, 330.0, 4, S)
  assert kill_reason(330.0, 330.0, 3, S) is None

def test_trip_persists_until_manual_reset(tmp_path: Path) -> None:
  """A restart (new connection) keeps it tripped; only reset() clears it."""
  db = tmp_path / "bot.sqlite"
  switch = KillSwitch(connect(db))
  assert not switch.tripped
  assert switch.reset() is False
  assert switch.trip("equity below floor")
  assert switch.trip("again") is False

  reopened = KillSwitch(connect(db))
  assert reopened.tripped
  assert reopened.reason == "equity below floor"
  assert reopened.reset()
  assert not reopened.tripped
  assert reopened.reason is None

def test_halt_file(tmp_path: Path) -> None:
  """A HALT file in the data directory stops entries."""
  assert not halt_file_present(tmp_path)
  (tmp_path / "HALT").touch()
  assert halt_file_present(tmp_path)
