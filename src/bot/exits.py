"""Exit rules and closing orders (pure; PLAN.md §5.5). Claude is not involved."""
import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from src.bot.broker import LegQuote
from src.bot.builder import TICK, ticks
from src.bot.store import PositionRow
from src.guard.models import EMERGENCY_CLOSE_REASONS, CloseReason, Leg, OrderIntent
from src.utilities.strategy_settings import StrategySettings

CLOSE_BY_DTE = 2  # hard rule, not a setting
EMERGENCY_STEP = 0.05
LADDER_STEPS = 3

@dataclass(frozen=True)
class ClosePlan:
  """A closing order: start at `start_price`, never below `intent.limit_price`."""

  intent: OrderIntent
  start_price: float
  step: float

def _finite(*xs: float | None) -> bool:
  return all(x is not None and math.isfinite(x) for x in xs)

def marks(
  p: PositionRow, quotes: dict[int, LegQuote],
) -> tuple[float | None, float | None]:
  """(mid value, value at the unfavourable side) per unit, or None if unquoted."""
  long = quotes.get(p.long_con_id)
  short = quotes.get(p.short_con_id) if p.short_con_id else None
  if long is None or not _finite(long.bid, long.ask) or (
    p.short_con_id and (short is None or not _finite(short.bid, short.ask))
  ):
    return None, None
  mid = long.mid - (short.mid if short else 0.0)
  worst = long.bid - (short.ask if short else 0.0)
  return max(mid, 0.0), max(worst, 0.0)

def trading_days_held(p: PositionRow, today: dt.date) -> int:
  """Weekdays from the opening day to today."""
  return int(np.busday_count(p.opened_on, today))

def exit_reason(
  p: PositionRow, value: float | None, today: dt.date, settings: StrategySettings,
) -> CloseReason | None:
  """Return the first exit rule that applies (§5.5 order), or None to hold."""
  if (p.expiry - today).days <= CLOSE_BY_DTE:
    return "expiry"
  if value is not None:
    x = settings.exits
    if value <= p.entry_price * x.stop_loss_pct / 100:
      return "stop"
    if p.width is None:
      target = p.entry_price * x.long_option_profit_target_pct / 100
    else:
      target = p.width * x.vertical_profit_target_pct_of_width / 100
    if value >= target:
      return "profit_target"
  if trading_days_held(p, today) >= settings.exits.max_hold_days:
    return "time_stop"
  return None

def take_profit_price(p: PositionRow, settings: StrategySettings) -> float:
  """Price for the resting take-profit order."""
  x = settings.exits
  if p.width is None:
    return ticks(p.entry_price * x.long_option_profit_target_pct / 100)
  return ticks(min(p.width * x.vertical_profit_target_pct_of_width / 100,
                   p.width - TICK))

def position_legs(p: PositionRow, quotes: dict[int, LegQuote]) -> tuple[Leg, ...]:
  """Return the position's legs as they were bought, with current quotes."""
  def leg(con_id: int, action: str, strike: float) -> Leg:
    q = quotes.get(con_id)
    return Leg(
      con_id=con_id, symbol=p.symbol, sec_type="OPT", action=action,
      expiry=p.expiry, right=p.right, strike=strike,
      bid=None if q is None else q.bid, ask=None if q is None else q.ask,
    )
  legs = [leg(p.long_con_id, "BUY", p.long_strike)]
  if p.short_con_id:
    legs.append(leg(p.short_con_id, "SELL", p.short_strike))
  return tuple(legs)

def close_plan(
  p: PositionRow, quotes: dict[int, LegQuote], reason: CloseReason,
  *, tif: str = "DAY", price: float | None = None,
) -> ClosePlan | None:
  """Sell the whole position back. None if it can't be priced."""
  mid, worst = marks(p, quotes)
  legs = position_legs(p, quotes)
  if price is not None:  # resting take-profit at a fixed price
    intent = OrderIntent(purpose="close", legs=legs, action="SELL",
                         quantity=p.quantity, order_type="LMT", limit_price=price,
                         tif=tif, close_reason=reason)
    return ClosePlan(intent, price, 0.0)
  if mid is None:
    return None
  if reason in EMERGENCY_CLOSE_REASONS:
    start = max(ticks(worst), TICK)
    floor = max(ticks(worst - LADDER_STEPS * EMERGENCY_STEP), TICK)
    step = EMERGENCY_STEP
  else:
    start = max(ticks(mid), TICK)
    floor = max(ticks(max(worst, start - LADDER_STEPS * TICK)), TICK)
    step = TICK
  floor = min(floor, start)
  intent = OrderIntent(purpose="close", legs=legs, action="SELL",
                       quantity=p.quantity, order_type="LMT", limit_price=floor,
                       tif=tif, close_reason=reason)
  return ClosePlan(intent, start, step)

def stock_close_plan(
  con_id: int, symbol: str, shares: float, quote: LegQuote | None,
) -> ClosePlan | None:
  """Flatten stock left by an assignment with a marketable limit order."""
  if quote is None or not _finite(quote.bid, quote.ask) or shares == 0:
    return None
  buying = shares < 0
  qty = int(abs(shares))
  if buying:
    start = round(quote.ask, 2)
    worst = round(quote.ask * 1.005, 2)
  else:
    start = round(quote.bid, 2)
    worst = round(quote.bid * 0.995, 2)
  leg = Leg(con_id=con_id, symbol=symbol, sec_type="STK", action="BUY",
            multiplier=1, bid=quote.bid, ask=quote.ask)
  intent = OrderIntent(purpose="close", legs=(leg,),
                       action="BUY" if buying else "SELL", quantity=qty,
                       order_type="LMT", limit_price=worst, tif="DAY",
                       close_reason="assignment")
  step = round(abs(worst - start) / LADDER_STEPS, 2) or TICK
  return ClosePlan(intent, start, step)
