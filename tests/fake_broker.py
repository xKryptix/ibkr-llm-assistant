"""An in-memory IBKR stand-in for engine and executor tests."""
import datetime as dt
from dataclasses import dataclass, field

from src.bot.broker import LegQuote, OrderHandle, OrderStatus
from src.guard import Approval
from src.guard.models import (
  AccountState,
  Holding,
  MarketClock,
  OrderIntent,
  UnderlyingInfo,
  WhatIf,
)
from src.survey.candidates import OptionQuote

NOW = dt.datetime(2026, 10, 21, 15, 0, tzinfo=dt.UTC)  # 11:00 New York
TODAY = dt.date(2026, 10, 21)
EXPIRY = dt.date(2026, 10, 27)

def chain(now: dt.datetime = NOW, expiry: dt.date = EXPIRY) -> list[OptionQuote]:
  """SPY calls and puts around 602."""
  calls = [(600, 5.00, 5.10, 0.60), (601, 4.40, 4.50, 0.55), (602, 3.85, 3.95, 0.50),
           (603, 3.45, 3.53, 0.45), (604, 2.98, 3.05, 0.40), (605, 2.55, 2.62, 0.35)]
  puts = [(599, 2.98, 3.05, -0.40), (600, 3.45, 3.53, -0.45),
          (601, 3.85, 3.95, -0.50), (602, 4.40, 4.50, -0.55)]
  out = []
  for right, rows, base in (("C", calls, 1000), ("P", puts, 2000)):
    for strike, bid, ask, delta in rows:
      out.append(OptionQuote("SPY", expiry, right, float(strike), bid, ask, delta,
                             con_id=base + strike, quoted_at=now))
  return out

@dataclass
class Placed:
  """One order the fake received."""

  intent: OrderIntent
  prices: list[float]
  perm_id: int
  status: str = "Submitted"
  filled: float = 0

@dataclass
class FakeBroker:
  """Fills each order on attempt `fill_on` (0 = first price; None = never)."""

  dry_run: bool = False
  now: dt.datetime = NOW
  cash: float = 330.0
  holdings: dict[int, Holding] = field(default_factory=dict)
  quotes: list[OptionQuote] = field(default_factory=chain)
  leg_q: dict[int, LegQuote] = field(default_factory=dict)
  fill_on: int | None = 0
  placed: list[Placed] = field(default_factory=list)
  known_orders: dict[int, OrderStatus] = field(default_factory=dict)
  session: bool = True
  next_perm: int = 9000
  # Simulate ib_async reporting Cancelled while IBKR still works the order.
  ghost_live: bool = False

  async def ensure_connected(self) -> None:
    """Nothing to connect."""

  async def account_state(self) -> AccountState:
    """Current fake account."""
    return AccountState("DU1234567", self.cash, None, dict(self.holdings))

  async def market_clock(self) -> MarketClock:
    """Regular session 13:30-20:00 UTC."""
    if not self.session:
      return MarketClock(self.now, TODAY, None, None)
    return MarketClock(self.now, TODAY,
                       dt.datetime(2026, 10, 21, 13, 30, tzinfo=dt.UTC),
                       dt.datetime(2026, 10, 21, 20, 0, tzinfo=dt.UTC))

  async def underlying_info(self, symbol: str) -> UnderlyingInfo:
    """No ex-dividend before expiry."""
    return UnderlyingInfo(602.5, dt.date(2026, 12, 18), True)

  async def option_quotes(self, symbol: str, today: dt.date, strategy: object, *,
                          right: str, target_dte: int) -> list[OptionQuote]:
    """The fixed chain."""
    return [q for q in self.quotes if q.right == right]

  async def leg_quotes(self, con_ids: list[int]) -> dict[int, LegQuote]:
    """Leg quotes from `leg_q`, falling back to the chain."""
    out = {}
    for i in con_ids:
      if i in self.leg_q:
        out[i] = self.leg_q[i]
        continue
      q = next((x for x in self.quotes if x.con_id == i), None)
      if q is not None:
        out[i] = LegQuote(i, q.bid, q.ask, self.now)
    return out

  async def audusd(self) -> float:
    """Fixed rate."""
    return 0.66

  async def market_snapshot(self, symbol: str) -> dict:
    """Minimal snapshot."""
    return {"symbol": symbol, "price": 602.5}

  async def whatif(self, intent: OrderIntent) -> WhatIf:
    """Clean preview."""
    return WhatIf(None, 1.30)

  async def place(self, approval: Approval, price: float) -> OrderHandle:
    """Record the order."""
    assert isinstance(approval, Approval)
    assert not self.dry_run
    self.next_perm += 1
    placed = Placed(approval.intent, [price], self.next_perm)
    self.placed.append(placed)
    self._maybe_fill(placed)
    return OrderHandle(len(self.placed), placed.perm_id, placed)

  async def modify(self, handle: OrderHandle, price: float) -> None:
    """New price."""
    handle.trade.prices.append(price)
    self._maybe_fill(handle.trade)

  def _maybe_fill(self, p: Placed) -> None:
    if p.intent.tif == "GTC":
      self.known_orders[p.perm_id] = OrderStatus("Submitted", 0, None)
      return
    if self.fill_on is not None and len(p.prices) - 1 >= self.fill_on:
      p.status, p.filled = "Filled", p.intent.quantity
      self._apply_fill(p.intent)

  def _apply_fill(self, intent: OrderIntent) -> None:
    sign = 1 if intent.action == "BUY" else -1
    for leg in intent.legs:
      change = sign * (1 if leg.action == "BUY" else -1) * intent.quantity * leg.ratio
      h = self.holdings.get(leg.con_id)
      qty = (h.quantity if h else 0) + change
      if qty == 0:
        self.holdings.pop(leg.con_id, None)
      else:
        self.holdings[leg.con_id] = Holding(leg.con_id, leg.symbol, leg.sec_type, qty,
                                            leg.expiry, leg.right, leg.strike)

  async def status(self, handle: OrderHandle) -> OrderStatus:
    """Status of a placed order."""
    p = handle.trade
    return OrderStatus(p.status, p.filled, p.prices[-1] if p.filled else None, 1.30)

  async def cancel(self, handle: OrderHandle) -> None:
    """Cancel unless filled."""
    if handle.trade.status != "Filled":
      handle.trade.status = "Cancelled"
      if self.ghost_live:
        handle.trade.ghost = True

  async def find_order(self, perm_id: int | None,
                       order_id: int | None = None) -> OrderStatus | None:
    """Known orders by permId (or orderId = position in `placed`)."""
    if perm_id in self.known_orders:
      return self.known_orders[perm_id]
    p = next((x for x in self.placed if x.perm_id == perm_id), None)
    if p is None and order_id and order_id <= len(self.placed):
      p = self.placed[order_id - 1]
    if p is None:
      return None
    if getattr(p, "ghost", False):
      return OrderStatus("Submitted", p.filled, None)
    return OrderStatus(p.status, p.filled, p.prices[-1])

  async def cancel_order(self, perm_id: int | None, order_id: int | None) -> None:
    """Cancel a resting order unless it filled."""
    st = self.known_orders.get(perm_id)
    if st is not None and st.status != "Filled":
      self.known_orders[perm_id] = OrderStatus("Cancelled", st.filled, st.avg_price)
