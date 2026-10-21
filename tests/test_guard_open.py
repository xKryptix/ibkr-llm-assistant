"""Adversarial tests: every way of sneaking a bad opening order past the guard."""
import datetime as dt
import math
from dataclasses import replace

import pytest

from src.guard import Approval, Decision, evaluate
from src.guard.models import AccountState, BookState, LiveFlags, UnderlyingInfo, WhatIf
from tests.guard_fixtures import (
  EXPIRY,
  LONG_CALL,
  LONG_PUT,
  NOW,
  SESSION_CLOSE,
  SESSION_OPEN,
  SHORT_CALL,
  SHORT_PUT,
  STOCK,
  TODAY,
  intent,
  open_ctx,
  position,
)

def failed_gates(ctx: object) -> set[str]:
  """Names of the gates that rejected the order."""
  decision, approval = evaluate(ctx)
  assert approval is None
  assert not decision.approved
  return {r.gate for r in decision.failures}

# ------------------------------------------------------------------ the happy path

def test_valid_call_debit_spread_is_approved() -> None:
  """The baseline order passes every gate and gets an Approval."""
  decision, approval = evaluate(open_ctx())
  assert decision.approved, decision.failures
  assert isinstance(approval, Approval)
  assert approval.intent.limit_price == 0.42

def test_valid_put_debit_spread_and_long_option_are_approved() -> None:
  """Put debit spread and a single long call are the other allowed shapes."""
  puts = intent(legs=(LONG_PUT, SHORT_PUT))
  assert evaluate(open_ctx(intent=puts))[0].approved
  single = replace(LONG_CALL, bid=0.40, ask=0.42)
  long_call = intent(legs=(single,), limit_price=0.41)
  assert evaluate(open_ctx(intent=long_call))[0].approved

def test_approval_cannot_be_forged() -> None:
  """Only evaluate() can make an Approval, and never for a rejected order."""
  decision, _ = evaluate(open_ctx())
  with pytest.raises(PermissionError):
    Approval(decision, object())
  rejected = Decision(decision.intent, ())
  with pytest.raises(PermissionError):
    Approval(rejected, object())

# ------------------------------------------------------- undefined-risk structures

@pytest.mark.parametrize(("legs", "action"), [
  pytest.param((replace(LONG_CALL, action="SELL"),), "BUY", id="naked short call"),
  pytest.param((replace(LONG_PUT, action="SELL"),), "BUY", id="naked short put"),
  pytest.param((LONG_CALL,), "SELL", id="sell-to-open single via combo action"),
  pytest.param((LONG_CALL, SHORT_CALL), "SELL", id="spread sold = credit spread"),
  pytest.param((replace(LONG_CALL, action="SELL"), SHORT_CALL), "BUY",
               id="two short legs"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, action="BUY")), "BUY",
               id="two long legs"),
  pytest.param((replace(LONG_CALL, strike=603.0), replace(SHORT_CALL, strike=602.0)),
               "BUY", id="call credit vertical (sold leg nearer the money)"),
  pytest.param((replace(LONG_PUT, strike=602.0), replace(SHORT_PUT, strike=603.0)),
               "BUY", id="put credit vertical"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, ratio=2)), "BUY", id="ratio 1:2"),
  pytest.param((replace(LONG_CALL, ratio=2), SHORT_CALL), "BUY", id="ratio 2:1"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, expiry=EXPIRY + dt.timedelta(days=7))),
               "BUY", id="calendar (different expiry)"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, right="P")), "BUY",
               id="mixed call and put"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, strike=602.0)), "BUY",
               id="same strike"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, con_id=1001)), "BUY",
               id="same contract twice"),
  pytest.param((LONG_CALL, replace(SHORT_CALL, symbol="QQQ")), "BUY",
               id="different underlyings"),
  pytest.param((LONG_CALL, SHORT_CALL, replace(SHORT_CALL, con_id=1003,
               strike=604.0)), "BUY", id="three legs"),
  pytest.param((LONG_CALL, SHORT_CALL, LONG_PUT, SHORT_PUT), "BUY", id="condor"),
  pytest.param((STOCK,), "BUY", id="stock"),
  pytest.param((LONG_CALL, replace(STOCK, action="SELL")), "BUY",
               id="option plus short stock"),
  pytest.param((replace(LONG_CALL, sec_type="FOP"),), "BUY", id="futures option"),
  pytest.param((replace(LONG_CALL, multiplier=10),), "BUY", id="mini option"),
  pytest.param((replace(LONG_CALL, strike=None),), "BUY", id="missing strike"),
  pytest.param((replace(LONG_CALL, strike=math.nan), SHORT_CALL), "BUY",
               id="NaN strike"),
  pytest.param((), "BUY", id="no legs"),
])
def test_undefined_risk_structures_rejected(legs: tuple, action: str) -> None:
  """Anything but a long option or a debit vertical fails the structure gate."""
  assert "structure" in failed_gates(open_ctx(intent=intent(legs=legs, action=action)))

