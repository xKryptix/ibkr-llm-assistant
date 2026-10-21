"""Turn Claude's view into a concrete, sized order (pure; no IBKR calls).

Claude only picks direction and structure. Strikes, expiry, quantity and price
are decided here, and the result still has to pass the guard.
"""
import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal

from src.guard.models import Leg, OrderIntent
from src.guard.structure import round_trip_commission
from src.survey.candidates import (
  Candidate,
  OptionQuote,
  build_debit_vertical,
  build_long_option,
)
from src.utilities.strategy_settings import StrategySettings

TICK = 0.01
LADDER_STEPS = 3
PREFERRED_DTE = 6

@dataclass(frozen=True)
class TradePlan:
  """An order ready for the guard.

  `intent.limit_price` is the worst price the executor may reach (the guard
  approves that); the executor starts at `start_price` and moves towards it.
  """

  intent: OrderIntent
  start_price: float
  candidate: Candidate
  max_loss_usd: float

def target_dte(settings: StrategySettings) -> int:
  """Preferred days to expiry, kept inside the configured range."""
  e = settings.entries
  return min(max(PREFERRED_DTE, e.min_dte), e.max_dte)

def ticks(x: float) -> float:
  """Round to the option tick."""
  return round(round(x / TICK) * TICK, 2)

def legs_for(candidate: Candidate, now: dt.datetime) -> tuple[Leg, ...]:
  """Order legs (as bought) with their quotes."""
  def leg(q: OptionQuote, action: Literal["BUY", "SELL"]) -> Leg:
    age = None if q.quoted_at is None else (now - q.quoted_at).total_seconds()
    return Leg(con_id=q.con_id, symbol=q.symbol, sec_type="OPT", action=action,
               expiry=q.expiry, right=q.right, strike=q.strike, bid=q.bid,
               ask=q.ask, quote_age_seconds=age)
  legs = [leg(candidate.long, "BUY")]
  if candidate.short is not None:
    legs.append(leg(candidate.short, "SELL"))
  return tuple(legs)

def _sized(
  candidate: Candidate, settings: StrategySettings, capital_usd: float,
) -> tuple[float, float, int, float] | None:
  """(start, ceiling, quantity, max loss) or None if even one doesn't fit."""
  start = max(ticks(candidate.debit_mid), TICK)
  natural = ticks(candidate.debit_natural)
  ceiling = min(natural, ticks(start + LADDER_STEPS * TICK))
  if candidate.width is not None:
    ceiling = min(ceiling, ticks(candidate.width - TICK))
  start = min(start, ceiling)
  if ceiling <= 0 or not math.isfinite(ceiling):
    return None
  cap = capital_usd * settings.risk.risk_per_trade_pct / 100
  legs = 1 if candidate.short is None else 2
  costs = settings.costs
  for qty in range(settings.risk.max_contracts, 0, -1):
    fee = round_trip_commission(legs, qty, costs.commission_per_contract,
                                costs.commission_min_per_order)
    loss = round(ceiling * 100 * qty + fee, 2)
    if loss <= cap:
      return start, ceiling, qty, loss
  return None

def plan_entry(  # noqa: PLR0913
  direction: Literal["bullish", "bearish"],
  structure: Literal["long_option", "debit_vertical"],
  quotes: list[OptionQuote],
  today: dt.date,
  now: dt.datetime,
  settings: StrategySettings,
  capital_usd: float,
) -> tuple[TradePlan | None, str]:
  """Pick the trade, or explain why none fits."""
  right = "C" if direction == "bullish" else "P"
  group = [q for q in quotes if q.right == right]
  if not group:
    return None, f"no {right} quotes"
  expiry = min({q.expiry for q in group},
               key=lambda e: (abs((e - today).days - target_dte(settings)), e))
  group = [q for q in group if q.expiry == expiry]
  e, costs = settings.entries, settings.costs
  if structure == "long_option":
    fee = round_trip_commission(1, 1, costs.commission_per_contract,
                                costs.commission_min_per_order)
    candidates = [build_long_option(group, e.long_option_delta, today, fee)]
  else:
    fee = round_trip_commission(2, 1, costs.commission_per_contract,
                                costs.commission_min_per_order)
    candidates = [build_debit_vertical(group, e.vertical_long_delta, w, today, fee)
                  for w in e.vertical_widths]
  candidates = [c for c in candidates if c is not None]
  if not candidates:
    return None, f"no usable {structure} at {expiry}"
  for c in candidates:
    sized = _sized(c, settings, capital_usd)
    if sized is None:
      continue
    start, ceiling, qty, loss = sized
    intent = OrderIntent(
      purpose="open", legs=legs_for(c, now), action="BUY", quantity=qty,
      order_type="LMT", limit_price=ceiling, tif="DAY",
    )
    return TradePlan(intent, start, c, loss), ""
  cheapest = min(c.max_loss_mid_usd for c in candidates)
  return None, (f"cheapest {structure} costs ${cheapest:.2f}, over the "
                f"{settings.risk.risk_per_trade_pct:g}% per-trade cap")
