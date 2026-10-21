"""Risk gates (PLAN.md §5.3). Each gate is a pure function of the context.

Every gate runs, every result is recorded, and any failure blocks the order.
Thresholds that are deliberately NOT user settings live here as constants.
"""
import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass

from src.guard.exposure import after_trade, increased, uncovered_exposure
from src.guard.models import (
  EMERGENCY_CLOSE_REASONS,
  AccountState,
  BookState,
  GateResult,
  LiveFlags,
  MarketClock,
  OrderIntent,
  UnderlyingInfo,
  WhatIf,
)
from src.guard.structure import (
  Classified,
  classify_open,
  effective_legs,
  fingerprint,
  max_loss_usd,
  natural_debit,
  round_trip_commission,
)
from src.utilities.strategy_settings import StrategySettings

MAX_QUOTE_AGE_SECONDS = 30
MIN_SPREAD_ALLOWANCE = 0.05
PRICE_TOLERANCE = 0.01
EX_DIVIDEND_ITM_BUFFER = 0.005

@dataclass(frozen=True)
class GuardContext:
  """Everything a gate may look at."""

  intent: OrderIntent
  account: AccountState
  book: BookState
  clock: MarketClock
  settings: StrategySettings
  live: LiveFlags
  underlyings: dict[str, UnderlyingInfo]
  whatif: WhatIf | None = None

Gate = Callable[[GuardContext], GateResult]

def _ok(name: str, reason: str = "") -> GateResult:
  return GateResult(name, passed=True, reason=reason)

def _fail(name: str, reason: str) -> GateResult:
  return GateResult(name, passed=False, reason=reason)

def _finite(*values: float | None) -> bool:
  return all(
    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    for v in values
  )

def _classified(ctx: GuardContext) -> Classified | None:
  return classify_open(ctx.intent)[0]

def _commission(ctx: GuardContext, c: Classified) -> float:
  costs = ctx.settings.costs
  estimate = round_trip_commission(
    2 if c.short else 1, ctx.intent.quantity,
    costs.commission_per_contract, costs.commission_min_per_order,
  )
  if ctx.whatif and ctx.whatif.commission_usd is not None:
    estimate = max(estimate, round(2 * ctx.whatif.commission_usd, 2))
  return estimate

def _order_max_loss(ctx: GuardContext, c: Classified) -> float:
  return max_loss_usd(c, ctx.intent.limit_price, ctx.intent.quantity,
                      _commission(ctx, c))

def _needs_structure(name: str, ctx: GuardContext) -> GateResult | None:
  if _classified(ctx) is None:
    return _fail(name, "not checked: structure not allowed")
  if not _finite(ctx.intent.limit_price):
    return _fail(name, "not checked: no valid limit price")
  return None

# ---------------------------------------------------------------- both purposes

def gate_account(ctx: GuardContext) -> GateResult:
  """Paper accounts only, unless both live flags name this account."""
  name = "account"
  account_id = ctx.account.account_id
  if account_id.startswith("DU"):
    return _ok(name, f"paper account {account_id}")
  if ctx.live.allow_live and ctx.live.live_confirm == account_id:
    return _ok(name, f"LIVE account {account_id} explicitly allowed")
  return _fail(name, f"{account_id} is not a paper account and live is not allowed")

# ---------------------------------------------------------------- opening gates

def gate_entries_enabled(ctx: GuardContext) -> GateResult:
  """No new entries while the kill switch is tripped or a HALT file exists."""
  name = "entries_enabled"
  if ctx.book.kill_switch_tripped:
    return _fail(name, "kill switch is tripped; reset it by hand")
  if ctx.book.halt_file_present:
    return _fail(name, "HALT file present")
  return _ok(name)

def gate_structure(ctx: GuardContext) -> GateResult:
  """Long option or debit vertical only."""
  c, reason = classify_open(ctx.intent)
  if c is None:
    return _fail("structure", reason)
  return _ok("structure", c.structure)

def gate_underlying(ctx: GuardContext) -> GateResult:
  """Only the configured underlyings (SPY/QQQ)."""
  name = "underlying"
  allowed = set(ctx.settings.entries.underlyings)
  symbols = {leg.symbol for leg in ctx.intent.legs}
  if not symbols or not symbols <= allowed:
    return _fail(name, f"{sorted(symbols)} not in {sorted(allowed)}")
  return _ok(name)

