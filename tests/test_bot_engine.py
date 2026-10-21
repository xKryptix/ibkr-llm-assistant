"""Engine flows against the fake broker: entries, exits, assignment, kill switch."""
import datetime as dt
from pathlib import Path

import pytest

from src.bot import brain as brain_module
from src.bot.broker import LegQuote, OrderStatus
from src.bot.brain import BrainResult, View
from src.bot.engine import Engine
from src.bot.store import PositionRow, Store
from src.guard import KillSwitch
from src.guard.models import Holding
from src.storage import connect
from src.utilities import Settings
from src.utilities.strategy_settings import StrategySettingsStore
from tests.fake_broker import EXPIRY, NOW, TODAY, FakeBroker

BULL_SPREAD = View(underlying="SPY", direction="bullish", structure="debit_vertical",
                   confidence=0.7, rationale="test")

def make_engine(tmp_path: Path, broker: FakeBroker) -> Engine:
  """Engine on a fresh database with instant executor timing."""
  conn = connect(tmp_path / "bot.sqlite")
  engine = Engine(
    broker=broker, store=Store(conn), kill_switch=KillSwitch(conn),
    settings_store=StrategySettingsStore(tmp_path / "settings.toml"),
    settings=Settings(data_dir=str(tmp_path)), claude=object(), data_dir=tmp_path,
  )
  engine.reprice_interval_s = 0
  engine.poll_s = 0
  engine.now = lambda: broker.now
  return engine

@pytest.fixture
def claude_says(monkeypatch: pytest.MonkeyPatch) -> list:
  """Make brain.ask return the given view; records each call."""
  calls: list = []
  def set_view(view: View | None, error: str | None = None) -> None:
    async def fake_ask(*args: object) -> BrainResult:
      calls.append(args)
      return BrainResult(view, error, "claude-sonnet-5-5")
    monkeypatch.setattr(brain_module, "ask", fake_ask)
  calls.append(set_view)
  return calls

def held_spread(broker: FakeBroker, long_id: int = 1602, short_id: int = 1603) -> None:
  """Put the 602/603 call spread in the fake account."""
  broker.holdings[long_id] = Holding(long_id, "SPY", "OPT", 1, EXPIRY, "C", 602.0)
  broker.holdings[short_id] = Holding(short_id, "SPY", "OPT", -1, EXPIRY, "C", 603.0)

def add_spread_position(engine: Engine, *, opened_on: dt.date = TODAY - dt.timedelta(days=1),
                        expiry: dt.date = EXPIRY, entry: float = 0.44,
                        pid: str = "P1") -> PositionRow:
  """Record the call spread as a bot position."""
  p = PositionRow(
    id=pid, symbol="SPY", expiry=expiry, right="C", structure="debit_vertical",
    quantity=1, long_con_id=1602, long_strike=602.0, short_con_id=1603,
    short_strike=603.0, entry_price=entry, entry_commission=1.30,
    max_loss_usd=46.60, opened_at=NOW.isoformat(), opened_on=opened_on,
    tp_order_id=None, tp_perm_id=None, realized_pnl_usd=0.0,
  )
  engine.store.add_position(p)
  return p

def quote(broker: FakeBroker, con_id: int, bid: float, ask: float) -> None:
  """Override one leg's quote."""
  broker.leg_q[con_id] = LegQuote(con_id, bid, ask, NOW)

# ------------------------------------------------------------------- entries

