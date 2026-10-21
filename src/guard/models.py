"""Inputs and outputs of the risk guard (broker-agnostic, so gates stay pure)."""
import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

Action = Literal["BUY", "SELL"]
Right = Literal["C", "P"]
Purpose = Literal["open", "close"]
CloseReason = Literal[
  "assignment", "expiry", "stop", "profit_target", "time_stop", "kill_switch",
]
EMERGENCY_CLOSE_REASONS: frozenset[str] = frozenset(
  {"assignment", "expiry", "kill_switch"},
)

@dataclass(frozen=True)
class Leg:
  """One leg of an order, with its latest quote."""

  con_id: int
  symbol: str
  sec_type: str
  action: Action
  ratio: int = 1
  expiry: dt.date | None = None
  right: Right | None = None
  strike: float | None = None
  multiplier: int = 100
  bid: float | None = None
  ask: float | None = None
  quote_age_seconds: float | None = None

@dataclass(frozen=True)
class OrderIntent:
  """An order the bot wants to send, before the guard has seen it.

  `limit_price` is per unit of the combo: positive means paying (a debit) when
  `action` is BUY, receiving when SELL.
  """

  purpose: Purpose
  legs: tuple[Leg, ...]
  action: Action
  quantity: int
  order_type: str
  limit_price: float | None
  tif: str = "DAY"
  close_reason: CloseReason | None = None

@dataclass(frozen=True)
class OpenPosition:
  """A position the bot opened and still holds."""

  position_id: str
  symbol: str
  expiry: dt.date
  right: Right
  structure: Literal["long_option", "debit_vertical"]
  quantity: int
  max_loss_usd: float
  opened_on: dt.date

@dataclass(frozen=True)
class Holding:
  """Something held in the account (signed quantity: negative = short)."""

  con_id: int
  symbol: str
  sec_type: str
  quantity: float
  expiry: dt.date | None = None
  right: Right | None = None
  strike: float | None = None
  avg_cost: float | None = None

@dataclass(frozen=True)
class AccountState:
  """Broker account snapshot."""

  account_id: str
  usd_cash: float
  day_trades_remaining: int | None = None
  # Everything held in the account, by conId.
  holdings: dict[int, Holding] = field(default_factory=dict)

@dataclass(frozen=True)
class BookState:
  """The bot's own bookkeeping."""

  capital_usd: float
  realized_pnl_usd: float
  open_positions: tuple[OpenPosition, ...] = ()
  # Fingerprints (see structure.fingerprint) of opening orders still working.
  working_open_orders: frozenset[str] = frozenset()
  trades_opened_today: int = 0
  kill_switch_tripped: bool = False
  halt_file_present: bool = False

@dataclass(frozen=True)
class MarketClock:
  """Regular trading hours for today (UTC), from IBKR liquidHours."""

  now: dt.datetime
  today: dt.date
  session_open: dt.datetime | None
  session_close: dt.datetime | None

@dataclass(frozen=True)
class UnderlyingInfo:
  """Underlying price and dividend dates for one symbol."""

  price: float
  # None means "unknown"; the guard fails closed on call spreads when unknown.
  next_ex_dividend: dt.date | None
  ex_dividend_known: bool

@dataclass(frozen=True)
class WhatIf:
  """IBKR whatIfOrder preview."""

  warning: str | None
  commission_usd: float | None

@dataclass(frozen=True)
class LiveFlags:
  """Both must be set to trade a non-paper account."""

  allow_live: bool = False
  live_confirm: str | None = None

@dataclass(frozen=True)
class GateResult:
  """One gate's verdict."""

  gate: str
  passed: bool
  reason: str = ""
