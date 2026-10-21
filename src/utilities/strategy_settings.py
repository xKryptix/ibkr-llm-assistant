"""Strategy and risk settings loaded from `data/settings.toml` (PLAN.md §5.7).

The file is re-read on every loop. An invalid edit is rejected as a whole and the
previous settings stay in force.
"""
import shutil
import tomllib
from pathlib import Path
from typing import Literal, Self

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

EXAMPLE_FILE = Path(__file__).resolve().parents[2] / "settings.example.toml"

class _Section(BaseModel):
  model_config = ConfigDict(extra="forbid", frozen=True)

class CapitalSettings(_Section):
  """Money given to the bot."""

  capital_aud: float = Field(500, gt=0)

class RiskSettings(_Section):
  """Per-trade and book-wide limits."""

  risk_per_trade_pct: float = Field(15, gt=0, le=50)
  max_open_positions: int = Field(3, ge=1, le=20)
  max_trades_per_day: int = Field(2, ge=1, le=20)
  max_drawdown_pct: float = Field(30, gt=0, le=100)
  max_consecutive_losses: int = Field(4, ge=1, le=50)
  max_contracts: int = Field(2, ge=1, le=50)

class EntrySettings(_Section):
  """What and when to open."""

  underlyings: list[Literal["SPY", "QQQ"]] = Field(
    default_factory=lambda: ["SPY", "QQQ"], min_length=1,
  )
  min_dte: int = Field(3, ge=3)
  max_dte: int = Field(10, ge=3, le=45)
  long_option_delta: float = Field(0.40, gt=0.05, lt=0.95)
  vertical_long_delta: float = Field(0.50, gt=0.05, lt=0.95)
  vertical_widths: list[float] = Field(
    default_factory=lambda: [1.0, 2.0], min_length=1,
  )
  max_leg_spread_pct: float = Field(10, gt=0, le=100)
  entry_interval_minutes: int = Field(30, ge=5, le=390)
  skip_first_minutes: int = Field(15, ge=0, le=120)
  skip_last_minutes: int = Field(30, ge=0, le=120)

  @model_validator(mode="after")
  def _check(self) -> Self:
    if self.max_dte < self.min_dte:
      msg = "max_dte must be >= min_dte"
      raise ValueError(msg)
    if any(w <= 0 or w > 10 for w in self.vertical_widths):
      msg = "vertical_widths must be between 0 and 10 dollars"
      raise ValueError(msg)
    return self

class ExitSettings(_Section):
  """When to close."""

  stop_loss_pct: float = Field(50, gt=0, lt=100)
  long_option_profit_target_pct: float = Field(150, gt=100, le=1000)
  vertical_profit_target_pct_of_width: float = Field(80, gt=0, le=100)
  max_hold_days: int = Field(4, ge=1, le=30)
  exit_interval_minutes: int = Field(15, ge=1, le=60)

class BrainSettings(_Section):
  """Claude settings for the trading brain."""

  model: str = "claude-sonnet-5-5"
  min_confidence: float = Field(0.6, ge=0, le=1)

class CostSettings(_Section):
  """Commission estimates (USD)."""

  commission_per_contract: float = Field(0.65, ge=0, le=10)
  commission_min_per_order: float = Field(1.00, ge=0, le=10)

class GoLiveSettings(_Section):
  """Paper results required before going live."""

  min_closed_trades: int = Field(40, ge=1)
  max_drawdown_pct: float = Field(25, gt=0, le=100)
  paper_fill_haircut_per_leg: float = Field(0.02, ge=0, le=1)

class SurveySettings(_Section):
  """Phase 1 price survey."""

  interval_minutes: int = Field(30, ge=5, le=390)
  strike_window_pct: float = Field(1.5, gt=0, le=10)

class StrategySettings(_Section):
  """All user-editable settings."""

  capital: CapitalSettings = CapitalSettings()
  risk: RiskSettings = RiskSettings()
  entries: EntrySettings = EntrySettings()
  exits: ExitSettings = ExitSettings()
  brain: BrainSettings = BrainSettings()
  costs: CostSettings = CostSettings()
  go_live: GoLiveSettings = GoLiveSettings()
  survey: SurveySettings = SurveySettings()

def parse_strategy_settings(text: str) -> StrategySettings:
  """Parse and validate settings TOML text.

  Raises:
    ValueError: If the TOML is malformed or any value is invalid.

  """
  try:
    return StrategySettings.model_validate(tomllib.loads(text))
  except (tomllib.TOMLDecodeError, ValidationError) as e:
    raise ValueError(str(e)) from e

def diff_settings(old: StrategySettings, new: StrategySettings) -> list[str]:
  """List human-readable changes between two settings objects."""
  changes = []
  old_d, new_d = old.model_dump(), new.model_dump()
  for section, values in new_d.items():
    for key, value in values.items():
      if old_d[section][key] != value:
        changes.append(f"{section}.{key}: {old_d[section][key]} -> {value}")
  return changes

class StrategySettingsStore:
  """Holds the current settings and reloads them from disk on request."""

  def __init__(self, path: Path) -> None:
    """Load settings from `path`, creating it from the example file if missing.

    Raises:
      ValueError: If the file exists but is invalid on first load.

    """
    self.path = path
    if not path.exists():
      path.parent.mkdir(parents=True, exist_ok=True)
      shutil.copyfile(EXAMPLE_FILE, path)
      logger.info("Created {} from {}", path, EXAMPLE_FILE.name)
    self.current = parse_strategy_settings(path.read_text())

  def reload(self) -> tuple[list[str], str | None]:
    """Re-read the file.

    Returns:
      (changes, error). On error the previous settings are kept and `changes`
      is empty.

    """
    try:
      new = parse_strategy_settings(self.path.read_text())
    except (OSError, ValueError) as e:
      logger.warning("Rejected settings edit, keeping previous settings: {}", e)
      return [], str(e)
    changes = diff_settings(self.current, new)
    if changes:
      logger.info("Settings changed: {}", changes)
    self.current = new
    return changes, None