async def test_bullish_view_opens_a_call_debit_spread(tmp_path: Path,
                                                      claude_says: list) -> None:
  """Claude's view -> builder -> guard -> executor -> position."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()

  [order] = broker.placed
  assert order.intent.action == "BUY"
  assert [leg.con_id for leg in order.intent.legs] == [1602, 1603]
  assert order.prices[0] == pytest.approx(0.41)          # starts at mid
  assert order.intent.limit_price == pytest.approx(0.44)  # approved ceiling
  [p] = engine.store.open_positions()
  assert (p.symbol, p.right, p.quantity, p.long_strike, p.short_strike) == (
    "SPY", "C", 1, 602.0, 603.0)
  assert engine.store.get_state("capital_usd") == "330.0"
  row = engine.store.conn.execute("SELECT outcome FROM decisions").fetchone()
  assert row["outcome"] == "filled"

@pytest.mark.parametrize("view", [
  View(underlying="none", direction="abstain", structure="long_option",
       confidence=0.0, rationale="nothing"),
  BULL_SPREAD.model_copy(update={"confidence": 0.55}),
  BULL_SPREAD.model_copy(update={"underlying": "none"}),
])
async def test_no_trade_without_a_confident_view(tmp_path: Path, claude_says: list,
                                                 view: View) -> None:
  """Abstain, low confidence or no underlying: nothing is sent."""
  claude_says[0](view)
  broker = FakeBroker()
  await make_engine(tmp_path, broker).entry_cycle()
  assert broker.placed == []

async def test_claude_error_means_no_trade(tmp_path: Path, claude_says: list) -> None:
  """An API error is an abstain."""
  claude_says[0](None, "api error")
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  assert broker.placed == []
  row = engine.store.conn.execute("SELECT outcome, error FROM decisions").fetchone()
  assert (row["outcome"], row["error"]) == ("error", "api error")

async def test_claude_not_asked_outside_entry_window(tmp_path: Path,
                                                     claude_says: list) -> None:
  """First 15 minutes: don't even spend a Claude call."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(now=dt.datetime(2026, 10, 21, 13, 40, tzinfo=dt.UTC))
  await make_engine(tmp_path, broker).entry_cycle()
  assert len(claude_says) == 1  # only the setter, no calls
  assert broker.placed == []

async def test_halt_file_stops_entries(tmp_path: Path, claude_says: list) -> None:
  """data/HALT: no Claude call, no order."""
  claude_says[0](BULL_SPREAD)
  (tmp_path / "HALT").touch()
  broker = FakeBroker()
  await make_engine(tmp_path, broker).entry_cycle()
  assert broker.placed == []

async def test_dry_run_sends_nothing(tmp_path: Path, claude_says: list) -> None:
  """Dry run journals the approved order but never places it."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(dry_run=True)
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  assert broker.placed == []
  assert engine.store.open_positions() == []
  row = engine.store.conn.execute("SELECT status FROM orders").fetchone()
  assert row["status"] == "dry_run"

async def test_unfilled_entry_is_cancelled_after_ladder(tmp_path: Path,
                                                        claude_says: list) -> None:
  """Never fills: 4 prices (start + 3 ticks), then cancel; no position."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(fill_on=None)
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  [order] = broker.placed
  assert order.prices == pytest.approx([0.41, 0.42, 0.43, 0.44])
  assert order.status == "Cancelled"
  assert engine.store.open_positions() == []

async def test_guard_rejection_is_recorded(tmp_path: Path, claude_says: list) -> None:
  """Not enough USD cash: the guard blocks it and the reason is stored."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(cash=10.0)
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  assert broker.placed == []
  row = engine.store.conn.execute("SELECT outcome FROM decisions").fetchone()
  assert row["outcome"].startswith("rejected: cash")

# --------------------------------------------------------------------- exits

async def test_stop_loss_closes_position(tmp_path: Path) -> None:
  """Worth <= 50% of cost: sell the spread back."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  add_spread_position(engine)
  quote(broker, 1602, 3.00, 3.10)
  quote(broker, 1603, 2.80, 2.85)
  await engine.exit_cycle()
  [order] = broker.placed
  assert order.intent.action == "SELL"
  assert order.intent.close_reason == "stop"
  assert engine.store.open_positions() == []
  assert engine.store.realized_total() < 0
  assert engine.store.consecutive_losses() == 1

