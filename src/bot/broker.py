"""The broker interface the bot uses, and its IBKR implementation.

Everything the engine, executor and exit manager need from IBKR goes through
`Broker`, so the logic can be tested against a fake. Orders can only be placed
with an `Approval` from the guard; with `dry_run` nothing is sent at all.
"""
import asyncio
import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Protocol

from ib_async import Contract, ComboLeg, LimitOrder, Stock, Trade, util
from loguru import logger

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
from src.survey.price_survey import SurveyClient
from src.utilities.strategy_settings import StrategySettings

NEW_YORK = util.ZoneInfo("America/New_York")
UNSET_DOUBLE_LIMIT = 1e300
TERMINAL = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})

@dataclass(frozen=True)
class LegQuote:
  """Latest quote for one held contract."""

  con_id: int
  bid: float
  ask: float
  quoted_at: dt.datetime

  @property
  def mid(self) -> float:
    """Midpoint."""
    return (self.bid + self.ask) / 2

@dataclass
class OrderHandle:
  """A placed order."""

  order_id: int
  perm_id: int | None
  trade: object | None = field(default=None, repr=False)

@dataclass(frozen=True)
class OrderStatus:
  """Where an order stands."""

  status: str
  filled: float
  avg_price: float | None
  commission: float | None = None

  @property
  def terminal(self) -> bool:
    """No further fills can happen."""
    return self.status in TERMINAL

class Broker(Protocol):
  """What the bot needs from the broker."""

  dry_run: bool

  async def ensure_connected(self) -> None:
    """Connect or reconnect."""

  async def account_state(self) -> AccountState:
    """Account ID, USD cash, day trades left and holdings."""

  async def market_clock(self) -> MarketClock:
    """Today's regular session from IBKR liquidHours."""

  async def underlying_info(self, symbol: str) -> UnderlyingInfo:
    """Price and next ex-dividend date."""

  async def option_quotes(
    self, symbol: str, today: dt.date, strategy: StrategySettings, *,
    right: str, target_dte: int,
  ) -> list[OptionQuote]:
    """Near-the-money quotes for one right at the expiry nearest target_dte."""

  async def leg_quotes(self, con_ids: list[int]) -> dict[int, LegQuote]:
    """Quotes for specific contracts."""

  async def audusd(self) -> float | None:
    """AUD.USD midpoint."""

  async def market_snapshot(self, symbol: str) -> dict:
    """Recent price action and implied volatility for Claude."""

  async def whatif(self, intent: OrderIntent) -> WhatIf:
    """IBKR order preview."""

  async def place(self, approval: Approval, price: float) -> OrderHandle:
    """Send an approved order at `price`."""

  async def modify(self, handle: OrderHandle, price: float) -> None:
    """Change a working order's limit price."""

  async def status(self, handle: OrderHandle) -> OrderStatus:
    """Return the current status of a placed order."""

  async def cancel(self, handle: OrderHandle) -> None:
    """Request cancellation."""

  async def find_order(self, perm_id: int) -> OrderStatus | None:
    """Status of an order by permanent ID (open or completed), if IBKR knows it."""

  async def cancel_perm(self, perm_id: int) -> None:
    """Cancel an open order by permanent ID."""

def _num(x: float | None) -> float:
  return math.nan if x is None or x == -1 else float(x)

def _expiry(contract: Contract) -> dt.date | None:
  raw = contract.lastTradeDateOrContractMonth
  if not raw:
    return None
  return dt.datetime.strptime(raw[:8], "%Y%m%d").date()  # noqa: DTZ007

