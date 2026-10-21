"""Uncovered (undefined-risk) exposure in a set of holdings.

A short option is covered by a long option on the same underlying, expiry and
right that is at least as far in the money. Short stock is covered by spare long
calls, long stock by spare long puts (100 shares each). Anything left over is
uncovered. A closing order must never increase it, which stops e.g. selling the
long leg of a debit spread and leaving the short leg naked.
"""
from collections import defaultdict
from dataclasses import replace

from src.guard.models import Holding, Leg

Key = tuple

def _matched(
  shorts: list[tuple[float, float]], longs: list[tuple[float, float]], right: str,
) -> tuple[float, float]:
  """Cover shorts with longs; return (uncovered short qty, spare long qty)."""
  # For calls a long at a lower-or-equal strike covers; for puts higher-or-equal.
  sign = 1 if right == "C" else -1
  shorts = sorted(shorts, key=lambda s: sign * s[0])
  longs = sorted(longs, key=lambda x: sign * x[0])
  pool, uncovered, i = 0.0, 0.0, 0
  for strike, qty in shorts:
    while i < len(longs) and sign * longs[i][0] <= sign * strike:
      pool += longs[i][1]
      i += 1
    used = min(pool, qty)
    pool -= used
    uncovered += qty - used
  spare = pool + sum(q for _, q in longs[i:])
  return uncovered, spare

def uncovered_exposure(holdings: list[Holding]) -> dict[Key, float]:
  """Uncovered quantity per (symbol, kind) — options in contracts, stock in shares."""
  options: dict[tuple, dict[str, list]] = defaultdict(
    lambda: {"short": [], "long": []},
  )
  stock: dict[str, float] = defaultdict(float)
  for h in holdings:
    if h.quantity == 0:
      continue
    if h.sec_type == "STK":
      stock[h.symbol] += h.quantity
    elif h.sec_type == "OPT":
      side = "long" if h.quantity > 0 else "short"
      options[(h.symbol, h.expiry, h.right)][side].append((h.strike, abs(h.quantity)))
    else:
      stock[f"{h.symbol}:{h.sec_type}"] += h.quantity  # unknown kinds count fully

  result: dict[Key, float] = {}
  spare_longs: dict[tuple[str, str], float] = defaultdict(float)
  for (symbol, expiry, right), sides in options.items():
    uncovered, spare = _matched(sides["short"], sides["long"], right)
    result[(symbol, "short_option", right, expiry)] = uncovered
    spare_longs[(symbol, right)] += spare
  for symbol, shares in stock.items():
    if shares < 0:
      cover = spare_longs.get((symbol, "C"), 0) * 100
      result[(symbol, "short_stock")] = max(0.0, -shares - cover)
    elif shares > 0:
      cover = spare_longs.get((symbol, "P"), 0) * 100
      result[(symbol, "long_stock")] = max(0.0, shares - cover)
  return result

def after_trade(
  holdings: dict[int, Holding], legs: tuple[Leg, ...], quantity: int,
) -> list[Holding]:
  """Compute holdings after the (effective) legs fill in full."""
  new = dict(holdings)
  for leg in legs:
    change = (1 if leg.action == "BUY" else -1) * leg.ratio * quantity
    if leg.con_id in new:
      h = new[leg.con_id]
      new[leg.con_id] = replace(h, quantity=h.quantity + change)
    else:
      new[leg.con_id] = Holding(
        con_id=leg.con_id, symbol=leg.symbol, sec_type=leg.sec_type,
        quantity=change, expiry=leg.expiry, right=leg.right, strike=leg.strike,
      )
  return list(new.values())

def increased(before: dict[Key, float], after: dict[Key, float]) -> list[str]:
  """Describe every kind of uncovered exposure that grew."""
  return [
    f"{key}: {before.get(key, 0):g} -> {value:g}"
    for key, value in after.items()
    if value > before.get(key, 0) + 1e-9
  ]
