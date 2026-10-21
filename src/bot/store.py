"""The bot's own book in SQLite: state, positions, orders and Claude's decisions."""
import datetime as dt
import json
import sqlite3
from dataclasses import asdict, dataclass

from src.guard.models import OpenPosition, OrderIntent

def _now() -> str:
  return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

@dataclass(frozen=True)
class PositionRow:
  """A position the bot opened."""

  id: str
  symbol: str
  expiry: dt.date
  right: str
  structure: str
  quantity: int
  long_con_id: int
  long_strike: float
  short_con_id: int | None
  short_strike: float | None
  entry_price: float
  entry_commission: float
  max_loss_usd: float
  opened_at: str
  opened_on: dt.date
  tp_order_id: int | None
  tp_perm_id: int | None
  realized_pnl_usd: float

  @property
  def width(self) -> float | None:
    """Strike distance for a vertical."""
    if self.short_strike is None:
      return None
    return abs(self.short_strike - self.long_strike)

  @property
  def con_ids(self) -> list[int]:
    """Leg contract IDs, long first."""
    return [self.long_con_id] + ([self.short_con_id] if self.short_con_id else [])

  def as_guard_position(self) -> OpenPosition:
    """Return the guard's view of this position."""
    return OpenPosition(
      position_id=self.id, symbol=self.symbol, expiry=self.expiry,
      right=self.right, structure=self.structure, quantity=self.quantity,
      max_loss_usd=self.max_loss_usd, opened_on=self.opened_on,
    )

def _row_to_position(r: sqlite3.Row) -> PositionRow:
  return PositionRow(
    id=r["id"], symbol=r["symbol"], expiry=dt.date.fromisoformat(r["expiry"]),
    right=r["option_right"], structure=r["structure"], quantity=r["quantity"],
    long_con_id=r["long_con_id"], long_strike=r["long_strike"],
    short_con_id=r["short_con_id"], short_strike=r["short_strike"],
    entry_price=r["entry_price"], entry_commission=r["entry_commission"],
    max_loss_usd=r["max_loss_usd"], opened_at=r["opened_at"],
    opened_on=dt.date.fromisoformat(r["opened_on"]),
    tp_order_id=r["tp_order_id"], tp_perm_id=r["tp_perm_id"],
    realized_pnl_usd=r["realized_pnl_usd"],
  )