class IBBroker(SurveyClient):
  """IBKR implementation of `Broker` on ib_async."""

  def __init__(self, *, dry_run: bool) -> None:
    """Open a trading connection.

    IBKR's order preview (whatIf) needs a non-read-only session even when
    dry-running; `place` is what refuses in dry-run mode.
    """
    super().__init__(readonly=False)
    self.dry_run = dry_run
    self._contracts: dict[int, Contract] = {}
    self._clock_cache: (
      tuple[dt.date, dt.datetime | None, dt.datetime | None] | None
    ) = None

  async def ensure_connected(self) -> None:
    """Connect, or reconnect after a Gateway restart; use live market data."""
    was_connected = self.ib.isConnected()
    await self._connect()
    if not was_connected:
      self.ib.reqMarketDataType(1)

  # ------------------------------------------------------------- account

  def _account_id(self) -> str:
    accounts = self.ib.managedAccounts()
    if not accounts:
      msg = "IBKR returned no managed accounts"
      raise RuntimeError(msg)
    return accounts[0]

  async def account_state(self) -> AccountState:
    """Account ID, USD cash, day trades left and holdings."""
    account = self._account_id()
    values = self.ib.accountValues(account)
    usd_cash = math.nan
    day_trades = None
    for v in values:
      if v.tag == "CashBalance" and v.currency == "USD":
        usd_cash = float(v.value)
      elif v.tag == "DayTradesRemaining":
        n = int(float(v.value))
        day_trades = None if n < 0 else n
    holdings = {}
    # Ask IBKR afresh rather than trusting the cached list after a reconnect.
    positions = [p for p in await self.ib.reqPositionsAsync() if p.account == account]
    for p in positions:
      c = p.contract
      holdings[c.conId] = Holding(
        con_id=c.conId, symbol=c.symbol, sec_type=c.secType,
        quantity=float(p.position), expiry=_expiry(c),
        right=c.right or None, strike=c.strike or None,
      )
    if math.isnan(usd_cash):
      # A brand-new account may not report a USD line until it holds USD.
      usd_cash = 0.0
    return AccountState(account_id=account, usd_cash=usd_cash,
                        day_trades_remaining=day_trades, holdings=holdings)

  # --------------------------------------------------------------- clock

  async def market_clock(self) -> MarketClock:
    """Today's regular session from IBKR liquidHours (cached per day)."""
    now = dt.datetime.now(dt.UTC)
    today = now.astimezone(NEW_YORK).date()
    if self._clock_cache is None or self._clock_cache[0] != today:
      [details] = await self.ib.reqContractDetailsAsync(Stock("SPY", "SMART", "USD"))
      session = next(
        (s for s in details.liquidSessions() if s.start.date() == today), None,
      )
      self._clock_cache = (
        today,
        None if session is None else session.start.astimezone(dt.UTC),
        None if session is None else session.end.astimezone(dt.UTC),
      )
    _, start, end = self._clock_cache
    return MarketClock(now=now, today=today, session_open=start, session_close=end)

  # ---------------------------------------------------------- market data

  async def underlying_info(self, symbol: str) -> UnderlyingInfo:
    """Price, and next ex-dividend date from IBKR's dividend tick (456)."""
    stock, price = await self.spot(symbol)
    ticker = self.ib.reqMktData(stock, "456", snapshot=False, regulatorySnapshot=False)
    try:
      for _ in range(30):
        if ticker.dividends is not None:
          break
        await asyncio.sleep(0.1)
    finally:
      self.ib.cancelMktData(stock)
    dividends = ticker.dividends
    return UnderlyingInfo(
      price=price,
      next_ex_dividend=None if dividends is None else dividends.nextDate,
      ex_dividend_known=dividends is not None,
    )

  async def option_quotes(
    self, symbol: str, today: dt.date, strategy: StrategySettings, *,
    right: str, target_dte: int,
  ) -> list[OptionQuote]:
    """Near-the-money quotes for one right at the expiry nearest target_dte."""
    stock, spot = await self.spot(symbol)
    if math.isnan(spot):
      return []
    return await SurveyClient.option_quotes(
      self, stock, spot, today, strategy, rights=(right,), target_dte=target_dte,
    )

  async def _contract(self, con_id: int) -> Contract:
    if con_id not in self._contracts:
      c = Contract(conId=con_id)
      [qualified] = await self.ib.qualifyContractsAsync(c)
      if qualified is None:
        msg = f"cannot qualify conId {con_id}"
        raise RuntimeError(msg)
      self._contracts[con_id] = qualified
    return self._contracts[con_id]

  async def leg_quotes(self, con_ids: list[int]) -> dict[int, LegQuote]:
    """Snapshot quotes for specific contracts."""
    contracts = [await self._contract(i) for i in con_ids]
    tickers = await self.ib.reqTickersAsync(*contracts)
    now = dt.datetime.now(dt.UTC)
    return {
      t.contract.conId: LegQuote(t.contract.conId, _num(t.bid), _num(t.ask),
                                 t.time or now)
      for t in tickers
    }

  async def audusd(self) -> float | None:
    """AUD.USD midpoint."""
    return await SurveyClient.audusd(self)

  async def market_snapshot(self, symbol: str) -> dict:
    """Recent price action and implied volatility, summarised for Claude."""
    stock, price = await self.spot(symbol)
    daily = await self.ib.reqHistoricalDataAsync(
      stock, "", "90 D", "1 day", "TRADES", useRTH=True,
    )
    iv = await self.ib.reqHistoricalDataAsync(
      stock, "", "1 Y", "1 day", "OPTION_IMPLIED_VOLATILITY", useRTH=True,
    )
    intraday = await self.ib.reqHistoricalDataAsync(
      stock, "", "1 D", "30 mins", "TRADES", useRTH=True,
    )
    return summarize_market(
      symbol, price,
      [b.close for b in daily], [b.close for b in iv], [b.close for b in intraday],
    )

  # ------------------------------------------------------------- orders

  async def _order_parts(
    self, intent: OrderIntent, price: float,
  ) -> tuple[Contract, LimitOrder]:
    if len(intent.legs) == 1:
      [leg] = intent.legs
      contract = await self._contract(leg.con_id)
      action = intent.action if leg.action == "BUY" else (
        "SELL" if intent.action == "BUY" else "BUY"
      )
    else:
      first = await self._contract(intent.legs[0].con_id)
      contract = Contract(
        symbol=first.symbol, secType="BAG", currency="USD", exchange="SMART",
        comboLegs=[
          ComboLeg(conId=leg.con_id, ratio=leg.ratio, action=leg.action,
                   exchange="SMART")
          for leg in intent.legs
        ],
      )
      action = intent.action
    order = LimitOrder(action, intent.quantity, round(price, 2), tif=intent.tif,
                       outsideRth=False)
    return contract, order

  async def whatif(self, intent: OrderIntent) -> WhatIf:
    """IBKR order preview at the intent's limit price."""
    contract, order = await self._order_parts(intent, intent.limit_price)
    state = await self.ib.whatIfOrderAsync(contract, order)
    commission = state.commission
    if commission is None or not math.isfinite(commission) or (
      commission > UNSET_DOUBLE_LIMIT
    ):
      commission = None
    return WhatIf(warning=state.warningText or None, commission_usd=commission)

  async def place(self, approval: Approval, price: float) -> OrderHandle:
    """Send an approved order. Refuses without an Approval or when dry-running."""
    if not isinstance(approval, Approval):
      msg = "orders need an Approval from the guard"
      raise PermissionError(msg)
    if self.dry_run:
      msg = "dry run: orders are never sent"
      raise PermissionError(msg)
    contract, order = await self._order_parts(approval.intent, price)
    trade = self.ib.placeOrder(contract, order)
    for _ in range(50):  # wait briefly for IBKR to assign a permId
      if trade.order.permId:
        break
      await asyncio.sleep(0.1)
    return OrderHandle(trade.order.orderId, trade.order.permId or None, trade)

  async def modify(self, handle: OrderHandle, price: float) -> None:
    """Change the limit price of a working order."""
    trade: Trade = handle.trade
    trade.order.lmtPrice = round(price, 2)
    self.ib.placeOrder(trade.contract, trade.order)

  async def status(self, handle: OrderHandle) -> OrderStatus:
    """Return the current status, fills and commissions."""
    trade: Trade = handle.trade
    s = trade.orderStatus
    if handle.perm_id is None and trade.order.permId:
      handle.perm_id = trade.order.permId
    commission = sum(
      f.commissionReport.commission for f in trade.fills
      if f.commissionReport and f.commissionReport.commission < UNSET_DOUBLE_LIMIT
    ) or None
    return OrderStatus(s.status, float(s.filled), s.avgFillPrice or None, commission)

  async def cancel(self, handle: OrderHandle) -> None:
    """Request cancellation."""
    self.ib.cancelOrder(handle.trade.order)

  async def find_order(self, perm_id: int) -> OrderStatus | None:
    """Look up an order by permanent ID among open, session and completed orders."""
    for t in self.ib.trades():
      if t.order.permId == perm_id:
        return OrderStatus(t.orderStatus.status, float(t.orderStatus.filled),
                           t.orderStatus.avgFillPrice or t.order.lmtPrice)
    for t in await self.ib.reqCompletedOrdersAsync(apiOnly=True):
      if t.order.permId == perm_id:
        filled = t.order.filledQuantity
        filled = float(filled) if filled and filled < UNSET_DOUBLE_LIMIT else 0.0
        return OrderStatus(t.orderStatus.status, filled, t.order.lmtPrice)
    return None

  async def cancel_perm(self, perm_id: int) -> None:
    """Cancel an open order by permanent ID."""
    for t in self.ib.openTrades():
      if t.order.permId == perm_id:
        self.ib.cancelOrder(t.order)
        return
    logger.warning("No open order with permId {} to cancel", perm_id)

