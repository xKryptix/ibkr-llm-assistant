"""AUD tax export for the ATO (PLAN.md §6).

Every cash flow the bot journals (fills table) is converted to AUD at the RBA
rate for its Australian (Perth) date: the rate published that day, or the most
recent one before it. Each closed position is reported as one CGT event in the
Australian financial year it closed.

This is raw data for a tax agent, not advice: whether frequent short-dated
trading is on capital or revenue account is their call.
"""
import csv
import datetime as dt
import io
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from src.bot.store import Store

RBA_F11_URL = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"
PERTH = ZoneInfo("Australia/Perth")
MAX_RATE_LOOKBACK_DAYS = 10
CACHE_MAX_AGE = dt.timedelta(hours=12)
POSITION_FIELDS = [
  "position", "description", "acquired_perth", "disposed_perth", "days_held",
  "cost_base_usd", "proceeds_usd", "commissions_usd", "gain_usd", "cost_base_aud",
  "proceeds_aud", "gain_aud", "exit_reason", "note",
]
CASHFLOW_FIELDS = [
  "position", "date_perth", "kind", "description", "quantity", "price_usd",
  "multiplier", "commission_usd", "cash_usd", "rba_date", "rba_aud_usd", "cash_aud",
]

def parse_rba_f11(text: str) -> dict[dt.date, float]:
  """A$1 = USD rates by date from the RBA F11.1 CSV."""
  rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
  header = next(r for r in rows if r and r[0] == "Series ID")
  col = header.index("FXRUSD")
  rates = {}
  for r in rows[rows.index(header) + 1:]:
    if len(r) <= col or not r[col].strip():
      continue
    try:
      day = dt.datetime.strptime(r[0], "%d-%b-%Y").date()  # noqa: DTZ007
      rates[day] = float(r[col])
    except ValueError:
      continue
  return rates

def load_rba_rates(data_dir: Path) -> dict[dt.date, float]:
  """RBA rates, downloaded at most twice a day and cached in the data folder."""
  cache = data_dir / "rba_f11.csv"
  fresh = cache.exists() and (
    dt.datetime.now(dt.UTC) - dt.datetime.fromtimestamp(cache.stat().st_mtime, dt.UTC)
    < CACHE_MAX_AGE
  )
  if not fresh:
    try:
      r = httpx.get(RBA_F11_URL, timeout=30, follow_redirects=True)
      r.raise_for_status()
      cache.parent.mkdir(parents=True, exist_ok=True)
      cache.write_bytes(r.content)
    except httpx.HTTPError as e:
      if not cache.exists():
        raise
      logger.warning("RBA download failed ({}); using cached rates", e)
  return parse_rba_f11(cache.read_text(encoding="utf-8-sig"))

def rate_on(rates: dict[dt.date, float], day: dt.date) -> tuple[dt.date, float]:
  """Return the RBA rate for `day`, or the latest published before it."""
  for back in range(MAX_RATE_LOOKBACK_DAYS + 1):
    d = day - dt.timedelta(days=back)
    if d in rates:
      return d, rates[d]
  msg = f"no RBA AUD/USD rate within {MAX_RATE_LOOKBACK_DAYS} days before {day}"
  raise LookupError(msg)

def perth_date(timestamp: str) -> dt.date:
  """Australian (Perth) calendar date of a stored UTC timestamp."""
  t = dt.datetime.fromisoformat(timestamp)
  if t.tzinfo is None:
    t = t.replace(tzinfo=dt.UTC)
  return t.astimezone(PERTH).date()

def financial_year(fy: int) -> tuple[dt.date, dt.date]:
  """FY2027 = 1 July 2026 to 30 June 2027."""
  return dt.date(fy - 1, 7, 1), dt.date(fy, 6, 30)

def build_tax_rows(
  store: Store, rates: dict[dt.date, float], fy: int,
) -> tuple[list[dict], list[dict]]:
  """(one row per position closed in the FY, one row per cash flow of those)."""
  start, end = financial_year(fy)
  fills_by_position: dict[str, list] = {}
  for f in store.fills():
    fills_by_position.setdefault(f["position_id"], []).append(f)
  positions, details = [], []
  for p in store.closed_positions():
    closed = perth_date(p["closed_at"])
    if not start <= closed <= end:
      continue
    cost_usd = proceeds_usd = cost_aud = proceeds_aud = fees_usd = 0.0
    for f in fills_by_position.get(p["id"], []):
      day = perth_date(f["at"])
      rate_day, rate = rate_on(rates, day)
      cash_aud = f["cash_usd"] / rate
      fees_usd += f["commission_usd"]
      if f["cash_usd"] < 0:
        cost_usd -= f["cash_usd"]
        cost_aud -= cash_aud
      else:
        proceeds_usd += f["cash_usd"]
        proceeds_aud += cash_aud
      details.append({
        "position": p["id"], "date_perth": day.isoformat(), "kind": f["kind"],
        "description": f["description"], "quantity": f["quantity"],
        "price_usd": f["price"], "multiplier": f["multiplier"],
        "commission_usd": f["commission_usd"], "cash_usd": f["cash_usd"],
        "rba_date": rate_day.isoformat(), "rba_aud_usd": rate,
        "cash_aud": round(cash_aud, 2),
      })
    opened = perth_date(p["opened_at"])
    strikes = f"{p['long_strike']:g}" + (
      f"/{p['short_strike']:g}" if p["short_strike"] else "")
    positions.append({
      "position": p["id"],
      "description": f"{p['symbol']} {p['expiry']} {strikes}{p['option_right']} "
                     f"{p['structure']}",
      "acquired_perth": opened.isoformat(), "disposed_perth": closed.isoformat(),
      "days_held": (closed - opened).days,
      "cost_base_usd": round(cost_usd, 2), "proceeds_usd": round(proceeds_usd, 2),
      "commissions_usd": round(fees_usd, 2),
      "gain_usd": round(proceeds_usd - cost_usd, 2),
      "cost_base_aud": round(cost_aud, 2), "proceeds_aud": round(proceeds_aud, 2),
      "gain_aud": round(proceeds_aud - cost_aud, 2),
      "exit_reason": p["exit_reason"],
      "note": "worst case booked: exit not seen by the bot; check broker statement"
              if p["exit_reason"] == "external_unknown" else "",
    })
  return positions, details

def write_tax_export(store: Store, data_dir: Path, fy: int) -> list[Path]:
  """Write FY<year>_positions.csv and FY<year>_cashflows.csv under data/tax."""
  rates = load_rba_rates(data_dir)
  positions, details = build_tax_rows(store, rates, fy)
  out_dir = data_dir / "tax"
  out_dir.mkdir(parents=True, exist_ok=True)
  paths = []
  for name, rows, fields in (("positions", positions, POSITION_FIELDS),
                             ("cashflows", details, CASHFLOW_FIELDS)):
    path = out_dir / f"FY{fy}_{name}.csv"
    with path.open("w", newline="") as fh:
      writer = csv.DictWriter(fh, fieldnames=fields)
      writer.writeheader()
      writer.writerows(rows)
    paths.append(path)
  total = sum(r["gain_aud"] for r in positions)
  logger.info("FY{}: {} closed positions, net A${:.2f}", fy, len(positions), total)
  return paths