def gate_order_type(ctx: GuardContext) -> GateResult:
  """Require opening orders to be BUY, limit and DAY."""
  name = "order_type"
  i = ctx.intent
  if i.action != "BUY":
    return _fail(name, "opening orders must be BUY (a debit)")
  if i.order_type != "LMT":
    return _fail(name, f"{i.order_type} not allowed; limit orders only")
  if i.tif != "DAY":
    return _fail(name, f"tif {i.tif}; opening orders are DAY only")
  return _ok(name)

def gate_quantity(ctx: GuardContext) -> GateResult:
  """Whole number of contracts, at most max_contracts."""
  name = "quantity"
  q = ctx.intent.quantity
  if not isinstance(q, int) or isinstance(q, bool) or q < 1:
    return _fail(name, f"quantity {q!r} must be a positive integer")
  if q > ctx.settings.risk.max_contracts:
    return _fail(name, f"{q} > max_contracts {ctx.settings.risk.max_contracts}")
  return _ok(name)

def gate_debit_price(ctx: GuardContext) -> GateResult:
  """0 < limit < width, and never above the natural price."""
  name = "debit_price"
  if (skip := _needs_structure(name, ctx)) is not None:
    return skip
  c, limit = _classified(ctx), ctx.intent.limit_price
  if limit <= 0:
    return _fail(name, f"limit {limit} is not a debit")
  if c.width is not None and limit >= c.width:
    return _fail(name, f"limit {limit} >= width {c.width}: no possible profit")
  natural = natural_debit(c)
  if natural is None or not _finite(natural):
    return _fail(name, "no quote to compare the limit with")
  if limit > natural + PRICE_TOLERANCE:
    return _fail(name, f"limit {limit} above natural price {natural:.2f}")
  return _ok(name, f"limit {limit}, natural {natural:.2f}")

def gate_dte(ctx: GuardContext) -> GateResult:
  """Days to expiry within the configured range (min is at least 3)."""
  name = "dte"
  c = _classified(ctx)
  if c is None:
    return _fail(name, "not checked: structure not allowed")
  dte = (c.long.expiry - ctx.clock.today).days
  e = ctx.settings.entries
  if not e.min_dte <= dte <= e.max_dte:
    return _fail(name, f"{dte} DTE outside {e.min_dte}-{e.max_dte}")
  return _ok(name, f"{dte} DTE")

def gate_liquidity(ctx: GuardContext) -> GateResult:
  """Fresh two-sided quotes with tight spreads on every leg."""
  name = "liquidity"
  pct = ctx.settings.entries.max_leg_spread_pct
  for leg in ctx.intent.legs:
    if not _finite(leg.bid, leg.ask) or leg.bid <= 0 or leg.ask < leg.bid:
      return _fail(name, f"leg {leg.con_id} has no usable bid/ask")
    if (
      not _finite(leg.quote_age_seconds)
      or not 0 <= leg.quote_age_seconds <= MAX_QUOTE_AGE_SECONDS
    ):
      return _fail(name, f"leg {leg.con_id} quote is stale")
    mid = (leg.bid + leg.ask) / 2
    spread = leg.ask - leg.bid
    if spread > max(MIN_SPREAD_ALLOWANCE, mid * pct / 100) + 1e-9:
      return _fail(
        name, f"leg {leg.con_id} spread {spread:.2f} too wide (mid {mid:.2f})",
      )
  return _ok(name)

def gate_max_loss_per_trade(ctx: GuardContext) -> GateResult:
  """Max loss (including commissions) within risk_per_trade_pct of capital."""
  name = "max_loss_per_trade"
  if (skip := _needs_structure(name, ctx)) is not None:
    return skip
  loss = _order_max_loss(ctx, _classified(ctx))
  cap = ctx.book.capital_usd * ctx.settings.risk.risk_per_trade_pct / 100
  if not _finite(loss, cap) or loss > cap:
    return _fail(name, f"max loss ${loss:.2f} > cap ${cap:.2f}")
  return _ok(name, f"max loss ${loss:.2f} <= cap ${cap:.2f}")

def gate_book_budget(ctx: GuardContext) -> GateResult:
  """Open max loss plus this order within capital minus realised losses."""
  name = "book_budget"
  if (skip := _needs_structure(name, ctx)) is not None:
    return skip
  used = sum(p.max_loss_usd for p in ctx.book.open_positions)
  budget = ctx.book.capital_usd + min(ctx.book.realized_pnl_usd, 0)
  new = _order_max_loss(ctx, _classified(ctx))
  if not _finite(used, budget, new) or used + new > budget:
    return _fail(name, f"${used:.2f} open + ${new:.2f} new > budget ${budget:.2f}")
  return _ok(name, f"${used + new:.2f} of ${budget:.2f}")