def summarize_market(
  symbol: str,
  price: float,
  daily_closes: list[float],
  iv_closes: list[float],
  intraday_closes: list[float],
) -> dict:
  """Compact numbers for Claude (pure, so it can be tested)."""
  def pct(a: float, b: float) -> float | None:
    if not b or math.isnan(a) or math.isnan(b):
      return None
    return round((a / b - 1) * 100, 2)

  out: dict = {"symbol": symbol, "price": round(price, 2)}
  if len(daily_closes) >= 2:
    prev = daily_closes[-2] if len(daily_closes) >= 2 else math.nan
    out["change_today_pct"] = pct(price, prev)
    for days in (5, 20, 60):
      if len(daily_closes) > days:
        out[f"return_{days}d_pct"] = pct(price, daily_closes[-days - 1])
    if len(daily_closes) > 21:
      rets = [math.log(b / a) for a, b in zip(daily_closes[-21:-1], daily_closes[-20:],
                                               strict=True) if a > 0 and b > 0]
      if len(rets) > 1:
        out["realized_vol_20d_pct"] = round(
          statistics.stdev(rets) * math.sqrt(252) * 100, 1,
        )
    window = daily_closes[-20:]
    out["range_20d"] = [round(min(window), 2), round(max(window), 2)]
  ivs = [x for x in iv_closes if x and not math.isnan(x)]
  if ivs:
    current = ivs[-1]
    lo, hi = min(ivs), max(ivs)
    out["implied_vol_pct"] = round(current * 100, 1)
    out["iv_rank_1y_pct"] = (
      None if hi == lo else round((current - lo) / (hi - lo) * 100)
    )
  if intraday_closes:
    out["today_30min_closes"] = [round(x, 2) for x in intraday_closes[-13:]]
  return out
