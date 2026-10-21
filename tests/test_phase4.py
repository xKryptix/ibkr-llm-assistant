"""Phase 4: notifications, journal of cash flows, daily summary and tax export."""
import datetime as dt
from pathlib import Path

import pytest

from src.bot.notify import Notifier
from src.bot.store import Store
from src.bot.tax import build_tax_rows, financial_year, parse_rba_f11, rate_on
from src.storage import connect
from tests.fake_broker import NOW, TODAY, FakeBroker
from tests.test_bot_engine import (
  BULL_SPREAD,
  add_spread_position,
  held_spread,
  make_engine,
  quote,
)

RBA_CSV = """﻿F11.1  EXCHANGE RATES
Title,A$1=USD,Trade-weighted Index May 1970 = 100
Units,USD,Index
Series ID,FXRUSD,FXRTWI
20-Oct-2026,0.6600,61.0
21-Oct-2026,0.6500,61.0
23-Oct-2026,,61.0
"""

# ------------------------------------------------------------- notifier

async def test_notifier_cooldown_and_log_only() -> None:
  """Without a topic it only logs; keyed warnings send once per cooldown."""
  n = Notifier()
  await n.send("A", "x", key="k")
  await n.send("A", "x again", key="k")
  await n.send("B", "y")
  assert [t for t, _, _ in n.sent] == ["A", "B"]
  await n.heartbeat()  # no URL: does nothing

# ----------------------------------------------------------- engine hooks

async def test_open_and_close_are_notified_and_journalled(tmp_path: Path,
                                                         claude_says: list) -> None:
  """Entry fill and stop-loss close each send a message and record cash flows."""
  claude_says[0](BULL_SPREAD)
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  await engine.tick()
  titles = [t for t, _, _ in engine.notifier.sent]
  assert any(t.startswith("Opened P") for t in titles)
  [fill] = engine.store.fills()
  assert (fill["kind"], fill["cash_usd"]) == ("open", pytest.approx(-(41 + 1.30)))

  quote(broker, 1602, 3.00, 3.10)
  quote(broker, 1603, 2.80, 2.85)
  engine.clocks.last_exit = None
  await engine.tick()
  kinds = [f["kind"] for f in engine.store.fills()]
  assert kinds == ["open", "close"]
  assert any(t.startswith("Closed P") for t, _, _ in engine.notifier.sent)
  realized = engine.store.realized_total()
  assert sum(f["cash_usd"] for f in engine.store.fills()) == pytest.approx(realized)

async def test_kill_switch_is_urgent(tmp_path: Path) -> None:
  """A kill-switch trip is sent at urgent priority."""
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
  await engine.tick()
  assert ("KILL SWITCH TRIPPED", "urgent") in [(t, p) for t, _, p in engine.notifier.sent]

async def test_daily_summary_sent_once_after_close(tmp_path: Path) -> None:
  """After the close: one summary with equity and go-live progress."""
  broker = FakeBroker(now=dt.datetime(2026, 10, 21, 20, 15, tzinfo=dt.UTC))
  engine = make_engine(tmp_path, broker)
  engine.store.set_state("capital_usd", "330.0")
  await engine.tick()
  await engine.tick()
  summaries = [m for t, m, _ in engine.notifier.sent if t.startswith("Daily summary")]
  assert len(summaries) == 1
  assert "Go-live: 0/40 trades" in summaries[0]
  assert "Equity $330.00" in summaries[0]

async def test_equity_snapshot_and_drawdown(tmp_path: Path) -> None:
  """Each exit pass stores the day's equity; drawdown is peak to trough."""
  broker = FakeBroker()
  engine = make_engine(tmp_path, broker)
  store = engine.store
  store.record_equity(TODAY - dt.timedelta(days=2), 330, 30, 0)   # 360 peak
  store.record_equity(TODAY - dt.timedelta(days=1), 330, -6, 0)   # 324
  assert store.max_drawdown_pct() == 10.0
  await engine.exit_cycle()
  row = store.conn.execute("SELECT equity_usd FROM equity WHERE day = ?",
                           (TODAY.isoformat(),)).fetchone()
  assert row["equity_usd"] == pytest.approx(330.0)