def gate_max_open_positions(ctx: GuardContext) -> GateResult:
  """At most max_open_positions at once."""
  name = "max_open_positions"
  n, limit = len(ctx.book.open_positions), ctx.settings.risk.max_open_positions
  if n >= limit:
    return _fail(name, f"{n} open, limit {limit}")
  return _ok(name)

def gate_max_trades_per_day(ctx: GuardContext) -> GateResult:
  """At most max_trades_per_day new trades today."""
  name = "max_trades_per_day"
  n, limit = ctx.book.trades_opened_today, ctx.settings.risk.max_trades_per_day
  if n >= limit:
    return _fail(name, f"{n} opened today, limit {limit}")
  return _ok(name)

def gate_cash(ctx: GuardContext) -> GateResult:
  """USD cash stays >= 0 after paying the debit and commissions (no borrowing)."""
  name = "cash"
  if (skip := _needs_structure(name, ctx)) is not None:
    return skip
  cost = _order_max_loss(ctx, _classified(ctx))
  if not _finite(ctx.account.usd_cash, cost):
    return _fail(name, "cash balance or cost unknown")
  after = ctx.account.usd_cash - cost
  if after < 0:
    return _fail(name, f"USD cash ${ctx.account.usd_cash:.2f} - ${cost:.2f} < 0")
  return _ok(name, f"USD cash after ${after:.2f}")

def gate_whatif(ctx: GuardContext) -> GateResult:
  """IBKR's order preview ran and returned no warning."""
  name = "whatif"
  if ctx.whatif is None:
    return _fail(name, "no whatIf preview")
  if ctx.whatif.warning:
    return _fail(name, f"IBKR warning: {ctx.whatif.warning}")
  return _ok(name)

def gate_duplicate(ctx: GuardContext) -> GateResult:
  """No identical working order and no same-direction position on that expiry."""
  name = "duplicate"
  c = _classified(ctx)
  if c is None:
    return _fail(name, "not checked: structure not allowed")
  if fingerprint(c) in ctx.book.working_open_orders:
    return _fail(name, "identical opening order already working")
  for p in ctx.book.open_positions:
    if (p.symbol, p.expiry, p.right) == (c.long.symbol, c.long.expiry, c.long.right):
      return _fail(name, f"already hold {p.position_id} on that expiry and right")
  return _ok(name)

def gate_entry_window(ctx: GuardContext) -> GateResult:
  """Regular hours, skipping the first and last minutes."""
  name = "entry_window"
  clk, e = ctx.clock, ctx.settings.entries
  if clk.session_open is None or clk.session_close is None:
    return _fail(name, "market closed today")
  start = clk.session_open + dt.timedelta(minutes=e.skip_first_minutes)
  end = clk.session_close - dt.timedelta(minutes=e.skip_last_minutes)
  if not start <= clk.now <= end:
    return _fail(name, f"{clk.now:%H:%M}Z outside entry window")
  return _ok(name)

def gate_ex_dividend(ctx: GuardContext) -> GateResult:
  """No call spread whose sold leg is near/in the money before an ex-dividend date."""
  name = "ex_dividend"
  c = _classified(ctx)
  if c is None:
    return _fail(name, "not checked: structure not allowed")
  if c.short is None or c.short.right != "C":
    return _ok(name, "no sold call")
  info = ctx.underlyings.get(c.short.symbol)
  if info is None or not info.ex_dividend_known:
    return _fail(name, "ex-dividend date unknown; refusing call spread")
  exdiv = info.next_ex_dividend
  if exdiv is None or not ctx.clock.today <= exdiv <= c.short.expiry:
    return _ok(name, "no ex-dividend date before expiry")
  if c.short.strike <= info.price * (1 + EX_DIVIDEND_ITM_BUFFER):
    return _fail(name, f"sold call {c.short.strike} near the money before "
                       f"ex-dividend {exdiv}")
  return _ok(name)

def gate_day_trades_open(ctx: GuardContext) -> GateResult:
  """If IBKR reports a day-trade limit, keep at least one in reserve."""
  name = "day_trades"
  remaining = ctx.account.day_trades_remaining
  if remaining is not None and remaining < 1:
    return _fail(name, "no day trades left in reserve for an emergency close")
  return _ok(name)

# ---------------------------------------------------------------- closing gates