async def test_closes_two_days_before_expiry(tmp_path: Path) -> None:
  """DTE <= 2 always closes, whatever the price."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  add_spread_position(engine, expiry=TODAY + dt.timedelta(days=2))
  await engine.exit_cycle()
  [order] = broker.placed
  assert order.intent.close_reason == "expiry"

async def test_take_profit_rests_from_next_day_only(tmp_path: Path) -> None:
  """Opened yesterday: a GTC take-profit at 80% of width. Opened today: none."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  add_spread_position(engine)
  await engine.exit_cycle()
  [tp] = broker.placed
  assert (tp.intent.tif, tp.intent.action, tp.prices[0]) == ("GTC", "SELL", 0.80)
  [p] = engine.store.open_positions()
  assert p.tp_perm_id == tp.perm_id

  broker2 = FakeBroker()
  engine2 = make_engine(tmp_path / "b", broker2)
  held_spread(broker2)
  add_spread_position(engine2, opened_on=TODAY)
  await engine2.exit_cycle()
  assert broker2.placed == []

async def test_take_profit_cancelled_before_stop(tmp_path: Path) -> None:
  """A resting take-profit is cancelled (and confirmed) before any other close."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  add_spread_position(engine)
  engine.store.set_take_profit("P1", 7, 555)
  broker.known_orders[555] = OrderStatus("Submitted", 0, None)
  quote(broker, 1602, 3.00, 3.10)
  quote(broker, 1603, 2.80, 2.85)
  await engine.exit_cycle()
  assert broker.known_orders[555].status == "Cancelled"
  [order] = broker.placed
  assert order.intent.close_reason == "stop"

async def test_take_profit_filled_while_away_is_booked(tmp_path: Path) -> None:
  """Legs gone and the take-profit filled: book the profit."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  add_spread_position(engine)
  engine.store.set_take_profit("P1", 7, 555)
  broker.known_orders[555] = OrderStatus("Filled", 1, 0.80)
  await engine.exit_cycle()
  assert len(engine.store.open_positions()) == 1  # missing once: check again
  await engine.exit_cycle()
  assert engine.store.open_positions() == []
  assert engine.store.realized_total() == pytest.approx(36 - 1.30 - 1.30)

async def test_vanished_position_books_worst_case(tmp_path: Path) -> None:
  """Legs gone with no known exit: assume the full loss."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  add_spread_position(engine)
  await engine.exit_cycle()
  await engine.exit_cycle()
  assert engine.store.realized_total() == pytest.approx(-44 - 1.30)

async def test_position_reappearing_is_not_booked(tmp_path: Path) -> None:
  """A one-off empty positions report (e.g. after a reconnect) books nothing."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  add_spread_position(engine, opened_on=TODAY)
  await engine.exit_cycle()
  held_spread(broker)
  await engine.exit_cycle()
  await engine.exit_cycle()
  assert len(engine.store.open_positions()) == 1
  assert engine.store.realized_total() == 0

async def test_assignment_closes_stock_before_long_leg(tmp_path: Path) -> None:
  """Short call assigned: buy back the stock first, then sell the long call."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  broker.holdings[1602] = Holding(1602, "SPY", "OPT", 1, EXPIRY, "C", 602.0)
  broker.holdings[756733] = Holding(756733, "SPY", "STK", -100)
  quote(broker, 756733, 603.0, 603.1)
  add_spread_position(engine)
  await engine.exit_cycle()
  stock, call = broker.placed
  assert (stock.intent.legs[0].sec_type, stock.intent.action) == ("STK", "BUY")
  assert stock.intent.quantity == 100
  assert [leg.con_id for leg in call.intent.legs] == [1602]
  assert (call.intent.action, call.intent.close_reason) == ("SELL", "assignment")
  assert broker.holdings == {}

async def test_kill_switch_trips_and_closes_everything(tmp_path: Path,
                                                       claude_says: list) -> None:
  """Equity under the 30% drawdown floor: trip, close all, no new entries."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  engine.store.set_state("capital_aud", "500")
  engine.store.set_state("capital_usd", "330.0")
  held_spread(broker)
  add_spread_position(engine)
  add_spread_position(engine, pid="OLD", opened_on=TODAY - dt.timedelta(days=9))
  engine.store.reduce_position("OLD", 1, 0.0, -120.0, "stop")
  quote(broker, 1602, 3.00, 3.10)
  quote(broker, 1603, 2.95, 3.00)
  await engine.exit_cycle()
  assert engine.kill.tripped
  [order] = broker.placed
  assert order.intent.close_reason == "kill_switch"
  await engine.entry_cycle()
  assert len(broker.placed) == 1