class Store:
  """Read and write the bot's book."""

  def __init__(self, conn: sqlite3.Connection) -> None:
    """Use an open, migrated connection."""
    self.conn = conn

  # ------------------------------------------------------------------ state

  def get_state(self, key: str) -> str | None:
    """Read a state value."""
    row = self.conn.execute(
      "SELECT value FROM bot_state WHERE key = ?", (key,),
    ).fetchone()
    return None if row is None else row["value"]

  def set_state(self, key: str, value: str) -> None:
    """Write a state value."""
    with self.conn:
      self.conn.execute(
        "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (key, value, _now()),
      )

  # -------------------------------------------------------------- positions

  def open_positions(self) -> list[PositionRow]:
    """All open positions, oldest first."""
    rows = self.conn.execute(
      "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at",
    ).fetchall()
    return [_row_to_position(r) for r in rows]

  def add_position(self, p: PositionRow) -> None:
    """Record a newly opened position."""
    with self.conn:
      self.conn.execute(
        "INSERT INTO positions (id, symbol, expiry, option_right, structure,"
        " quantity, long_con_id, long_strike, short_con_id, short_strike,"
        " entry_price, entry_commission, max_loss_usd, opened_at, opened_on)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p.id, p.symbol, p.expiry.isoformat(), p.right, p.structure, p.quantity,
         p.long_con_id, p.long_strike, p.short_con_id, p.short_strike,
         p.entry_price, p.entry_commission, p.max_loss_usd, p.opened_at,
         p.opened_on.isoformat()),
      )

  def set_take_profit(
    self, position_id: str, order_id: int | None, perm_id: int | None,
  ) -> None:
    """Remember (or clear, with None) the resting take-profit order."""
    with self.conn:
      self.conn.execute(
        "UPDATE positions SET tp_order_id = ?, tp_perm_id = ? WHERE id = ?",
        (order_id, perm_id, position_id),
      )

  def reduce_position(
    self, position_id: str, quantity: int, exit_price: float | None,
    realized_pnl_usd: float, reason: str,
  ) -> None:
    """Close `quantity` of a position; closes it fully when nothing is left."""
    p = next(x for x in self.open_positions() if x.id == position_id)
    left = p.quantity - quantity
    keep = left / p.quantity if left > 0 else 1.0  # history keeps the last size
    with self.conn:
      self.conn.execute(
        "UPDATE positions SET quantity = ?, max_loss_usd = ?, entry_commission = ?,"
        " realized_pnl_usd = realized_pnl_usd + ?, exit_price = ?,"
        " exit_reason = ?, status = ?, closed_at = ? WHERE id = ?",
        (
          left if left > 0 else p.quantity,
          round(p.max_loss_usd * keep, 2),
          round(p.entry_commission * keep, 2),
          realized_pnl_usd, exit_price, reason,
          "open" if left > 0 else "closed",
          None if left > 0 else _now(),
          position_id,
        ),
      )

  def add_realized(self, position_id: str, pnl_usd: float) -> None:
    """Add P&L from something tied to a position (e.g. stock after assignment)."""
    with self.conn:
      self.conn.execute(
        "UPDATE positions SET realized_pnl_usd = realized_pnl_usd + ? WHERE id = ?",
        (pnl_usd, position_id),
      )

  def has_position(self, position_id: str) -> bool:
    """Whether a position with this ID was ever recorded."""
    row = self.conn.execute(
      "SELECT 1 FROM positions WHERE id = ?", (position_id,),
    ).fetchone()
    return row is not None

  def realized_total(self) -> float:
    """Realised P&L over all positions (USD)."""
    row = self.conn.execute(
      "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS total FROM positions",
    ).fetchone()
    return float(row["total"])

  def consecutive_losses(self) -> int:
    """Losing closed trades in a row, most recent first."""
    rows = self.conn.execute(
      "SELECT realized_pnl_usd FROM positions WHERE status = 'closed'"
      " ORDER BY closed_at DESC, rowid DESC",
    ).fetchall()
    n = 0
    for r in rows:
      if r["realized_pnl_usd"] >= 0:
        break
      n += 1
    return n

  def trades_opened_on(self, day: dt.date) -> int:
    """Positions opened on a New York trading day."""
    row = self.conn.execute(
      "SELECT COUNT(*) AS n FROM positions WHERE opened_on = ?", (day.isoformat(),),
    ).fetchone()
    return int(row["n"])

  # ----------------------------------------------------------------- orders

  def record_order(  # noqa: PLR0913
    self, intent: OrderIntent, *, position_id: str | None, fingerprint: str | None,
    start_price: float, dry_run: bool, status: str = "intent",
  ) -> int:
    """Journal an order intent BEFORE it is sent; returns the row id."""
    now = _now()
    with self.conn:
      cur = self.conn.execute(
        "INSERT INTO orders (created_at, updated_at, purpose, reason, position_id,"
        " fingerprint, intent_json, start_price, limit_price, status, dry_run)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now, now, intent.purpose, intent.close_reason, position_id, fingerprint,
         json.dumps(asdict(intent), default=str), start_price, intent.limit_price,
         status, int(dry_run)),
      )
    return int(cur.lastrowid)

  def update_order(self, order_row: int, **fields: object) -> None:
    """Update journal fields (status, order_id, perm_id, filled_qty, ...)."""
    allowed = {"status", "order_id", "perm_id", "filled_qty", "avg_fill_price",
               "position_id"}
    if not fields or not set(fields) <= allowed:
      msg = f"bad order fields {sorted(fields)}"
      raise ValueError(msg)
    sets = ", ".join(f"{k} = ?" for k in fields)
    with self.conn:
      self.conn.execute(
        f"UPDATE orders SET {sets}, updated_at = ? WHERE id = ?",  # noqa: S608
        (*fields.values(), _now(), order_row),
      )

  def unresolved_open_fingerprints(self) -> frozenset[str]:
    """Return opening orders whose outcome is unknown (blocks duplicates)."""
    rows = self.conn.execute(
      "SELECT fingerprint FROM orders WHERE purpose = 'open'"
      " AND status IN ('intent', 'sent', 'unknown') AND fingerprint IS NOT NULL",
    ).fetchall()
    return frozenset(r["fingerprint"] for r in rows)

  def unresolved_orders(self) -> list[sqlite3.Row]:
    """Orders sent but never resolved (e.g. the bot crashed mid-order)."""
    return self.conn.execute(
      "SELECT * FROM orders WHERE status IN ('intent', 'sent', 'unknown')"
      " AND dry_run = 0",
    ).fetchall()

  # ------------------------------------------------------------------ fills

  def record_fill(  # noqa: PLR0913
    self, *, position_id: str | None, kind: str, description: str,
    quantity: float, price: float | None, multiplier: int, commission_usd: float,
    cash_usd: float, at: str | None = None,
  ) -> None:
    """Journal money moving: cash_usd < 0 paid, > 0 received (after commission)."""
    with self.conn:
      self.conn.execute(
        "INSERT INTO fills (at, position_id, kind, description, quantity, price,"
        " multiplier, commission_usd, cash_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (at or _now(), position_id, kind, description, quantity, price, multiplier,
         round(commission_usd, 2), round(cash_usd, 2)),
      )

  def fills(self) -> list[sqlite3.Row]:
    """Every fill, oldest first."""
    return self.conn.execute("SELECT * FROM fills ORDER BY at, id").fetchall()

  def closed_positions(self) -> list[sqlite3.Row]:
    """Return closed positions, oldest close first."""
    return self.conn.execute(
      "SELECT * FROM positions WHERE status = 'closed' ORDER BY closed_at, rowid",
    ).fetchall()

  # ----------------------------------------------------------------- equity

  def record_equity(
    self, day: dt.date, capital: float, realized: float, unrealized: float,
  ) -> None:
    """Store (or replace) the day's closing equity."""
    with self.conn:
      self.conn.execute(
        "INSERT OR REPLACE INTO equity (day, at, capital_usd, realized_usd,"
        " unrealized_usd, equity_usd) VALUES (?, ?, ?, ?, ?, ?)",
        (day.isoformat(), _now(), capital, round(realized, 2),
         round(unrealized, 2), round(capital + realized + unrealized, 2)),
      )

  def max_drawdown_pct(self) -> float:
    """Largest peak-to-trough fall in daily equity, as % of the peak."""
    rows = self.conn.execute("SELECT equity_usd FROM equity ORDER BY day").fetchall()
    peak, worst = None, 0.0
    for r in rows:
      e = r["equity_usd"]
      peak = e if peak is None else max(peak, e)
      if peak > 0:
        worst = max(worst, (peak - e) / peak * 100)
    return round(worst, 1)

  def day_stats(self, day: dt.date) -> dict:
    """Summarise what happened on a New York trading day."""
    d = day.isoformat()
    opened = self.conn.execute(
      "SELECT COUNT(*) AS n FROM positions WHERE opened_on = ?", (d,),
    ).fetchone()["n"]
    # US regular hours fall inside one UTC date, so UTC timestamps match the day.
    closed = self.conn.execute(
      "SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl_usd), 0) AS pnl"
      " FROM positions WHERE status = 'closed' AND substr(closed_at, 1, 10) = ?",
      (d,),
    ).fetchone()
    outcomes = dict(self.conn.execute(
      "SELECT COALESCE(outcome, 'none'), COUNT(*) FROM decisions"
      " WHERE substr(decided_at, 1, 10) = ? GROUP BY 1",
      (d,),
    ).fetchall())
    return {"opened": opened, "closed": closed["n"], "closed_pnl": closed["pnl"],
            "decisions": outcomes}

  def closed_count(self) -> int:
    """Count closed positions so far."""
    return self.conn.execute(
      "SELECT COUNT(*) AS n FROM positions WHERE status = 'closed'",
    ).fetchone()["n"]

  # -------------------------------------------------------------- decisions

  def record_decision(self, **fields: object) -> int:
    """Store one Claude decision (including abstains and errors)."""
    cols = ["model", "underlying", "direction", "structure", "confidence",
            "rationale", "outcome", "error"]
    values = [fields.get(c) for c in cols]
    with self.conn:
      cur = self.conn.execute(
        f"INSERT INTO decisions (decided_at, {', '.join(cols)})"  # noqa: S608
        f" VALUES (?, {', '.join('?' * len(cols))})",
        (_now(), *values),
      )
    return int(cur.lastrowid)

  def set_decision_outcome(self, decision_id: int, outcome: str) -> None:
    """Record what happened after a decision (no trade, rejected, filled ...)."""
    with self.conn:
      self.conn.execute(
        "UPDATE decisions SET outcome = ? WHERE id = ?", (outcome, decision_id),
      )
