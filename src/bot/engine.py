"""The trading loop: reconcile, exits (code only), kill switch, then entries.

Every order, opening or closing, goes through the guard; the executor only
accepts an Approval.
"""
import asyncio
import datetime as dt
import json
from dataclasses import dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from loguru import logger

from src.bot import brain
from src.bot.broker import Broker, LegQuote
from src.bot.builder import TradePlan, plan_entry, target_dte
from src.bot.executor import ExecResult, execute, place_resting
from src.bot.exits import (
  close_plan,
  exit_reason,
  marks,
  stock_close_plan,
  take_profit_price,
)
from src.bot.store import PositionRow, Store
from src.guard import GuardContext, KillSwitch, evaluate, halt_file_present, kill_reason
from src.guard.models import (
  AccountState,
  BookState,
  CloseReason,
  LiveFlags,
  MarketClock,
  OrderIntent,
)
from src.guard.structure import classify_open, fingerprint, round_trip_commission
from src.utilities import Settings
from src.utilities.strategy_settings import StrategySettings, StrategySettingsStore

NEW_YORK = ZoneInfo("America/New_York")
TICK_SECONDS = 30
CLOSED_POLL = dt.timedelta(minutes=5)
TP_CANCEL_WAIT_S = 15

@dataclass
class Clocks:
  """When each cycle last ran."""

  last_entry: dt.datetime | None = None
  last_exit: dt.datetime | None = None