async def test_missed_fill_is_adopted(tmp_path: Path, claude_says: list) -> None:
  """An opening order that filled while the bot was down becomes a position."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(fill_on=None)
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  [order] = broker.placed
  engine.store.update_order(1, status="unknown")
  order.status, order.filled = "Filled", 1
  held_spread(broker)
  await engine.exit_cycle()
  [p] = engine.store.open_positions()
  assert (p.long_con_id, p.short_con_id, p.quantity) == (1602, 1603, 1)

# ------------------------------------------------------- review regressions

async def test_quote_age_measured_after_quotes_arrive(tmp_path: Path,
                                                      claude_says: list) -> None:
  """Real quotes are stamped after the cycle started; they must not look stale."""
  claude_says[0](BULL_SPREAD)
  later = NOW + dt.timedelta(seconds=5)
  broker = FakeBroker()
  broker.quotes = [__import__("dataclasses").replace(q, quoted_at=later)
                   for q in broker.quotes]
  engine = make_engine(tmp_path, broker)
  engine.now = lambda: later + dt.timedelta(seconds=3)
  await engine.entry_cycle()
  assert len(broker.placed) == 1

async def test_no_resting_take_profit_for_single_options(tmp_path: Path) -> None:
  """A stray take-profit on a single option could sell it naked, so none is placed."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  broker.holdings[1604] = Holding(1604, "SPY", "OPT", 1, EXPIRY, "C", 604.0)
  engine.store.add_position(PositionRow(
    id="L1", symbol="SPY", expiry=EXPIRY, right="C", structure="long_option",
    quantity=1, long_con_id=1604, long_strike=604.0, short_con_id=None,
    short_strike=None, entry_price=3.0, entry_commission=1.0, max_loss_usd=302.0,
    opened_at=NOW.isoformat(), opened_on=TODAY - dt.timedelta(days=1),
    tp_order_id=None, tp_perm_id=None, realized_pnl_usd=0.0,
  ))
  await engine.exit_cycle()
  assert broker.placed == []

@pytest.mark.parametrize(("expiry", "closes"), [
  (EXPIRY, False),                              # stop: wait for confirmation
  (TODAY + dt.timedelta(days=2), True),         # expiry: close anyway
])
async def test_unconfirmed_take_profit_cancel(tmp_path: Path, expiry: dt.date,
                                              closes: bool) -> None:  # noqa: FBT001
  """If IBKR can't confirm the take-profit is gone, only emergencies proceed."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  add_spread_position(engine, expiry=expiry)
  engine.store.set_take_profit("P1", 77, 777)  # unknown to the broker
  quote(broker, 1602, 3.00, 3.10)
  quote(broker, 1603, 2.80, 2.85)
  await engine.exit_cycle()
  assert bool(broker.placed) is closes

async def test_partly_filled_take_profit_is_booked_then_rest_closed(tmp_path: Path) -> None:
  """1 of 2 filled at the take-profit: book it, then close the other 1."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  broker.holdings[1602] = Holding(1602, "SPY", "OPT", 2, EXPIRY, "C", 602.0)
  broker.holdings[1603] = Holding(1603, "SPY", "OPT", -2, EXPIRY, "C", 603.0)
  add_spread_position(engine)
  with engine.store.conn:
    engine.store.conn.execute("UPDATE positions SET quantity = 2")
  engine.store.set_take_profit("P1", 7, 555)
  broker.known_orders[555] = OrderStatus("Submitted", 1, 0.80)
  quote(broker, 1602, 3.00, 3.10)
  quote(broker, 1603, 2.80, 2.85)
  await engine.exit_cycle()
  [order] = broker.placed
  assert order.intent.quantity == 1
  row = engine.store.conn.execute("SELECT status, realized_pnl_usd FROM positions").fetchone()
  assert row["status"] == "closed"

