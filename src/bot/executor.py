"""Send approved orders: limit at a start price, a short reprice ladder, then cancel.

The intent is journalled before anything is sent. If the outcome can't be
established the order is marked 'unknown' and never blindly retried; the next
reconcile works it out from IBKR's positions.
"""
import asyncio
from dataclasses import dataclass

from loguru import logger

from src.bot.broker import Broker, OrderHandle, OrderStatus
from src.bot.store import Store
from src.guard import Approval

@dataclass(frozen=True)
class ExecResult:
  """What happened to an order."""

  status: str  # filled | partial | cancelled | rejected | unknown | dry_run
  filled: float
  avg_price: float | None
  commission: float | None
  order_row: int
  perm_id: int | None = None

def ladder(start: float, limit: float, step: float, *, paying: bool) -> list[float]:
  """Prices from start towards limit (never past it), at most 3 moves."""
  start = min(start, limit) if paying else max(start, limit)
  prices = [round(start, 2)]
  for _ in range(3):
    nxt = prices[-1] + step if paying else prices[-1] - step
    nxt = min(nxt, limit) if paying else max(nxt, limit)
    nxt = round(nxt, 2)
    if nxt == prices[-1]:
      break
    prices.append(nxt)
  return prices

async def _wait(broker: Broker, handle: OrderHandle, seconds: float,
                poll: float) -> OrderStatus:
  status = await broker.status(handle)
  waited = 0.0
  while not status.terminal and waited < seconds:
    await asyncio.sleep(poll)
    waited += max(poll, 0.01)
    status = await broker.status(handle)
  return status

async def execute(  # noqa: C901, PLR0913
  broker: Broker,
  store: Store,
  approval: Approval,
  *,
  start_price: float,
  step: float,
  position_id: str | None = None,
  fingerprint: str | None = None,
  interval_s: float = 20.0,
  poll_s: float = 1.0,
) -> ExecResult:
  """Work an approved order through the ladder; journal every step."""
  intent = approval.intent
  paying = intent.action == "BUY"
  prices = ladder(start_price, intent.limit_price, step, paying=paying)
  row = store.record_order(intent, position_id=position_id, fingerprint=fingerprint,
                           start_price=prices[0], dry_run=broker.dry_run)
  if broker.dry_run:
    store.update_order(row, status="dry_run")
    logger.info("DRY RUN: would {} {} x{} from {} to {}", intent.action,
                [leg.con_id for leg in intent.legs], intent.quantity,
                prices[0], prices[-1])
    return ExecResult("dry_run", 0, None, None, row)
  try:
    handle = await broker.place(approval, prices[0])
  except Exception:
    logger.exception("Order placement failed")
    store.update_order(row, status="rejected")
    return ExecResult("rejected", 0, None, None, row)
  store.update_order(row, status="sent", order_id=handle.order_id,
                     perm_id=handle.perm_id)

  status = OrderStatus("Submitted", 0, None)
  try:
    for i, price in enumerate(prices):
      if i:
        await broker.modify(handle, price)
      status = await _wait(broker, handle, interval_s, poll_s)
      if status.terminal:
        break
    if not status.terminal:
      await broker.cancel(handle)
      status = await _wait(broker, handle, 15.0, poll_s)
  except Exception:
    logger.exception("Lost track of order {}", handle.order_id)
    store.update_order(row, status="unknown", perm_id=handle.perm_id)
    return ExecResult("unknown", status.filled, status.avg_price, status.commission,
                      row, handle.perm_id)

  if status.filled >= intent.quantity:
    result = "filled"
  elif status.filled > 0 and status.terminal:
    result = "partial"
  elif status.terminal:
    result = "cancelled" if status.status != "Inactive" else "rejected"
  else:
    result = "unknown"
  store.update_order(row, status=result, filled_qty=status.filled,
                     avg_fill_price=status.avg_price, perm_id=handle.perm_id)
  logger.info("Order {} {}: {}/{} at {}", handle.order_id, result, status.filled,
              intent.quantity, status.avg_price)
  return ExecResult(result, status.filled, status.avg_price, status.commission, row,
                    handle.perm_id)

async def place_resting(
  broker: Broker, store: Store, approval: Approval, *, position_id: str,
) -> OrderHandle | None:
  """Place a resting (GTC) order and leave it working."""
  intent = approval.intent
  row = store.record_order(intent, position_id=position_id, fingerprint=None,
                           start_price=intent.limit_price, dry_run=broker.dry_run)
  if broker.dry_run:
    store.update_order(row, status="dry_run")
    return None
  try:
    handle = await broker.place(approval, intent.limit_price)
  except Exception:
    logger.exception("Resting order failed")
    store.update_order(row, status="rejected")
    return None
  store.update_order(row, status="resting", order_id=handle.order_id,
                     perm_id=handle.perm_id)
  return handle
