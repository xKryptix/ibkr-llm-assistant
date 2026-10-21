"""Run every gate and issue an Approval only if all pass.

The Phase 3 executor accepts only an `Approval`, which can only be created here,
so there is no path from Claude (or any other code) to the broker that skips the
guard.
"""
import json
import sqlite3
from dataclasses import asdict, dataclass

from loguru import logger

from src.guard.gates import CLOSE_GATES, OPEN_GATES, GuardContext
from src.guard.models import GateResult, OrderIntent

_TOKEN = object()

@dataclass(frozen=True)
class Decision:
  """Results of every gate for one order."""

  intent: OrderIntent
  results: tuple[GateResult, ...]

  @property
  def approved(self) -> bool:
    """True only if every gate passed."""
    return bool(self.results) and all(r.passed for r in self.results)

  @property
  def failures(self) -> list[GateResult]:
    """The gates that failed."""
    return [r for r in self.results if not r.passed]

class Approval:
  """Proof that an order passed the guard. Only `evaluate` can create one."""

  def __init__(self, decision: Decision, token: object) -> None:
    """Not for direct use; call `evaluate`."""
    if token is not _TOKEN or not decision.approved:
      msg = "Approval can only be created by the guard for an approved order"
      raise PermissionError(msg)
    self.decision = decision

  @property
  def intent(self) -> OrderIntent:
    """The approved order."""
    return self.decision.intent

def _run(gate: object, ctx: GuardContext) -> GateResult:
  name = getattr(gate, "__name__", "gate").removeprefix("gate_")
  try:
    return gate(ctx)
  except Exception as e:  # a crashing gate is a failed gate
    logger.exception("Gate {} raised", name)
    return GateResult(name, passed=False, reason=f"error: {e}")

def evaluate(
  ctx: GuardContext, conn: sqlite3.Connection | None = None,
) -> tuple[Decision, Approval | None]:
  """Run all gates for the intent's purpose; record the decision if `conn` given."""
  gates = OPEN_GATES if ctx.intent.purpose == "open" else CLOSE_GATES
  if ctx.intent.purpose not in ("open", "close"):
    gates = ()
  decision = Decision(ctx.intent, tuple(_run(g, ctx) for g in gates))
  if conn is not None:
    record(conn, ctx, decision)
  if not decision.approved:
    logger.info("Guard rejected {} order: {}", ctx.intent.purpose,
                "; ".join(f"{r.gate}: {r.reason}" for r in decision.failures))
    return decision, None
  return decision, Approval(decision, _TOKEN)

def record(conn: sqlite3.Connection, ctx: GuardContext, decision: Decision) -> None:
  """Store the decision and every gate result."""
  with conn:
    conn.execute(
      "INSERT INTO guard_decisions (decided_at, purpose, approved, intent_json,"
      " results_json) VALUES (?, ?, ?, ?, ?)",
      (
        ctx.clock.now.isoformat(timespec="seconds"),
        ctx.intent.purpose,
        int(decision.approved),
        json.dumps(asdict(ctx.intent), default=str),
        json.dumps([asdict(r) for r in decision.results]),
      ),
    )
