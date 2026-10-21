"""Price survey: log real SPY/QQQ option and debit-spread prices to SQLite.

Read-only. It answers PLAN.md §4's open question (what a 1-lot trade really
costs against the per-trade cap) with real quotes instead of estimates.
"""
import asyncio
import datetime as dt
import math
import sqlite3
import statistics
from collections import defaultdict
from itertools import batched
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as ecals
import pandas as pd
from ib_async import Forex, Option, Stock, Ticker
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.ib_helper.client import IBClient
from src.storage import connect
from src.survey.candidates import (
  Candidate,
  OptionQuote,
  build_debit_vertical,
  build_long_option,
  round_trip_commission,
)
from src.utilities import Settings
from src.utilities.strategy_settings import StrategySettings, StrategySettingsStore

NEW_YORK = ZoneInfo("America/New_York")
TICKER_BATCH = 50
CLOSED_MARKET_POLL = dt.timedelta(minutes=15)

def _num(x: float | None) -> float:
  return math.nan if x is None or x == -1 else float(x)

class SurveyClient(IBClient):
  """Read-only IBKR queries for the survey."""

  async def ensure_connected(self) -> None:
    """Connect, or reconnect after a Gateway restart."""
    await self._connect()

  async def audusd(self) -> float | None:
    """AUD.USD midpoint from IDEALPRO, or None if unavailable."""
    [ticker] = await self.ib.reqTickersAsync(Forex("AUDUSD"))
    mid = ticker.midpoint()
    return None if math.isnan(mid) else mid

  async def spot(self, symbol: str) -> tuple[Stock, float]:
    """Qualified stock contract and its current price."""
    stock = Stock(symbol, "SMART", "USD")
    await self.ib.qualifyContractsAsync(stock)
    [ticker] = await self.ib.reqTickersAsync(stock)
    price = ticker.marketPrice()
    if math.isnan(price):
      price = _num(ticker.close)
    return stock, price

  async def option_quotes(
    self,
    stock: Stock,
    spot: float,
    today: dt.date,
    strategy: StrategySettings,
  ) -> list[OptionQuote]:
    """Quotes for calls and puts near the money, for expiries in the DTE range."""
    chains = await self.ib.reqSecDefOptParamsAsync(
      stock.symbol, "", "STK", stock.conId,
    )
    chain = next(
      c for c in chains
      if c.exchange == "SMART" and c.tradingClass == stock.symbol
    )
    entries = strategy.entries
    expiries = sorted(
      e for e in chain.expirations
      if entries.min_dte
      <= (dt.datetime.strptime(e, "%Y%m%d").date() - today).days  # noqa: DTZ007
      <= entries.max_dte
    )
    window = (
      spot * strategy.survey.strike_window_pct / 100 + max(entries.vertical_widths)
    )
    strikes = sorted(k for k in chain.strikes if abs(k - spot) <= window)
    contracts = [
      Option(stock.symbol, expiry, strike, right, "SMART", tradingClass=stock.symbol)
      for expiry in expiries for strike in strikes for right in ("C", "P")
    ]
    qualified = [
      c for c in await self.ib.qualifyContractsAsync(*contracts)
      if isinstance(c, Option)
    ]
    tickers: list[Ticker] = []
    for batch in batched(qualified, TICKER_BATCH):
      tickers += await self.ib.reqTickersAsync(*batch)
    return [_to_quote(t) for t in tickers]

def _to_quote(ticker: Ticker) -> OptionQuote:
  c = ticker.contract
  greeks = ticker.modelGreeks
  delta = None if greeks is None or greeks.delta is None else greeks.delta
  return OptionQuote(
    symbol=c.symbol,
    expiry=dt.datetime.strptime(c.lastTradeDateOrContractMonth, "%Y%m%d").date(),  # noqa: DTZ007
    right=c.right,
    strike=c.strike,
    bid=_num(ticker.bid),
    ask=_num(ticker.ask),
    delta=delta,
  )

def build_candidates(
  quotes: list[OptionQuote],
  today: dt.date,
  strategy: StrategySettings,
) -> list[Candidate]:
  """All long-option and debit-vertical candidates for each expiry and right."""
  groups: dict[tuple, list[OptionQuote]] = defaultdict(list)
  for q in quotes:
    groups[(q.symbol, q.expiry, q.right)].append(q)
  costs = strategy.costs
  single_fee = round_trip_commission(
    1, 1, costs.commission_per_contract, costs.commission_min_per_order,
  )
  spread_fee = round_trip_commission(
    2, 1, costs.commission_per_contract, costs.commission_min_per_order,
  )
  candidates = []
  for group in groups.values():
    long_option = build_long_option(
      group, strategy.entries.long_option_delta, today, single_fee,
    )
    if long_option:
      candidates.append(long_option)
    for width in strategy.entries.vertical_widths:
      vertical = build_debit_vertical(
        group, strategy.entries.vertical_long_delta, width, today, spread_fee,
      )
      if vertical:
        candidates.append(vertical)
  return candidates

def summarize(candidates: list[Candidate], cap_usd: float | None) -> list[dict]:
  """Summarize per (symbol, structure, width): count, median cost, fits in cap."""
  groups: dict[tuple, list[Candidate]] = defaultdict(list)
  for c in candidates:
    groups[(c.symbol, c.structure, c.width)].append(c)
  rows = []
  for (symbol, structure, width), group in sorted(
    groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0),
  ):
    losses = [c.max_loss_mid_usd for c in group]
    rows.append({
      "symbol": symbol,
      "structure": structure,
      "width": width,
      "count": len(group),
      "median_max_loss_usd": round(statistics.median(losses), 2),
      "min_max_loss_usd": round(min(losses), 2),
      "fit_cap": None if cap_usd is None else sum(x <= cap_usd for x in losses),
      "median_leg_spread_pct": round(
        statistics.median(c.max_leg_spread_pct for c in group), 1,
      ),
    })
  return rows

