"""Uncovered-exposure maths."""
import datetime as dt

from src.guard.exposure import uncovered_exposure
from src.guard.models import Holding

E = dt.date(2026, 10, 27)

def opt(con_id: int, right: str, strike: float, qty: float) -> Holding:
  """Build an SPY option holding."""
  return Holding(con_id, "SPY", "OPT", qty, E, right, strike)

def test_debit_spreads_are_covered() -> None:
  """Long call below the short call, long put above the short put."""
  calls = uncovered_exposure([opt(1, "C", 600, 1), opt(2, "C", 601, -1)])
  puts = uncovered_exposure([opt(1, "P", 601, 1), opt(2, "P", 600, -1)])
  assert set(calls.values()) == set(puts.values()) == {0}

def test_credit_spread_and_naked_short_are_uncovered() -> None:
  """Long further OTM doesn't cover; no long at all doesn't either."""
  key = ("SPY", "short_option", "C", E)
  assert uncovered_exposure([opt(1, "C", 602, 1), opt(2, "C", 601, -1)])[key] == 1
  assert uncovered_exposure([opt(2, "C", 601, -2), opt(1, "C", 600, 1)])[key] == 1

def test_stock_covered_by_spare_options() -> None:
  """Short stock is covered by a spare long call; long stock by a spare long put."""
  short = Holding(9, "SPY", "STK", -100)
  assert uncovered_exposure([short, opt(1, "C", 600, 1)])[("SPY", "short_stock")] == 0
  assert uncovered_exposure([short])[("SPY", "short_stock")] == 100
  long = Holding(9, "SPY", "STK", 100)
  assert uncovered_exposure([long, opt(1, "P", 600, 1)])[("SPY", "long_stock")] == 0
