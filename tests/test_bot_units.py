"""Pure parts of the bot: builder, exit rules, ladder, brain parsing, snapshot maths."""
import datetime as dt
from types import SimpleNamespace

import pytest

from src.bot import brain
from src.bot.broker import LegQuote, summarize_market
from src.bot.builder import plan_entry, target_dte
from src.bot.executor import ladder
from src.bot.exits import close_plan, exit_reason, stock_close_plan, take_profit_price
from src.bot.store import PositionRow
from src.utilities.strategy_settings import StrategySettings
from tests.fake_broker import EXPIRY, NOW, TODAY, chain

S = StrategySettings()

def position(**changes: object) -> PositionRow:
  """The 602/603 call spread bought at 0.44, opened yesterday."""
  base = {
    "id": "P1", "symbol": "SPY", "expiry": EXPIRY, "right": "C",
    "structure": "debit_vertical", "quantity": 1, "long_con_id": 1602,
    "long_strike": 602.0, "short_con_id": 1603, "short_strike": 603.0,
    "entry_price": 0.44, "entry_commission": 1.30, "max_loss_usd": 46.60,
    "opened_at": NOW.isoformat(), "opened_on": TODAY - dt.timedelta(days=1),
    "tp_order_id": None, "tp_perm_id": None, "realized_pnl_usd": 0.0,
  }
  return PositionRow(**{**base, **changes})

# ------------------------------------------------------------------ builder

def test_plan_sizes_to_the_cap() -> None:
  """$330 capital: 1 lot of the $1 spread fits the $49.50 cap, 2 don't."""
  plan, why = plan_entry("bullish", "debit_vertical", chain(), TODAY, NOW, S, 330.0)
  assert why == ""
  i = plan.intent
  assert (i.quantity, i.limit_price, plan.start_price) == (1, 0.44, 0.41)
  assert plan.max_loss_usd == pytest.approx(46.60)
  assert {leg.quote_age_seconds for leg in i.legs} == {0}

def test_plan_bearish_uses_puts() -> None:
  """Bearish: put debit spread, sold leg lower."""
  plan, _ = plan_entry("bearish", "debit_vertical", chain(), TODAY, NOW, S, 330.0)
  long, short = plan.intent.legs
  assert (long.right, long.strike, short.strike) == ("P", 601.0, 600.0)

def test_plan_refuses_when_nothing_fits() -> None:
  """Too little capital: explain instead of trading."""
  plan, why = plan_entry("bullish", "debit_vertical", chain(), TODAY, NOW, S, 100.0)
  assert plan is None
  assert "per-trade cap" in why
  plan, why = plan_entry("bullish", "long_option", chain(), TODAY, NOW, S, 330.0)
  assert plan is None  # a $3 call costs ~$300

def test_target_dte_respects_range() -> None:
  """6 DTE preferred, clamped into the configured range."""
  assert target_dte(S) == 6
  s = StrategySettings.model_validate({"entries": {"min_dte": 8, "max_dte": 10}})
  assert target_dte(s) == 8

# ------------------------------------------------------------------- exits

@pytest.mark.parametrize(("changes", "value", "expected"), [
  ({}, 0.44, None),
  ({}, 0.22, "stop"),
  ({}, 0.80, "profit_target"),
  ({"expiry": TODAY + dt.timedelta(days=2)}, 0.80, "expiry"),
  ({"opened_on": TODAY - dt.timedelta(days=7)}, 0.44, "time_stop"),
  ({}, None, None),
  ({"short_con_id": None, "short_strike": None, "structure": "long_option",
    "entry_price": 2.0}, 3.0, "profit_target"),
])
def test_exit_rules(changes: dict, value: float | None, expected: str | None) -> None:
  """Expiry first, then stop, then target, then time."""
  assert exit_reason(position(**changes), value, TODAY, S) == expected

def test_take_profit_price() -> None:
  """80% of width for spreads, +50% for single options."""
  assert take_profit_price(position(), S) == 0.80
  single = position(short_con_id=None, short_strike=None, entry_price=2.0)
  assert take_profit_price(single, S) == 3.00