def save(  # noqa: PLR0913
  conn: sqlite3.Connection,
  run_at: str,
  *,
  live_data: bool,
  audusd: float | None,
  cap_usd: float | None,
  spots: dict[str, float],
  candidates: list[Candidate],
) -> None:
  """Insert one row per candidate."""
  rows = [
    (
      run_at, int(live_data), audusd, cap_usd, c.symbol, spots[c.symbol],
      c.expiry.isoformat(), c.dte, c.right, c.structure, c.width,
      c.long.strike, c.long.delta, c.long.bid, c.long.ask,
      c.short.strike if c.short else None,
      c.short.delta if c.short else None,
      c.short.bid if c.short else None,
      c.short.ask if c.short else None,
      c.debit_mid, c.debit_natural, c.commission_usd,
      round(c.max_loss_mid_usd, 2), round(c.max_loss_natural_usd, 2),
      round(c.max_leg_spread_pct, 2),
      None if cap_usd is None else int(c.max_loss_mid_usd <= cap_usd),
    )
    for c in candidates
  ]
  with conn:
    conn.executemany(
      "INSERT INTO price_survey (run_at, live_data, audusd, cap_usd, symbol, spot,"
      " expiry, dte, option_right, structure, width, long_strike, long_delta,"
      " long_bid, long_ask, short_strike, short_delta, short_bid, short_ask,"
      " debit_mid, debit_natural, commission_usd, max_loss_mid_usd,"
      " max_loss_natural_usd, max_leg_spread_pct, fits_cap)"
      " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
      ", ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      rows,
    )

def print_summary(
  rows: list[dict], cap_usd: float | None, audusd: float | None,
) -> None:
  """Print the summary table."""
  cap = "unknown (no AUD.USD quote)" if cap_usd is None else f"US${cap_usd:.2f}"
  rate = "n/a" if audusd is None else f"{audusd:.4f}"
  table = Table(title=f"Price survey: per-trade cap {cap} (AUD.USD {rate})")
  for col in ("Symbol", "Structure", "Width", "Count", "Median max loss",
              "Cheapest", "Fit cap", "Median leg spread %"):
    table.add_column(col)
  for r in rows:
    table.add_row(
      r["symbol"], r["structure"], "" if r["width"] is None else f"${r['width']:g}",
      str(r["count"]), f"${r['median_max_loss_usd']:.2f}",
      f"${r['min_max_loss_usd']:.2f}",
      "?" if r["fit_cap"] is None else f"{r['fit_cap']}/{r['count']}",
      f"{r['median_leg_spread_pct']}",
    )
  Console().print(table)

async def survey_once(
  client: SurveyClient,
  conn: sqlite3.Connection,
  strategy: StrategySettings,
  *,
  live_data: bool,
) -> list[dict]:
  """Run one survey, save it and return the summary rows."""
  client.ib.reqMarketDataType(1 if live_data else 2)
  today = dt.datetime.now(NEW_YORK).date()
  run_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
  audusd = await client.audusd()
  cap_usd = None if audusd is None else round(
    strategy.capital.capital_aud * audusd * strategy.risk.risk_per_trade_pct / 100, 2,
  )
  spots: dict[str, float] = {}
  candidates: list[Candidate] = []
  for symbol in strategy.entries.underlyings:
    stock, spot = await client.spot(symbol)
    if math.isnan(spot):
      logger.warning("No price for {}, skipping", symbol)
      continue
    spots[symbol] = spot
    quotes = await client.option_quotes(stock, spot, today, strategy)
    candidates += build_candidates(quotes, today, strategy)
  save(conn, run_at, live_data=live_data, audusd=audusd, cap_usd=cap_usd,
       spots=spots, candidates=candidates)
  rows = summarize(candidates, cap_usd)
  print_summary(rows, cap_usd, audusd)
  logger.info("Survey saved {} candidates at {}", len(candidates), run_at)
  return rows

async def run_price_survey(settings: Settings, *, once: bool) -> None:
  """Survey every interval during NYSE hours; with `once`, run one survey now."""
  data_dir = Path(settings.data_dir)
  store = StrategySettingsStore(data_dir / "settings.toml")
  conn = connect(data_dir / "bot.sqlite")
  client = SurveyClient(readonly=True)
  nyse = ecals.get_calendar("XNYS")
  while True:
    store.reload()
    now = pd.Timestamp.now(tz="UTC").floor("min")
    market_open = nyse.is_open_on_minute(now)
    if once or market_open:
      try:
        await client.ensure_connected()
        await survey_once(client, conn, store.current, live_data=market_open)
      except Exception:
        logger.exception("Survey failed")
        if once:
          raise
      if once:
        return
      await asyncio.sleep(store.current.survey.interval_minutes * 60)
    else:
      next_open = nyse.next_open(now)
      wait = min(next_open - now, pd.Timedelta(CLOSED_MARKET_POLL))
      logger.debug("Market closed; next open {}, sleeping {}", next_open, wait)
      await asyncio.sleep(max(wait.total_seconds(), 60))