def test_wrong_underlying_rejected() -> None:
  """Only SPY and QQQ."""
  legs = tuple(replace(leg, symbol="TSLA") for leg in (LONG_CALL, SHORT_CALL))
  assert "underlying" in failed_gates(open_ctx(intent=intent(legs=legs)))

# ------------------------------------------------------------------- order fields

@pytest.mark.parametrize(("changes", "gate"), [
  ({"order_type": "MKT"}, "order_type"),
  ({"order_type": "STP"}, "order_type"),
  ({"tif": "GTC"}, "order_type"),
  ({"limit_price": None}, "debit_price"),
  ({"limit_price": math.nan}, "debit_price"),
  ({"limit_price": math.inf}, "debit_price"),
  ({"limit_price": 0.0}, "debit_price"),
  ({"limit_price": -0.30}, "debit_price"),
  ({"limit_price": 1.00}, "debit_price"),   # = width: can't profit
  ({"limit_price": 0.55}, "debit_price"),   # above natural 0.50
  ({"quantity": 0}, "quantity"),
  ({"quantity": -1}, "quantity"),
  ({"quantity": True}, "quantity"),
  ({"quantity": 1.5}, "quantity"),
  ({"quantity": 3}, "quantity"),            # > max_contracts 2
])
def test_bad_order_fields_rejected(changes: dict, gate: str) -> None:
  """Market/stop orders, bad prices and bad quantities are rejected."""
  assert gate in failed_gates(open_ctx(intent=intent(**changes)))

def test_nan_limit_cannot_sneak_past_money_gates() -> None:
  """NaN makes every comparison False; the money gates must still fail."""
  failed = failed_gates(open_ctx(intent=intent(limit_price=math.nan)))
  assert {"max_loss_per_trade", "book_budget", "cash"} <= failed

# ------------------------------------------------------------------------- money

def test_per_trade_cap() -> None:
  """15% of capital: at $280 capital the $44.60 trade exceeds the $42 cap."""
  ctx = open_ctx(book=BookState(capital_usd=280.0, realized_pnl_usd=0.0))
  assert "max_loss_per_trade" in failed_gates(ctx)

def test_quantity_counts_toward_cap() -> None:
  """Two spreads double the max loss."""
  assert "max_loss_per_trade" in failed_gates(open_ctx(intent=intent(quantity=2)))

def test_whatif_commission_used_if_higher() -> None:
  """A larger real commission from the preview raises the max loss."""
  ctx = open_ctx(whatif=WhatIf(warning=None, commission_usd=4.00))
  assert "max_loss_per_trade" in failed_gates(ctx)

def test_book_budget_counts_open_positions_and_realized_losses() -> None:
  """Open max loss + new must fit capital minus realised losses."""
  full = BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                   open_positions=(position(max_loss_usd=290.0,
                                            expiry=EXPIRY + dt.timedelta(days=1)),))
  assert "book_budget" in failed_gates(open_ctx(book=full))
  lost = BookState(capital_usd=330.0, realized_pnl_usd=-300.0)
  assert "book_budget" in failed_gates(open_ctx(book=lost))
  won = BookState(capital_usd=330.0, realized_pnl_usd=+500.0)
  assert evaluate(open_ctx(book=won))[0].approved

def test_cash_never_goes_negative() -> None:
  """No borrowing: USD cash must cover the debit and commissions."""
  for cash in (40.0, -5.0, math.nan):
    ctx = open_ctx(account=AccountState(account_id="DU1234567", usd_cash=cash))
    assert "cash" in failed_gates(ctx)

@pytest.mark.parametrize("whatif", [
  None,
  WhatIf(warning="Order would exceed margin", commission_usd=1.30),
])
def test_whatif_must_run_cleanly(whatif: WhatIf | None) -> None:
  """No preview, or any preview warning, blocks the order."""
  assert "whatif" in failed_gates(open_ctx(whatif=whatif))

# ---------------------------------------------------------------- book limits

def test_position_and_daily_limits() -> None:
  """Max open positions and max new trades per day."""
  three = tuple(position(position_id=f"P{i}", max_loss_usd=1.0,
                         expiry=EXPIRY + dt.timedelta(days=i + 1)) for i in range(3))
  ctx = open_ctx(book=BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                                open_positions=three))
  assert "max_open_positions" in failed_gates(ctx)
  ctx = open_ctx(book=BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                                trades_opened_today=2))
  assert "max_trades_per_day" in failed_gates(ctx)

def test_duplicates() -> None:
  """Same expiry/right already held, or the identical order already working."""
  held = BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                   open_positions=(position(max_loss_usd=1.0),))
  assert "duplicate" in failed_gates(open_ctx(book=held))
  working = BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                      working_open_orders=frozenset({"SPY:2026-10-27:C:1001/1002"}))
  assert "duplicate" in failed_gates(open_ctx(book=working))

# ---------------------------------------------------------------- time and data

@pytest.mark.parametrize("expiry", [
  TODAY + dt.timedelta(days=2), TODAY + dt.timedelta(days=11), TODAY,
])
def test_dte_range(expiry: dt.date) -> None:
  """Entries only at 3-10 DTE."""
  legs = (replace(LONG_CALL, expiry=expiry), replace(SHORT_CALL, expiry=expiry))
  assert "dte" in failed_gates(open_ctx(intent=intent(legs=legs)))