async def test_recorded_size_synced_to_ibkr(tmp_path: Path) -> None:
  """Book says 2, IBKR holds 1: book the missing one, close what's held."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  held_spread(broker)
  add_spread_position(engine, expiry=TODAY + dt.timedelta(days=2))
  with engine.store.conn:
    engine.store.conn.execute("UPDATE positions SET quantity = 2")
  await engine.exit_cycle()
  [order] = broker.placed
  assert order.intent.quantity == 1
  assert engine.store.realized_total() < -40

async def test_lone_short_leg_is_bought_back(tmp_path: Path) -> None:
  """Bought leg gone, sold leg still held: buy it back at once."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  broker.holdings[1603] = Holding(1603, "SPY", "OPT", -1, EXPIRY, "C", 603.0)
  add_spread_position(engine)
  await engine.exit_cycle()
  [order] = broker.placed
  assert [leg.con_id for leg in order.intent.legs] == [1603]
  assert order.intent.action == "BUY"
  assert 1603 not in broker.holdings

async def test_assignment_stock_pnl_is_booked(tmp_path: Path) -> None:
  """The stock buy-back's P&L lands on the position."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  broker.holdings[1602] = Holding(1602, "SPY", "OPT", 1, EXPIRY, "C", 602.0)
  broker.holdings[756733] = Holding(756733, "SPY", "STK", -100, avg_cost=603.0)
  quote(broker, 756733, 604.0, 604.1)
  add_spread_position(engine)
  await engine.exit_cycle()
  row = engine.store.conn.execute(
    "SELECT position_id FROM orders WHERE intent_json LIKE '%STK%'").fetchone()
  assert row["position_id"] == "P1"
  # Stock: bought back 1.10 above the 603 assignment price = -110 - 1.30 fee.
  # Long call sold at 3.85 against 0.44 paid: +341 - 1.30 - 1.30 fees.
  assert engine.store.realized_total() == pytest.approx(-111.30 + 338.40)
  assert not engine.kill.tripped  # the assigned spread is valued as long call only

async def test_ghost_cancel_is_not_trusted(tmp_path: Path, claude_says: list) -> None:
  """ib_async says Cancelled but IBKR still works it: unknown, still blocking."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(fill_on=None, ghost_live=True)
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  row = engine.store.conn.execute("SELECT status FROM orders").fetchone()
  assert row["status"] == "unknown"
  assert engine.store.unresolved_open_fingerprints()

async def test_unconfirmed_order_keeps_blocking(tmp_path: Path, claude_says: list) -> None:
  """An order IBKR can't find yet stays unresolved (not 'lost') for a day."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker(fill_on=None)
  engine = make_engine(tmp_path, broker)
  await engine.entry_cycle()
  engine.store.update_order(1, status="unknown", perm_id=None, order_id=None)
  broker.placed.clear()
  await engine.exit_cycle()
  row = engine.store.conn.execute("SELECT status FROM orders").fetchone()
  assert row["status"] == "unknown"

async def test_unbooked_options_pause_entries(tmp_path: Path, claude_says: list) -> None:
  """Options in the account that the bot doesn't know about: no new entries."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker()
  held_spread(broker)
  engine = make_engine(tmp_path, broker)
  await engine.exit_cycle()
  await engine.entry_cycle()
  assert broker.placed == []
