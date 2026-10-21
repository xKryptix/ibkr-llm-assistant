"""A valid opening order and context, for tests to break one thing at a time."""
import datetime as dt
from dataclasses import replace

from src.guard.gates import GuardContext
from src.guard.models import (
  AccountState,
  BookState,
  Holding,
  Leg,
  LiveFlags,
  MarketClock,
  OpenPosition,
  OrderIntent,
  UnderlyingInfo,
  WhatIf,
)
from src.utilities.strategy_settings import StrategySettings

TODAY = dt.date(2026, 10, 21)
EXPIRY = dt.date(2026, 10, 27)  # 6 DTE
NOW = dt.datetime(2026, 10, 21, 15, 0, tzinfo=dt.UTC)  # 11:00 New York
SESSION_OPEN = dt.datetime(2026, 10, 21, 13, 30, tzinfo=dt.UTC)
SESSION_CLOSE = dt.datetime(2026, 10, 21, 20, 0, tzinfo=dt.UTC)

LONG_CALL = Leg(
  con_id=1001, symbol="SPY", sec_type="OPT", action="BUY", expiry=EXPIRY,
  right="C", strike=602.0, bid=3.85, ask=3.95, quote_age_seconds=2,
)
SHORT_CALL = Leg(
  con_id=1002, symbol="SPY", sec_type="OPT", action="SELL", expiry=EXPIRY,
  right="C", strike=603.0, bid=3.45, ask=3.55, quote_age_seconds=2,
)
LONG_PUT = Leg(
  con_id=2001, symbol="SPY", sec_type="OPT", action="BUY", expiry=EXPIRY,
  right="P", strike=603.0, bid=3.85, ask=3.95, quote_age_seconds=2,
)
SHORT_PUT = Leg(
  con_id=2002, symbol="SPY", sec_type="OPT", action="SELL", expiry=EXPIRY,
  right="P", strike=602.0, bid=3.45, ask=3.55, quote_age_seconds=2,
)
STOCK = Leg(con_id=756733, symbol="SPY", sec_type="STK", action="BUY",
            multiplier=1, bid=602.4, ask=602.5, quote_age_seconds=1)

# $1-wide SPY call debit spread: natural 0.50, limit 0.42.
# Max loss = 42 + 2.60 commissions = $44.60; cap = 15% of $330 = $49.50.
OPEN_INTENT = OrderIntent(
  purpose="open", legs=(LONG_CALL, SHORT_CALL), action="BUY", quantity=1,
  order_type="LMT", limit_price=0.42, tif="DAY",
)

def open_ctx(**changes: object) -> GuardContext:
  """Build a context that passes every opening gate, with `changes` applied."""
  ctx = GuardContext(
    intent=OPEN_INTENT,
    account=AccountState(account_id="DU1234567", usd_cash=330.0),
    book=BookState(capital_usd=330.0, realized_pnl_usd=0.0),
    clock=MarketClock(now=NOW, today=TODAY, session_open=SESSION_OPEN,
                      session_close=SESSION_CLOSE),
    settings=StrategySettings(),
    live=LiveFlags(),
    underlyings={
      "SPY": UnderlyingInfo(price=602.5, next_ex_dividend=dt.date(2026, 12, 18),
                            ex_dividend_known=True),
      "QQQ": UnderlyingInfo(price=520.0, next_ex_dividend=None,
                            ex_dividend_known=True),
    },
    whatif=WhatIf(warning=None, commission_usd=1.30),
  )
  return replace(ctx, **changes)

def intent(**changes: object) -> OrderIntent:
  """Build the valid opening intent with `changes` applied."""
  return replace(OPEN_INTENT, **changes)

def position(**changes: object) -> OpenPosition:
  """Build an open position (default: the call spread, opened yesterday)."""
  p = OpenPosition(
    position_id="P1", symbol="SPY", expiry=EXPIRY, right="C",
    structure="debit_vertical", quantity=1, max_loss_usd=44.60,
    opened_on=TODAY - dt.timedelta(days=1),
  )
  return replace(p, **changes)

CLOSE_INTENT = OrderIntent(
  purpose="close", legs=(LONG_CALL, SHORT_CALL), action="SELL", quantity=1,
  order_type="LMT", limit_price=0.60, tif="DAY", close_reason="profit_target",
)

def held(leg: Leg, quantity: float) -> Holding:
  """Build a holding of `leg`'s contract."""
  return Holding(con_id=leg.con_id, symbol=leg.symbol, sec_type=leg.sec_type,
                 quantity=quantity, expiry=leg.expiry, right=leg.right,
                 strike=leg.strike)

def holdings(*items: Holding) -> dict[int, Holding]:
  """Index holdings by conId."""
  return {h.con_id: h for h in items}

SPREAD_HELD = holdings(held(LONG_CALL, 1), held(SHORT_CALL, -1))

def close_ctx(**changes: object) -> GuardContext:
  """Build a context that passes every closing gate (holding the spread)."""
  ctx = open_ctx(
    intent=CLOSE_INTENT,
    account=AccountState(account_id="DU1234567", usd_cash=290.0,
                         holdings=SPREAD_HELD),
    book=BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                   open_positions=(position(),)),
    whatif=None,
  )
  return replace(ctx, **changes)
