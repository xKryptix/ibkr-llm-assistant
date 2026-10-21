"""Classify an order's legs and compute its worst-case cost."""
import math
from dataclasses import dataclass, replace
from typing import Literal

from src.guard.models import Leg, OrderIntent

Structure = Literal["long_option", "debit_vertical"]

@dataclass(frozen=True)
class Classified:
  """A recognised opening structure."""

  structure: Structure
  long: Leg
  short: Leg | None

  @property
  def width(self) -> float | None:
    """Strike distance for a vertical."""
    if self.short is None:
      return None
    return abs(self.short.strike - self.long.strike)

def _flip(action: str) -> str:
  return "SELL" if action == "BUY" else "BUY"

def effective_legs(intent: OrderIntent) -> tuple[Leg, ...]:
  """Legs with the combo's order action applied (a SELL combo reverses each leg)."""
  if intent.action == "BUY":
    return intent.legs
  return tuple(replace(leg, action=_flip(leg.action)) for leg in intent.legs)

def classify_open(  # noqa: C901, PLR0911, PLR0912 (flat list of checks)
  intent: OrderIntent,
) -> tuple[Classified | None, str]:
  """Return the structure, or None and why it is not allowed.

  Allowed: one bought option, or a debit vertical (same underlying, expiry and
  right, 1:1, the sold leg further out of the money than the bought leg).
  """
  legs = effective_legs(intent)
  if not legs:
    return None, "no legs"
  if any(leg.sec_type != "OPT" for leg in legs):
    return None, "only options are allowed"
  if any(leg.ratio != 1 for leg in legs):
    return None, "every leg must have ratio 1"
  if any(
    leg.expiry is None or leg.right not in ("C", "P") or leg.strike is None
    for leg in legs
  ):
    return None, "leg missing expiry, right or strike"
  if any(leg.multiplier != 100 for leg in legs):
    return None, "only standard 100-multiplier options"

  if len(legs) == 1:
    [leg] = legs
    if leg.action != "BUY":
      return None, "single leg must be bought (no naked shorts)"
    return Classified("long_option", leg, None), ""

  if len(legs) != 2:
    return None, f"{len(legs)} legs; only 1 or 2 are allowed"
  buys = [leg for leg in legs if leg.action == "BUY"]
  sells = [leg for leg in legs if leg.action == "SELL"]
  if len(buys) != 1 or len(sells) != 1:
    return None, "a spread needs exactly one bought and one sold leg"
  long, short = buys[0], sells[0]
  if long.con_id == short.con_id:
    return None, "both legs are the same contract"
  if long.symbol != short.symbol:
    return None, "legs have different underlyings"
  if long.expiry != short.expiry:
    return None, "legs have different expiries"
  if long.right != short.right:
    return None, "legs mix calls and puts"
  if math.isclose(long.strike, short.strike):
    return None, "legs have the same strike"
  further_otm = (
    short.strike > long.strike if long.right == "C" else short.strike < long.strike
  )
  if not further_otm:
    return None, "sold leg must be further out of the money (debit vertical only)"
  return Classified("debit_vertical", long, short), ""

def natural_debit(c: Classified) -> float | None:
  """Price to open now at the far side of each quote, or None if unquoted."""
  if c.long.ask is None or (c.short is not None and c.short.bid is None):
    return None
  return c.long.ask - (c.short.bid if c.short else 0.0)

def round_trip_commission(
  legs: int, quantity: int, per_contract: float, min_per_order: float,
) -> float:
  """Estimated open + close commission (IBKR Pro fixed)."""
  return round(2 * max(legs * quantity * per_contract, min_per_order), 2)

def max_loss_usd(
  c: Classified, limit_price: float, quantity: int, commission_usd: float,
) -> float:
  """Most this order can lose: debit paid (capped at the width) plus commissions."""
  per_unit = limit_price if c.width is None else min(limit_price, c.width)
  return round(per_unit * 100 * quantity + commission_usd, 2)

def fingerprint(c: Classified) -> str:
  """Identify a structure regardless of price/quantity (for duplicate checks)."""
  short = "" if c.short is None else f"/{c.short.con_id}"
  return f"{c.long.symbol}:{c.long.expiry}:{c.long.right}:{c.long.con_id}{short}"
