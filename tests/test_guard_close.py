"""Closing orders may only shrink what is already held."""
import datetime as dt
from dataclasses import replace

import pytest

from src.guard import evaluate
from src.guard.models import AccountState, BookState, LiveFlags
from src.storage import connect
from tests.guard_fixtures import (
  CLOSE_INTENT,
  LONG_CALL,
  SESSION_CLOSE,
  SHORT_CALL,
  SPREAD_HELD,
  STOCK,
  TODAY,
  close_ctx,
  held,
  holdings,
  position,
)

def close(**changes: object) -> object:
  """Build the valid closing intent with `changes` applied."""
  return replace(CLOSE_INTENT, **changes)

def failed_gates(ctx: object) -> set[str]:
  """Names of the gates that rejected the order."""
  decision, approval = evaluate(ctx)
  assert approval is None
  return {r.gate for r in decision.failures}

def test_valid_close_approved() -> None:
  """Selling the held spread back is allowed, DAY or GTC."""
  assert evaluate(close_ctx())[0].approved
  assert evaluate(close_ctx(intent=close(tif="GTC")))[0].approved

@pytest.mark.parametrize("changes", [
  {"quantity": 2},                                  # more than held
  {"action": "BUY"},                                # adds to the position
  {"legs": (replace(LONG_CALL, con_id=9999), SHORT_CALL)},  # not held
  {"legs": (LONG_CALL, LONG_CALL)},                 # duplicate leg
  {"legs": ()},
])
def test_close_must_only_reduce(changes: dict) -> None:
  """No opening, flipping or enlarging via a 'close'."""
  assert "reduces_only" in failed_gates(close_ctx(intent=close(**changes)))

def test_buying_back_the_short_leg_alone_is_allowed() -> None:
  """Closing the sold leg first only removes risk."""
  ctx = close_ctx(intent=close(legs=(replace(SHORT_CALL, action="BUY"),),
                               action="BUY", limit_price=3.6))
  assert evaluate(ctx)[0].approved

def test_selling_the_long_leg_alone_is_rejected() -> None:
  """Selling the bought leg would leave the sold leg naked."""
  ctx = close_ctx(intent=close(legs=(LONG_CALL,), limit_price=3.0))
  assert failed_gates(ctx) == {"no_new_uncovered"}

def test_never_beyond_zero() -> None:
  """A close larger than a leg's holding is rejected."""
  over = close_ctx(account=AccountState(
    account_id="DU1234567", usd_cash=0,
    holdings=holdings(held(LONG_CALL, 1), held(SHORT_CALL, -2)),
  ), intent=close(quantity=2))
  assert "reduces_only" in failed_gates(over)

def test_swapping_risk_between_expiries_is_rejected() -> None:
  """Buying back a naked short in one expiry can't pay for a new one in another."""
  later = dt.timedelta(days=7)
  naked = replace(SHORT_CALL, con_id=3002, expiry=SHORT_CALL.expiry + later)
  account = AccountState(account_id="DU1234567", usd_cash=0, holdings=holdings(
    held(LONG_CALL, 1), held(SHORT_CALL, -1), held(naked, -1),
  ))
  intent = close(legs=(replace(naked, action="BUY"), replace(LONG_CALL, action="SELL")),
                 action="BUY", limit_price=0.5)
  assert "no_new_uncovered" in failed_gates(close_ctx(account=account, intent=intent))

@pytest.mark.parametrize(("changes", "gate"), [
  ({"order_type": "MKT"}, "close_order"),
  ({"tif": "IOC"}, "close_order"),
  ({"limit_price": None}, "close_order"),
  ({"limit_price": 0.0}, "close_order"),
  ({"quantity": 0}, "close_order"),
  ({"close_reason": None}, "close_order"),
])
def test_close_order_fields(changes: dict, gate: str) -> None:
  """Limit orders with a reason only."""
  assert gate in failed_gates(close_ctx(intent=close(**changes)))

def test_assignment_stock_can_be_flattened() -> None:
  """Buying back stock left short by an assignment is a reduction."""
  ctx = close_ctx(
    intent=close(legs=(STOCK,), action="BUY", quantity=100, limit_price=603.0,
                 close_reason="assignment"),
    account=AccountState(account_id="DU1234567", usd_cash=60000,
                         holdings=holdings(held(STOCK, -100), held(LONG_CALL, 1))),
  )
  assert evaluate(ctx)[0].approved
  # Selling the long call first would leave the short stock uncovered.
  call_first = replace(ctx, intent=close(legs=(LONG_CALL,), limit_price=3.0,
                                         close_reason="assignment"))
  assert "no_new_uncovered" in failed_gates(call_first)
  bigger = replace(ctx, intent=replace(ctx.intent, quantity=200))
  assert "reduces_only" in failed_gates(bigger)

def test_kill_switch_and_halt_do_not_block_exits() -> None:
  """Exits keep running when entries are switched off."""
  book = BookState(capital_usd=330.0, realized_pnl_usd=0.0, kill_switch_tripped=True,
                   halt_file_present=True, open_positions=(position(),))
  assert evaluate(close_ctx(book=book))[0].approved

def test_live_account_blocks_closes_too() -> None:
  """Without both live flags the bot does nothing at all on a live account."""
  account = AccountState(account_id="U1234567", usd_cash=0, holdings=SPREAD_HELD)
  assert "account" in failed_gates(close_ctx(account=account, live=LiveFlags()))

def test_close_outside_regular_hours() -> None:
  """No closing orders after the bell."""
  clock = replace(close_ctx().clock, now=SESSION_CLOSE + dt.timedelta(minutes=1))
  assert "close_window" in failed_gates(close_ctx(clock=clock))

@pytest.mark.parametrize(("reason", "remaining", "approved"), [
  ("profit_target", 1, False),
  ("profit_target", 2, True),
  ("time_stop", 1, False),
  ("stop", 1, True),
  ("stop", 0, False),
  ("assignment", 0, True),
  ("expiry", 0, True),
  ("kill_switch", 0, True),
])
def test_same_day_close_under_day_trade_limit(
  reason: str, remaining: int, approved: bool,  # noqa: FBT001
) -> None:
  """Emergencies always close; others only with day trades to spare."""
  ctx = close_ctx(
    intent=close(close_reason=reason),
    account=AccountState(account_id="DU1234567", usd_cash=0,
                         holdings=SPREAD_HELD, day_trades_remaining=remaining),
    book=BookState(capital_usd=330.0, realized_pnl_usd=0.0,
                   open_positions=(position(opened_on=TODAY),)),
  )
  assert evaluate(ctx)[0].approved is approved

def test_decisions_are_recorded(tmp_path: object) -> None:
  """Every decision and all gate results land in SQLite."""
  conn = connect(tmp_path / "bot.sqlite")
  evaluate(close_ctx(), conn)
  evaluate(close_ctx(intent=close(order_type="MKT")), conn)
  rows = conn.execute(
    "SELECT approved, results_json FROM guard_decisions ORDER BY id",
  ).fetchall()
  assert [r["approved"] for r in rows] == [1, 0]
  assert "limit orders only" in rows[1]["results_json"]