@pytest.mark.parametrize(("now", "session_open", "session_close"), [
  (SESSION_OPEN + dt.timedelta(minutes=5), SESSION_OPEN, SESSION_CLOSE),
  (SESSION_CLOSE - dt.timedelta(minutes=10), SESSION_OPEN, SESSION_CLOSE),
  (SESSION_CLOSE + dt.timedelta(hours=1), SESSION_OPEN, SESSION_CLOSE),
  (NOW, None, None),
])
def test_entry_window(
  now: dt.datetime, session_open: dt.datetime | None, session_close: dt.datetime | None,
) -> None:
  """No entries in the first 15 / last 30 minutes, after hours or on holidays."""
  clock = replace(open_ctx().clock, now=now, session_open=session_open,
                  session_close=session_close)
  assert "entry_window" in failed_gates(open_ctx(clock=clock))

@pytest.mark.parametrize("leg_changes", [
  {"quote_age_seconds": 120},
  {"quote_age_seconds": None},
  {"quote_age_seconds": math.nan},
  {"bid": 0.0},
  {"bid": None},
  {"bid": math.nan},
  {"ask": math.inf},
  {"bid": 3.50, "ask": 3.95},  # spread 0.45 on mid 3.72 > 10%
  {"bid": 4.00, "ask": 3.95},  # crossed
])
def test_liquidity(leg_changes: dict) -> None:
  """Stale, missing, crossed or wide quotes block the order."""
  legs = (replace(LONG_CALL, **leg_changes), SHORT_CALL)
  assert "liquidity" in failed_gates(open_ctx(intent=intent(legs=legs)))

# ---------------------------------------------------------------- ex-dividend

def test_call_spread_blocked_before_ex_dividend_when_near_the_money() -> None:
  """Short call near the money with ex-dividend before expiry: early assignment risk."""
  spy = UnderlyingInfo(price=602.5, next_ex_dividend=TODAY + dt.timedelta(days=3),
                       ex_dividend_known=True)
  ctx = open_ctx(underlyings={"SPY": spy})
  assert "ex_dividend" in failed_gates(ctx)

def test_unknown_ex_dividend_blocks_call_spreads_only() -> None:
  """Fail closed for call spreads; put spreads and long calls don't care."""
  unknown = {"SPY": UnderlyingInfo(price=602.5, next_ex_dividend=None,
                                   ex_dividend_known=False)}
  assert "ex_dividend" in failed_gates(open_ctx(underlyings=unknown))
  puts = intent(legs=(LONG_PUT, SHORT_PUT))
  assert evaluate(open_ctx(intent=puts, underlyings=unknown))[0].approved

# ---------------------------------------------------------------- switches

def test_kill_switch_and_halt_block_entries() -> None:
  """Tripped kill switch or HALT file: no new trades."""
  for book in (
    BookState(capital_usd=330.0, realized_pnl_usd=0.0, kill_switch_tripped=True),
    BookState(capital_usd=330.0, realized_pnl_usd=0.0, halt_file_present=True),
  ):
    assert "entries_enabled" in failed_gates(open_ctx(book=book))

@pytest.mark.parametrize("live", [
  LiveFlags(),
  LiveFlags(allow_live=True),
  LiveFlags(allow_live=True, live_confirm="U9999999"),
  LiveFlags(allow_live=False, live_confirm="U1234567"),
])
def test_live_account_needs_both_flags(live: LiveFlags) -> None:
  """A non-DU account is refused unless ALLOW_LIVE and LIVE_CONFIRM both match."""
  account = AccountState(account_id="U1234567", usd_cash=330.0)
  assert "account" in failed_gates(open_ctx(account=account, live=live))

def test_live_account_with_both_flags_allowed() -> None:
  """Both flags naming this exact account lets it through."""
  account = AccountState(account_id="U1234567", usd_cash=330.0)
  live = LiveFlags(allow_live=True, live_confirm="U1234567")
  assert evaluate(open_ctx(account=account, live=live))[0].approved

def test_no_day_trades_left_blocks_entries() -> None:
  """If IBKR reports zero day trades left, keep the reserve for emergencies."""
  account = AccountState(account_id="DU1234567", usd_cash=330.0,
                         day_trades_remaining=0)
  assert "day_trades" in failed_gates(open_ctx(account=account))

# ---------------------------------------------------------------- fail closed

def test_crashing_gate_fails_closed() -> None:
  """A gate that raises counts as a failure, and the rest still run."""
  decision, approval = evaluate(open_ctx(settings=None))
  assert approval is None
  assert any(r.reason.startswith("error:") for r in decision.failures)
  assert len(decision.results) > 10

def test_unknown_purpose_never_approved() -> None:
  """No gates ran means not approved."""
  decision, approval = evaluate(open_ctx(intent=intent(purpose="adjust")))
  assert approval is None
  assert not decision.approved