class Engine:
  """Runs the bot against a broker."""

  def __init__(  # noqa: PLR0913
    self,
    broker: Broker,
    store: Store,
    kill_switch: KillSwitch,
    settings_store: StrategySettingsStore,
    settings: Settings,
    claude: anthropic.AsyncAnthropic | None,
    data_dir: Path,
  ) -> None:
    """Wire the parts together."""
    self.broker = broker
    self.store = store
    self.kill = kill_switch
    self.settings_store = settings_store
    self.settings = settings
    self.claude = claude
    self.data_dir = data_dir
    self.clocks = Clocks()
    # Executor timing (seconds); tests shrink these.
    self.reprice_interval_s = 20.0
    self.poll_s = 1.0
    # Positions IBKR didn't report on the last check (must be missing twice).
    self._missing: set[str] = set()

  @property
  def strategy(self) -> StrategySettings:
    """Current user settings."""
    return self.settings_store.current

  @property
  def live(self) -> LiveFlags:
    """Live-trading flags from the environment."""
    return LiveFlags(self.settings.allow_live, self.settings.live_confirm)

  # ---------------------------------------------------------------- capital

  async def capital_usd(self) -> float | None:
    """Capital in USD, fixed when first seen (or when capital_aud is edited)."""
    aud = self.strategy.capital.capital_aud
    stored_aud = self.store.get_state("capital_aud")
    stored_usd = self.store.get_state("capital_usd")
    if stored_usd is not None and stored_aud is not None and float(stored_aud) == aud:
      return float(stored_usd)
    rate = await self.broker.audusd()
    if rate is None or not 0.3 < rate < 1.5:
      logger.warning("No sensible AUD.USD rate ({}); not trading yet", rate)
      return None
    usd = round(aud * rate, 2)
    self.store.set_state("capital_aud", str(aud))
    self.store.set_state("capital_usd", str(usd))
    self.store.set_state("capital_audusd", str(rate))
    logger.info("Capital fixed at US${} (A${} at AUD.USD {})", usd, aud, rate)
    return usd

  def book(self, capital_usd: float, today: dt.date) -> BookState:
    """Build the guard's view of the bot's book."""
    return BookState(
      capital_usd=capital_usd,
      realized_pnl_usd=self.store.realized_total(),
      open_positions=tuple(p.as_guard_position() for p in self.store.open_positions()),
      working_open_orders=self.store.unresolved_open_fingerprints(),
      trades_opened_today=self.store.trades_opened_on(today),
      kill_switch_tripped=self.kill.tripped,
      halt_file_present=halt_file_present(self.data_dir),
    )

  def _one_way_fee(self, legs: int, qty: int) -> float:
    c = self.strategy.costs
    return round_trip_commission(legs, qty, c.commission_per_contract,
                                 c.commission_min_per_order) / 2

  # ------------------------------------------------------------- reconcile

  async def reconcile(self, account: AccountState) -> None:
    """Adopt missed fills and close the books on positions IBKR no longer holds.

    A position can vanish when its take-profit filled while the bot was down, or
    when it was closed by hand.
    """
    for row in self.store.unresolved_orders():
      await self._resolve_order(row)
    missing = set()
    for p in self.store.open_positions():
      held_long = _held(account, p.long_con_id)
      held_short = _held(account, p.short_con_id) if p.short_con_id else 0
      if held_long == 0 and held_short == 0:
        if p.id in self._missing:
          await self._finalize_external(p)
        else:
          missing.add(p.id)
          logger.warning("Position {} not reported by IBKR; checking again", p.id)
    self._missing = missing
    bot_legs = {c for p in self.store.open_positions() for c in p.con_ids}
    for h in account.holdings.values():
      if h.sec_type == "OPT" and h.quantity and h.con_id not in bot_legs:
        logger.error("Option {} {} {} x{} held but not in the bot's book",
                     h.symbol, h.expiry, h.strike, h.quantity)

  async def _resolve_order(self, row: object) -> None:
    status = None if row["perm_id"] is None else await self.broker.find_order(
      row["perm_id"],
    )
    if status is None:
      # Never reached IBKR (or IBKR forgot it): nothing was filled by it.
      self.store.update_order(row["id"], status="lost")
      logger.warning("Order {} never confirmed by IBKR; marked lost", row["id"])
      return
    if not status.terminal:
      return  # still working; check again next cycle
    self.store.update_order(
      row["id"], status="filled" if status.filled else "cancelled",
      filled_qty=status.filled, avg_fill_price=status.avg_price,
    )
    if row["purpose"] == "open" and status.filled > 0:
      self._adopt_open(row, status.filled, status.avg_price)

  def _adopt_open(self, row: object, filled: float, avg: float | None) -> None:
    intent = json.loads(row["intent_json"])
    legs = intent["legs"]
    price = avg or intent["limit_price"]  # limit = the worst price it could fill at
    long, short = legs[0], (legs[1] if len(legs) > 1 else None)
    qty = int(filled)
    pid = f"P{row['id']}"
    fee = self._one_way_fee(len(legs), qty)
    self.store.add_position(PositionRow(
      id=pid, symbol=long["symbol"], expiry=dt.date.fromisoformat(long["expiry"]),
      right=long["right"],
      structure="long_option" if short is None else "debit_vertical",
      quantity=qty, long_con_id=long["con_id"], long_strike=long["strike"],
      short_con_id=None if short is None else short["con_id"],
      short_strike=None if short is None else short["strike"],
      entry_price=price, entry_commission=fee,
      max_loss_usd=round(price * 100 * qty + 2 * fee, 2),
      opened_at=row["created_at"],
      opened_on=dt.datetime.fromisoformat(row["created_at"]).astimezone(
        NEW_YORK).date(),
      tp_order_id=None, tp_perm_id=None, realized_pnl_usd=0.0,
    ))
    self.store.update_order(row["id"], position_id=pid)
    logger.warning("Adopted position {} from an order filled while unattended", pid)

  async def _finalize_external(self, p: PositionRow) -> None:
    status = None if p.tp_perm_id is None else await self.broker.find_order(
      p.tp_perm_id,
    )
    if status is not None and status.status == "Filled":
      exit_price, reason = status.avg_price, "profit_target"
    else:
      # Unknown exit: book the worst case so budget and kill switch stay safe.
      exit_price, reason = 0.0, "external_unknown"
      logger.warning("Position {} vanished from IBKR; booking worst case", p.id)
    legs = 2 if p.short_con_id else 1
    pnl = (exit_price - p.entry_price) * 100 * p.quantity - p.entry_commission - (
      self._one_way_fee(legs, p.quantity) if exit_price else 0
    )
    self.store.reduce_position(p.id, p.quantity, exit_price, round(pnl, 2), reason)

  # ------------------------------------------------------------------ exits

  async def exit_cycle(self) -> None:  # noqa: C901 (one pass, in PLAN.md order)
    """Handle assignment, the kill switch, exit rules and resting take-profits."""
    account = await self.broker.account_state()
    clock = await self.broker.market_clock()
    await self.reconcile(account)
    account = await self.broker.account_state()
    positions = self.store.open_positions()
    stocks = [h for h in account.holdings.values()
              if h.sec_type == "STK" and h.quantity != 0]
    con_ids = sorted({c for p in positions for c in p.con_ids}
                     | {h.con_id for h in stocks})
    quotes = await self.broker.leg_quotes(con_ids) if con_ids else {}

    for h in stocks:  # assignment: flatten the stock first
      logger.warning("Stock {} x{} in account (assignment?)", h.symbol, h.quantity)
      plan = stock_close_plan(h.con_id, h.symbol, h.quantity, quotes.get(h.con_id))
      if plan is None:
        logger.error("Cannot price stock {} to close it", h.symbol)
        continue
      await self._send_close(plan.intent, plan.start_price, plan.step, None,
                             account, clock)
    if stocks:
      account = await self.broker.account_state()

    capital = await self.capital_usd()
    if capital is not None and not self.kill.tripped:
      equity = capital + self.store.realized_total() + sum(
        _unrealized(p, quotes) for p in positions
      )
      if reason := kill_reason(equity, capital, self.store.consecutive_losses(),
                               self.strategy):
        self.kill.trip(reason)

    for p in self.store.open_positions():
      if p.id in self._missing:
        continue  # not reported by IBKR right now; reconcile decides next time
      _, worst = marks(p, quotes)
      short_gone = p.short_con_id and _held(account, p.short_con_id) == 0
      reason: CloseReason | None
      if self.kill.tripped:
        reason = "kill_switch"
      elif short_gone:
        reason = "assignment"
      else:
        reason = exit_reason(p, worst, clock.today, self.strategy)
      if reason is not None:
        await self._close_position(p, reason, quotes, account, clock)
      elif p.tp_perm_id is None and p.opened_on < clock.today:
        await self._place_take_profit(p, quotes, account, clock)

  async def _close_position(
    self, p: PositionRow, reason: CloseReason, quotes: dict[int, LegQuote],
    account: AccountState, clock: MarketClock,
  ) -> None:
    if p.tp_perm_id is not None and not await self._cancel_take_profit(p):
      return
    if p.short_con_id and _held(account, p.short_con_id) == 0:
      # Short leg already gone (assigned): sell only the long leg.
      p = replace(p, short_con_id=None, short_strike=None)
    plan = close_plan(p, quotes, reason)
    if plan is None:
      logger.error("Cannot price {} to close it ({})", p.id, reason)
      return
    result = await self._send_close(plan.intent, plan.start_price, plan.step, p.id,
                                    account, clock)
    if result is not None and result.filled > 0:
      legs = len(plan.intent.legs)
      qty = int(result.filled)
      exit_fee = result.commission or self._one_way_fee(legs, qty)
      share = qty / p.quantity
      pnl = ((result.avg_price or 0) - p.entry_price) * 100 * qty - (
        p.entry_commission * share) - exit_fee
      self.store.reduce_position(p.id, qty, result.avg_price, round(pnl, 2), reason)
      logger.info("Closed {} x{} ({}): P&L ${:.2f}", p.id, qty, reason, pnl)

  async def _send_close(  # noqa: PLR0913
    self, intent: OrderIntent, start: float, step: float, position_id: str | None,
    account: AccountState, clock: MarketClock,
  ) -> ExecResult | None:
    capital = await self.capital_usd() or 0.0
    ctx = GuardContext(intent=intent, account=account,
                       book=self.book(capital, clock.today), clock=clock,
                       settings=self.strategy, live=self.live, underlyings={})
    decision, approval = evaluate(ctx, self.store.conn)
    if approval is None:
      logger.error("Guard blocked closing order: {}", decision.failures)
      return None
    return await execute(self.broker, self.store, approval, start_price=start,
                         step=step, position_id=position_id,
                         interval_s=self.reprice_interval_s, poll_s=self.poll_s)

  async def _cancel_take_profit(self, p: PositionRow) -> bool:
    """Cancel the resting take-profit; False if it filled (position is done)."""
    await self.broker.cancel_perm(p.tp_perm_id)
    status = None
    for _ in range(TP_CANCEL_WAIT_S):
      status = await self.broker.find_order(p.tp_perm_id)
      if status is None or status.terminal:
        break
      await asyncio.sleep(self.poll_s)
    if status is not None and status.status == "Filled":
      await self._finalize_external(p)
      return False
    if status is not None and not status.terminal:
      logger.error("Take-profit for {} won't cancel; not closing yet", p.id)
      return False
    self.store.set_take_profit(p.id, None, None)
    return True

  async def _place_take_profit(
    self, p: PositionRow, quotes: dict[int, LegQuote], account: AccountState,
    clock: MarketClock,
  ) -> None:
    price = take_profit_price(p, self.strategy)
    plan = close_plan(p, quotes, "profit_target", tif="GTC", price=price)
    capital = await self.capital_usd() or 0.0
    ctx = GuardContext(intent=plan.intent, account=account,
                       book=self.book(capital, clock.today), clock=clock,
                       settings=self.strategy, live=self.live, underlyings={})
    decision, approval = evaluate(ctx, self.store.conn)
    if approval is None:
      logger.warning("Guard blocked take-profit for {}: {}", p.id, decision.failures)
      return
    handle = await place_resting(self.broker, self.store, approval, position_id=p.id)
    if handle is not None:
      self.store.set_take_profit(p.id, handle.order_id, handle.perm_id)
      logger.info("Resting take-profit for {} at {}", p.id, price)

  # ---------------------------------------------------------------- entries

  def _entry_precheck(  # noqa: PLR0911 (flat list of reasons)
    self, clock: MarketClock, capital: float | None,
  ) -> str | None:
    """Cheap reasons not to ask Claude at all."""
    if capital is None:
      return "capital unknown"
    if self.kill.tripped:
      return "kill switch tripped"
    if halt_file_present(self.data_dir):
      return "HALT file present"
    if clock.session_open is None or clock.session_close is None:
      return "market closed"
    e = self.strategy.entries
    if not (clock.session_open + dt.timedelta(minutes=e.skip_first_minutes)
            <= clock.now
            <= clock.session_close - dt.timedelta(minutes=e.skip_last_minutes)):
      return "outside entry window"
    r = self.strategy.risk
    if len(self.store.open_positions()) >= r.max_open_positions:
      return "max open positions"
    if self.store.trades_opened_on(clock.today) >= r.max_trades_per_day:
      return "max trades today"
    if self.claude is None:
      return "no ANTHROPIC_API_KEY"
    return None

  def _book_for_claude(self, clock: MarketClock) -> dict:
    return {
      "open_positions": [
        {"symbol": p.symbol, "right": p.right, "structure": p.structure,
         "expiry": p.expiry.isoformat(),
         "days_to_expiry": (p.expiry - clock.today).days,
         "entry_price": p.entry_price, "quantity": p.quantity}
        for p in self.store.open_positions()
      ],
      "trades_opened_today": self.store.trades_opened_on(clock.today),
      "now_new_york": clock.now.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M"),
    }

  async def entry_cycle(self) -> None:
    """Ask Claude for a view and, if it holds up, open one trade."""
    clock = await self.broker.market_clock()
    capital = await self.capital_usd()
    if why := self._entry_precheck(clock, capital):
      logger.debug("No entry check: {}", why)
      return
    symbols = list(self.strategy.entries.underlyings)
    snapshots = [await self.broker.market_snapshot(s) for s in symbols]
    model = self.strategy.brain.model
    result = await brain.ask(self.claude, model, snapshots,
                             self._book_for_claude(clock))
    v = result.view
    decision_id = self.store.record_decision(
      model=result.model, error=result.error,
      underlying=v and v.underlying, direction=v and v.direction,
      structure=v and v.structure, confidence=v and v.confidence,
      rationale=v and v.rationale,
    )
    if not result.tradeable(self.strategy.brain.min_confidence, symbols):
      outcome = "error" if result.error else (
        "abstain" if v.direction == "abstain" else "low_confidence")
      self.store.set_decision_outcome(decision_id, outcome)
      logger.info("Claude: {} ({})", outcome, result.error or v.rationale)
      return
    outcome = await self._try_open(v, clock, capital)
    self.store.set_decision_outcome(decision_id, outcome)
    logger.info("Entry outcome: {}", outcome)

  async def _try_open(self, v: brain.View, clock: MarketClock, capital: float) -> str:
    right = "C" if v.direction == "bullish" else "P"
    quotes = await self.broker.option_quotes(
      v.underlying, clock.today, self.strategy, right=right,
      target_dte=target_dte(self.strategy),
    )
    plan, why = plan_entry(v.direction, v.structure, quotes, clock.today,
                           clock.now, self.strategy, capital)
    if plan is None:
      return f"no_fit: {why}"
    account = await self.broker.account_state()
    info = await self.broker.underlying_info(v.underlying)
    whatif = await self.broker.whatif(plan.intent)
    ctx = GuardContext(intent=plan.intent, account=account,
                       book=self.book(capital, clock.today), clock=clock,
                       settings=self.strategy, live=self.live,
                       underlyings={v.underlying: info}, whatif=whatif)
    decision, approval = evaluate(ctx, self.store.conn)
    if approval is None:
      return "rejected: " + "; ".join(
        f"{r.gate}: {r.reason}" for r in decision.failures
      )
    classified, _ = classify_open(plan.intent)
    result = await execute(self.broker, self.store, approval,
                           start_price=plan.start_price, step=0.01,
                           fingerprint=fingerprint(classified),
                           interval_s=self.reprice_interval_s, poll_s=self.poll_s)
    if result.filled > 0:
      self._record_fill(plan, result, clock)
    return result.status

  def _record_fill(
    self, plan: TradePlan, result: ExecResult, clock: MarketClock,
  ) -> None:
    c = plan.candidate
    qty = int(result.filled)
    legs = 1 if c.short is None else 2
    fee = result.commission or self._one_way_fee(legs, qty)
    price = result.avg_price or plan.intent.limit_price
    pid = f"P{result.order_row}"
    self.store.add_position(PositionRow(
      id=pid, symbol=c.symbol, expiry=c.expiry, right=c.right, structure=c.structure,
      quantity=qty, long_con_id=c.long.con_id, long_strike=c.long.strike,
      short_con_id=None if c.short is None else c.short.con_id,
      short_strike=None if c.short is None else c.short.strike,
      entry_price=price, entry_commission=fee,
      max_loss_usd=round(price * 100 * qty + fee + self._one_way_fee(legs, qty), 2),
      opened_at=clock.now.isoformat(timespec="seconds"), opened_on=clock.today,
      tp_order_id=None, tp_perm_id=None, realized_pnl_usd=0.0,
    ))
    self.store.update_order(result.order_row, position_id=pid)
    logger.info("Opened {}: {} {} x{} at {}", pid, c.symbol, c.structure, qty, price)

  # -------------------------------------------------------------------- loop

  async def tick(self) -> dt.timedelta:
    """One pass; returns how long to wait before the next."""
    changes, error = self.settings_store.reload()
    if error:
      logger.warning("Settings edit rejected: {}", error)
    elif changes:
      logger.info("Settings changed: {}", changes)
    await self.broker.ensure_connected()
    clock = await self.broker.market_clock()
    if clock.session_open is None or not (
      clock.session_open <= clock.now <= clock.session_close
    ):
      return CLOSED_POLL
    s = self.strategy
    if _due(self.clocks.last_exit, clock.now, s.exits.exit_interval_minutes):
      self.clocks.last_exit = clock.now
      await self.exit_cycle()
    if _due(self.clocks.last_entry, clock.now, s.entries.entry_interval_minutes):
      self.clocks.last_entry = clock.now
      await self.entry_cycle()
    return dt.timedelta(seconds=TICK_SECONDS)

  async def run(self) -> None:
    """Run until stopped. Errors in a pass are logged and the loop continues."""
    mode = "DRY RUN (no orders sent)" if self.broker.dry_run else "PAPER/LIVE ORDERS"
    logger.info("Bot starting: {}", mode)
    while True:
      try:
        wait = await self.tick()
      except Exception:
        logger.exception("Pass failed; retrying shortly")
        wait = dt.timedelta(seconds=60)
      await asyncio.sleep(wait.total_seconds())

def _due(last: dt.datetime | None, now: dt.datetime, minutes: int) -> bool:
  return last is None or now - last >= dt.timedelta(minutes=minutes)

def _held(account: AccountState, con_id: int | None) -> float:
  if con_id is None:
    return 0
  h = account.holdings.get(con_id)
  return 0 if h is None else h.quantity

def _unrealized(p: PositionRow, quotes: dict[int, LegQuote]) -> float:
  _, worst = marks(p, quotes)
  if worst is None:
    return 0.0  # unquoted: assume unchanged rather than trip on missing data
  return (worst - p.entry_price) * 100 * p.quantity - p.entry_commission
