"""Tests for candidate building (pure, no IBKR)."""
import datetime as dt
import math

from src.survey.candidates import (
  OptionQuote,
  build_debit_vertical,
  build_long_option,
  pick_by_delta,
  round_trip_commission,
)

TODAY = dt.date(2026, 10, 21)
EXPIRY = dt.date(2026, 10, 27)

def q(
  strike: float, bid: float, ask: float, delta: float | None, right: str = "C",
) -> OptionQuote:
  """Make a quote."""
  return OptionQuote("SPY", EXPIRY, right, strike, bid, ask, delta)

CALLS = [
  q(600, 5.00, 5.10, 0.60),
  q(601, 4.40, 4.50, 0.55),
  q(602, 3.85, 3.95, 0.50),
  q(603, 3.30, 3.40, 0.45),
  q(604, 2.85, 2.95, 0.40),
  q(605, 2.40, 2.50, 0.35),
]
PUTS = [
  q(598, 2.40, 2.50, -0.35, "P"),
  q(599, 2.85, 2.95, -0.40, "P"),
  q(600, 3.30, 3.40, -0.45, "P"),
  q(601, 3.85, 3.95, -0.50, "P"),
]

def test_round_trip_commission_uses_order_minimum() -> None:
  """A single contract pays the $1 minimum each way."""
  assert round_trip_commission(1, 1, 0.65, 1.00) == 2.00
  assert round_trip_commission(2, 1, 0.65, 1.00) == 2.60
  assert round_trip_commission(2, 2, 0.65, 1.00) == 5.20

def test_pick_by_delta_uses_absolute_delta_and_skips_bad_quotes() -> None:
  """Put deltas are negative; quotes without delta or bid are ignored."""
  assert pick_by_delta(PUTS, 0.40).strike == 599
  quotes = [q(602, 0, 3.95, 0.50), q(603, 3.30, 3.40, None), q(604, 2.85, 2.95, 0.40)]
  assert pick_by_delta(quotes, 0.50).strike == 604
  assert pick_by_delta([q(602, math.nan, math.nan, 0.5)], 0.5) is None

def test_long_option() -> None:
  """Long call near 0.40 delta, cost includes commissions."""
  c = build_long_option(CALLS, 0.40, TODAY, 2.00)
  assert c.long.strike == 604
  assert c.dte == 6
  assert c.debit_natural == 2.95
  assert math.isclose(c.max_loss_mid_usd, 290 + 2.00)

def test_call_vertical_short_leg_is_higher_strike() -> None:
  """Call debit spread: buy 0.50 delta, sell $1 higher."""
  c = build_debit_vertical(CALLS, 0.50, 1.0, TODAY, 2.60)
  assert (c.long.strike, c.short.strike) == (602, 603)
  assert math.isclose(c.debit_mid, 3.90 - 3.35)
  assert math.isclose(c.debit_natural, 3.95 - 3.30)
  assert math.isclose(c.max_loss_mid_usd, 55 + 2.60)

def test_put_vertical_short_leg_is_lower_strike() -> None:
  """Put debit spread: buy 0.50 delta put, sell $2 lower."""
  c = build_debit_vertical(PUTS, 0.50, 2.0, TODAY, 2.60)
  assert (c.long.strike, c.short.strike) == (601, 599)
  assert c.short.strike < c.long.strike

def test_vertical_missing_short_strike_returns_none() -> None:
  """No strike `width` away means no candidate."""
  assert build_debit_vertical(CALLS, 0.35, 1.0, TODAY, 2.60) is None

def test_vertical_must_be_a_debit_below_width() -> None:
  """A crossed or zero-width debit is rejected."""
  bad = [q(602, 3.85, 3.95, 0.50), q(603, 3.95, 4.05, 0.45)]
  assert build_debit_vertical(bad, 0.50, 1.0, TODAY, 2.60) is None
  too_wide = [q(602, 3.85, 3.95, 0.50), q(603, 0.00, 0.05, 0.02)]
  assert build_debit_vertical(too_wide, 0.50, 1.0, TODAY, 2.60) is None