async def test_heartbeat_only_around_market_hours(tmp_path: Path,
                                                  claude_says: list) -> None:
  """The dead-man's switch is pinged near US hours, not at 3am New York."""
  claude_says[0](None, "not needed")
  beats: list[int] = []
  class CountingNotifier(Notifier):
    async def heartbeat(self) -> None:
      beats.append(1)
  broker = FakeBroker(now=dt.datetime(2026, 10, 21, 7, 0, tzinfo=dt.UTC))
  engine = make_engine(tmp_path, broker)
  engine.notifier = CountingNotifier()
  await engine.tick()
  assert beats == []
  broker.now = NOW
  await engine.tick()
  assert beats == [1]

# ------------------------------------------------------------------ tax

def test_parse_rba_and_rate_lookup() -> None:
  """FXRUSD column, blanks skipped, falls back to the previous published rate."""
  rates = parse_rba_f11(RBA_CSV)
  assert rates == {dt.date(2026, 10, 20): 0.66, dt.date(2026, 10, 21): 0.65}
  assert rate_on(rates, dt.date(2026, 10, 24)) == (dt.date(2026, 10, 21), 0.65)
  with pytest.raises(LookupError):
    rate_on(rates, dt.date(2026, 12, 1))

def test_financial_year() -> None:
  """FY2027 runs July 2026 to June 2027."""
  assert financial_year(2027) == (dt.date(2026, 7, 1), dt.date(2027, 6, 30))

def test_tax_rows_convert_each_cash_flow_at_its_date(tmp_path: Path) -> None:
  """Cost at the opening date's rate, proceeds at the closing date's rate."""
  store = Store(connect(tmp_path / "bot.sqlite"))
  from tests.test_bot_units import position
  store.add_position(position(opened_at="2026-10-20T15:00:00+00:00"))
  store.record_fill(position_id="P1", kind="open", description="BUY", quantity=1,
                    price=0.44, multiplier=100, commission_usd=1.30, cash_usd=-45.30,
                    at="2026-10-20T15:00:00+00:00")
  store.record_fill(position_id="P1", kind="close", description="SELL", quantity=1,
                    price=0.80, multiplier=100, commission_usd=1.30, cash_usd=78.70,
                    at="2026-10-21T15:00:00+00:00")
  store.reduce_position("P1", 1, 0.80, 33.40, "profit_target")
  with store.conn:
    store.conn.execute("UPDATE positions SET closed_at = '2026-10-21T15:00:00+00:00'")
  positions, details = build_tax_rows(store, parse_rba_f11(RBA_CSV), 2027)
  [p] = positions
  # Perth dates: open 20 Oct 23:00 -> rate 0.66; close 21 Oct 23:00 -> rate 0.65.
  assert p["cost_base_aud"] == pytest.approx(45.30 / 0.66, abs=0.01)
  assert p["proceeds_aud"] == pytest.approx(78.70 / 0.65, abs=0.01)
  assert p["gain_usd"] == pytest.approx(33.40)
  assert [d["rba_aud_usd"] for d in details] == [0.66, 0.65]
  assert build_tax_rows(store, parse_rba_f11(RBA_CSV), 2026) == ([], [])

def test_field_lists_match_rows(tmp_path: Path) -> None:
  """The CSV headers cover exactly the keys each row has."""
  from src.bot.tax import CASHFLOW_FIELDS, POSITION_FIELDS
  test_tax_rows_convert_each_cash_flow_at_its_date(tmp_path)
  store = Store(connect(tmp_path / "bot.sqlite"))
  positions, details = build_tax_rows(store, parse_rba_f11(RBA_CSV), 2027)
  assert list(positions[0]) == POSITION_FIELDS
  assert list(details[0]) == CASHFLOW_FIELDS