def gate_close_order(ctx: GuardContext) -> GateResult:
  """Require closing orders to be limit, DAY or GTC, priced, with a reason."""
  name = "close_order"
  i = ctx.intent
  if i.order_type != "LMT":
    return _fail(name, f"{i.order_type} not allowed; limit orders only")
  if i.tif not in ("DAY", "GTC"):
    return _fail(name, f"tif {i.tif} not allowed")
  if not _finite(i.limit_price) or i.limit_price <= 0:
    return _fail(name, f"limit {i.limit_price} must be positive")
  if not isinstance(i.quantity, int) or isinstance(i.quantity, bool) or i.quantity < 1:
    return _fail(name, f"quantity {i.quantity!r} must be a positive integer")
  if i.close_reason is None:
    return _fail(name, "closing order without a reason")
  return _ok(name, i.close_reason)

def gate_close_reduces_only(ctx: GuardContext) -> GateResult:
  """Every leg must shrink something already held, never open or flip."""
  name = "reduces_only"
  legs = effective_legs(ctx.intent)
  if not legs:
    return _fail(name, "no legs")
  if len({leg.con_id for leg in legs}) != len(legs):
    return _fail(name, "duplicate legs")
  for leg in legs:
    holding = ctx.account.holdings.get(leg.con_id)
    held = 0 if holding is None else holding.quantity
    if not _finite(held):
      return _fail(name, f"leg {leg.con_id}: holding unknown")
    change = (1 if leg.action == "BUY" else -1) * leg.ratio * ctx.intent.quantity
    if held == 0 or (held > 0) == (change > 0) or abs(change) > abs(held):
      return _fail(name, f"leg {leg.con_id}: {leg.action} {abs(change)} "
                         f"does not reduce holding {held}")
  return _ok(name)

def gate_close_no_new_uncovered(ctx: GuardContext) -> GateResult:
  """Reject a close that leaves any short option or stock less covered."""
  name = "no_new_uncovered"
  holdings = ctx.account.holdings
  quantity = ctx.intent.quantity
  if not isinstance(quantity, int) or isinstance(quantity, bool):
    return _fail(name, "not checked: bad quantity")
  before = uncovered_exposure(list(holdings.values()))
  after = uncovered_exposure(
    after_trade(holdings, effective_legs(ctx.intent), quantity),
  )
  if grew := increased(before, after):
    return _fail(name, "would leave uncovered risk: " + "; ".join(grew))
  return _ok(name)

def gate_close_window(ctx: GuardContext) -> GateResult:
  """Allow closing orders only during regular hours."""
  name = "close_window"
  clk = ctx.clock
  if clk.session_open is None or clk.session_close is None:
    return _fail(name, "market closed today")
  if not clk.session_open <= clk.now <= clk.session_close:
    return _fail(name, f"{clk.now:%H:%M}Z outside regular hours")
  return _ok(name)

def gate_day_trades_close(ctx: GuardContext) -> GateResult:
  """Same-day closes under a day-trade limit: emergencies always, others sparingly."""
  name = "day_trades"
  remaining = ctx.account.day_trades_remaining
  reason = ctx.intent.close_reason
  if remaining is None or reason in EMERGENCY_CLOSE_REASONS:
    return _ok(name)
  legs = effective_legs(ctx.intent)
  same_day = any(
    p.opened_on == ctx.clock.today
    and any((leg.symbol, leg.expiry, leg.right) == (p.symbol, p.expiry, p.right)
            for leg in legs)
    for p in ctx.book.open_positions
  )
  if not same_day:
    return _ok(name)
  needed = 1 if reason == "stop" else 2
  if remaining < needed:
    return _fail(name, f"same-day {reason} close needs {needed} day trades, "
                       f"{remaining} left")
  return _ok(name)

OPEN_GATES: tuple[Gate, ...] = (
  gate_account, gate_entries_enabled, gate_structure, gate_underlying,
  gate_order_type, gate_quantity, gate_debit_price, gate_dte, gate_liquidity,
  gate_max_loss_per_trade, gate_book_budget, gate_max_open_positions,
  gate_max_trades_per_day, gate_cash, gate_whatif, gate_duplicate,
  gate_entry_window, gate_ex_dividend, gate_day_trades_open,
)

CLOSE_GATES: tuple[Gate, ...] = (
  gate_account, gate_close_order, gate_close_reduces_only,
  gate_close_no_new_uncovered, gate_close_window, gate_day_trades_close,
)
