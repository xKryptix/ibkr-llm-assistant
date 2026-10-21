"""Tests for the editable settings file."""
from pathlib import Path

import pytest

from src.utilities.strategy_settings import (
  EXAMPLE_FILE,
  StrategySettings,
  StrategySettingsStore,
  parse_strategy_settings,
)

def test_example_file_matches_defaults() -> None:
  """The shipped example file parses to exactly the code defaults."""
  assert parse_strategy_settings(EXAMPLE_FILE.read_text()) == StrategySettings()

def test_defaults_match_plan() -> None:
  """Defaults Sebastian accepted on 2026-10-21."""
  s = StrategySettings()
  assert s.capital.capital_aud == 500
  assert s.risk.risk_per_trade_pct == 15
  assert s.risk.max_open_positions == 3
  assert s.risk.max_trades_per_day == 2
  assert s.risk.max_drawdown_pct == 30
  assert (s.entries.min_dte, s.entries.max_dte) == (3, 10)

@pytest.mark.parametrize("toml", [
  "[risk]\nrisk_per_trade_pct = 60",
  "[entries]\nmin_dte = 2",
  "[entries]\nmin_dte = 8\nmax_dte = 5",
  "[entries]\nunderlyings = ['TSLA']",
  "[entries]\nvertical_widths = [0]",
  "[risk]\nunknown_key = 1",
  "[risk\nbroken",
])
def test_invalid_values_rejected(toml: str) -> None:
  """Out-of-range values, typos and broken TOML are errors."""
  with pytest.raises(ValueError):
    parse_strategy_settings(toml)

def test_store_creates_file_and_reloads(tmp_path: Path) -> None:
  """First run copies the example; edits are picked up; bad edits are rejected."""
  path = tmp_path / "data" / "settings.toml"
  store = StrategySettingsStore(path)
  assert path.exists()
  assert store.current == StrategySettings()

  path.write_text("[risk]\nrisk_per_trade_pct = 10\n")
  changes, error = store.reload()
  assert error is None
  assert changes == ["risk.risk_per_trade_pct: 15.0 -> 10.0"]
  assert store.current.risk.risk_per_trade_pct == 10

  path.write_text("[risk]\nrisk_per_trade_pct = 99\n")
  changes, error = store.reload()
  assert changes == []
  assert error
  assert store.current.risk.risk_per_trade_pct == 10
