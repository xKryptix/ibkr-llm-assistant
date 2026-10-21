"""Build candidate trades from option quotes (pure functions, no IBKR calls).

The same selection rules (delta targets, widths, debit-only) feed both the price
survey and the trading builder (src/bot/builder.py).
"""
import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal

Right = Literal["C", "P"]

@dataclass(frozen=True)
class OptionQuote:
  """One option's quote."""

  symbol: str
  expiry: dt.date
  right: Right
  strike: float
  bid: float
  ask: float
  delta: float | None
  con_id: int = 0
  quoted_at: dt.datetime | None = None

  @property
  def mid(self) -> float:
    """Midpoint of bid and ask."""
    return (self.bid + self.ask) / 2

  @property
  def spread_pct(self) -> float:
    """Bid/ask spread as % of mid (inf if mid is zero)."""
    return math.inf if self.mid <= 0 else (self.ask - self.bid) / self.mid * 100

  @property
  def has_market(self) -> bool:
    """True if the quote has a usable two-sided market."""
    return (
      _finite(self.bid) and _finite(self.ask)
      and self.bid >= 0 and self.ask > 0 and self.ask >= self.bid
    )

@dataclass(frozen=True)
class Candidate:
  """A long option or debit vertical, priced for 1 contract."""

  symbol: str
  expiry: dt.date
  dte: int
  right: Right
  structure: Literal["long_option", "debit_vertical"]
  width: float | None
  long: OptionQuote
  short: OptionQuote | None
  debit_mid: float
  debit_natural: float
  commission_usd: float

  @property
  def max_loss_mid_usd(self) -> float:
    """Max loss if filled at mid, including round-trip commissions."""
    return self.debit_mid * 100 + self.commission_usd

  @property
  def max_loss_natural_usd(self) -> float:
    """Max loss if filled at the natural price, including round-trip commissions."""
    return self.debit_natural * 100 + self.commission_usd

  @property
  def max_leg_spread_pct(self) -> float:
    """Widest bid/ask spread among the legs, as % of mid."""
    legs = [self.long] if self.short is None else [self.long, self.short]
    return max(leg.spread_pct for leg in legs)

def _finite(x: float) -> bool:
  return x is not None and not math.isnan(x)

def round_trip_commission(
  legs: int,
  quantity: int,
  per_contract: float,
  min_per_order: float,
) -> float:
  """Estimate open + close commission for one combo order (IBKR Pro fixed)."""
  per_order = max(legs * quantity * per_contract, min_per_order)
  return round(2 * per_order, 2)

def pick_by_delta(quotes: list[OptionQuote], target: float) -> OptionQuote | None:
  """Return the quote whose |delta| is nearest `target`, among usable quotes."""
  usable = [
    q for q in quotes
    if q.delta is not None and _finite(q.delta) and q.has_market and q.bid > 0
  ]
  if not usable:
    return None
  return min(usable, key=lambda q: (abs(abs(q.delta) - target), q.strike))

def build_long_option(
  quotes: list[OptionQuote],
  target_delta: float,
  today: dt.date,
  commission: float,
) -> Candidate | None:
  """Build a single long call/put near `target_delta`.

  `quotes` must all share one symbol, expiry and right.
  """
  leg = pick_by_delta(quotes, target_delta)
  if leg is None:
    return None
  return Candidate(
    symbol=leg.symbol,
    expiry=leg.expiry,
    dte=(leg.expiry - today).days,
    right=leg.right,
    structure="long_option",
    width=None,
    long=leg,
    short=None,
    debit_mid=round(leg.mid, 4),
    debit_natural=leg.ask,
    commission_usd=commission,
  )

def build_debit_vertical(
  quotes: list[OptionQuote],
  long_delta: float,
  width: float,
  today: dt.date,
  commission: float,
) -> Candidate | None:
  """Build a debit vertical: long near `long_delta`, short `width` further OTM.

  `quotes` must all share one symbol, expiry and right. Returns None unless the
  result is a genuine debit strictly between 0 and the width.
  """
  long_leg = pick_by_delta(quotes, long_delta)
  if long_leg is None:
    return None
  sign = 1 if long_leg.right == "C" else -1
  short_strike = round(long_leg.strike + sign * width, 4)
  short_leg = next(
    (q for q in quotes if math.isclose(q.strike, short_strike) and q.has_market),
    None,
  )
  if short_leg is None:
    return None
  debit_mid = round(long_leg.mid - short_leg.mid, 4)
  debit_natural = round(long_leg.ask - short_leg.bid, 4)
  if not 0 < debit_mid < width or debit_natural <= 0:
    return None
  return Candidate(
    symbol=long_leg.symbol,
    expiry=long_leg.expiry,
    dte=(long_leg.expiry - today).days,
    right=long_leg.right,
    structure="debit_vertical",
    width=width,
    long=long_leg,
    short=short_leg,
    debit_mid=debit_mid,
    debit_natural=min(debit_natural, width),
    commission_usd=commission,
  )