def test_close_plans() -> None:
  """Normal closes start at mid and step 1c; emergencies start at the bid side."""
  quotes = {1602: LegQuote(1602, 3.85, 3.95, NOW), 1603: LegQuote(1603, 3.45, 3.53, NOW)}
  normal = close_plan(position(), quotes, "stop")
  assert normal.intent.action == "SELL"
  assert (normal.start_price, normal.intent.limit_price, normal.step) == (0.41, 0.38, 0.01)
  urgent = close_plan(position(), quotes, "expiry")
  assert (urgent.start_price, urgent.intent.limit_price, urgent.step) == (0.32, 0.17, 0.05)
  assert close_plan(position(), {}, "stop") is None

def test_stock_close_plan() -> None:
  """Short stock is bought back at the ask, worst 0.5% higher."""
  plan = stock_close_plan(9, "SPY", -100, LegQuote(9, 603.0, 603.1, NOW))
  assert (plan.intent.action, plan.intent.quantity) == ("BUY", 100)
  assert (plan.start_price, plan.intent.limit_price) == (603.1, 606.12)

# ---------------------------------------------------------------- executor

def test_ladder() -> None:
  """At most 3 moves, never past the limit, both directions."""
  assert ladder(0.41, 0.44, 0.01, paying=True) == [0.41, 0.42, 0.43, 0.44]
  assert ladder(0.41, 0.48, 0.01, paying=True) == [0.41, 0.42, 0.43, 0.44]
  assert ladder(0.41, 0.42, 0.01, paying=True) == [0.41, 0.42]
  assert ladder(0.50, 0.44, 0.01, paying=True) == [0.44]
  assert ladder(0.32, 0.17, 0.05, paying=False) == [0.32, 0.27, 0.22, 0.17]

# ------------------------------------------------------------------- brain

class FakeMessages:
  """Stands in for client.beta.messages."""

  def __init__(self, response: object) -> None:
    """Return `response` from create()."""
    self.response = response
    self.kwargs: dict = {}

  async def create(self, **kwargs: object) -> object:
    """Record the request."""
    self.kwargs = kwargs
    return self.response

def client_returning(text: str | None, stop_reason: str = "end_turn") -> object:
  """A fake AsyncAnthropic whose reply is `text`."""
  content = [] if text is None else [SimpleNamespace(type="text", text=text)]
  response = SimpleNamespace(stop_reason=stop_reason, content=content,
                             model="claude-sonnet-5-5")
  return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(response)))

async def test_brain_parses_a_view() -> None:
  """A valid JSON answer becomes a View; request uses structured output."""
  client = client_returning(
    '{"underlying": "SPY", "direction": "bullish", "structure": "debit_vertical",'
    ' "confidence": 0.7, "rationale": "x"}')
  result = await brain.ask(client, "claude-sonnet-5-5", [{"symbol": "SPY"}], {})
  assert result.error is None
  assert result.tradeable(0.6, ["SPY", "QQQ"])
  kwargs = client.beta.messages.kwargs
  assert kwargs["output_config"]["format"]["type"] == "json_schema"
  assert "tools" not in kwargs

@pytest.mark.parametrize(("text", "stop_reason"), [
  ("not json", "end_turn"),
  ('{"underlying": "SPY", "direction": "bullish", "structure": "debit_vertical",'
   ' "confidence": 1.7, "rationale": "x"}', "end_turn"),
  ('{"underlying": "TSLA", "direction": "bullish", "structure": "debit_vertical",'
   ' "confidence": 0.9, "rationale": "x"}', "end_turn"),
  (None, "refusal"),
  ('{"underlying": "SPY"', "max_tokens"),
])
async def test_brain_failures_abstain(text: str | None, stop_reason: str) -> None:
  """Bad JSON, out-of-range values, refusals and truncation all abstain."""
  result = await brain.ask(client_returning(text, stop_reason), "m", [], {})
  assert result.view is None
  assert result.error
  assert not result.tradeable(0.0, ["SPY"])

# --------------------------------------------------------------- snapshot

def test_summarize_market() -> None:
  """Returns, realised vol, IV and IV rank from bar closes."""
  daily = [100 + i * 0.5 for i in range(70)]
  ivs = [0.10, 0.20, 0.15]
  out = summarize_market("SPY", 135.0, daily, ivs, [134.0, 135.0])
  # During the session the last daily bar is today's, so the previous close is [-2].
  assert out["change_today_pct"] == pytest.approx(0.75)
  assert out["implied_vol_pct"] == 15.0
  assert out["iv_rank_1y_pct"] == 50
  assert out["today_30min_closes"] == [134.0, 135.0]
  assert "realized_vol_20d_pct" in out
