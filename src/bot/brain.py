"""Claude's view: direction and structure, or abstain (PLAN.md §5.1).

Claude sees market numbers and the bot's book, and answers with one JSON object.
It has no tools and cannot place orders. Any error, refusal, timeout or invalid
answer counts as abstain.
"""
import json
from dataclasses import dataclass
from typing import Literal

import anthropic
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SYSTEM_PROMPT = """\
You decide whether a small automated options account should open one new trade \
right now, and if so in which direction. Code you don't control picks strikes, \
sizes the trade, enforces risk limits and manages every exit; you only give a view.

What can be traded: SPY or QQQ options, 3-10 days to expiry, held at most a few \
days and always closed 2 days before expiry. Structures:
- long_option: buy one call (bullish) or put (bearish). Pays off on a quick, \
sizeable move; loses value fast if the move doesn't come.
- debit_vertical: buy a call/put spread $1-2 wide. Cheaper and less sensitive to \
implied volatility, capped profit. Usually the better choice when implied \
volatility is high relative to its past year.

Abstain unless the numbers show a clear, specific reason to expect a move in one \
direction over the next few days. Abstaining is the normal answer and costs \
nothing; most checks should end in abstain. Don't trade because a position \
"hasn't been opened today", don't average into an existing position, and don't \
trade against a position the account already holds on the same underlying.

Confidence is your honest probability (0-1) that the trade's direction is right \
over its holding period. Code ignores views below its threshold.

Reply with the JSON object only. Keep the rationale to two or three sentences \
naming the specific numbers that drove the decision."""

VIEW_SCHEMA = {
  "type": "object",
  "properties": {
    "underlying": {"type": "string", "enum": ["SPY", "QQQ", "none"]},
    "direction": {"type": "string", "enum": ["bullish", "bearish", "abstain"]},
    "structure": {"type": "string", "enum": ["long_option", "debit_vertical"]},
    "confidence": {"type": "number"},
    "rationale": {"type": "string"},
  },
  "required": ["underlying", "direction", "structure", "confidence", "rationale"],
  "additionalProperties": False,
}

class View(BaseModel):
  """Claude's answer."""

  model_config = ConfigDict(extra="forbid")

  underlying: Literal["SPY", "QQQ", "none"]
  direction: Literal["bullish", "bearish", "abstain"]
  structure: Literal["long_option", "debit_vertical"]
  confidence: float = Field(ge=0, le=1)
  rationale: str

@dataclass(frozen=True)
class BrainResult:
  """The view plus how it was obtained."""

  view: View | None
  error: str | None
  model: str

  def tradeable(self, min_confidence: float, allowed: list[str]) -> bool:
    """Check for a direction, an allowed underlying and enough confidence."""
    v = self.view
    return (
      v is not None and v.direction != "abstain" and v.underlying in allowed
      and v.confidence >= min_confidence
    )

def build_prompt(snapshots: list[dict], book: dict) -> str:
  """User message: market numbers and the account's current book."""
  return (
    "Market snapshot (prices in USD, volatility in % annualised):\n"
    + json.dumps(snapshots, indent=1, default=str)
    + "\n\nAccount book:\n"
    + json.dumps(book, indent=1, default=str)
    + "\n\nShould the account open one new trade now?"
  )

async def ask(
  client: anthropic.AsyncAnthropic,
  model: str,
  snapshots: list[dict],
  book: dict,
) -> BrainResult:
  """One structured call; anything unexpected becomes abstain."""
  try:
    response = await client.beta.messages.create(
      model=model,
      max_tokens=16000,
      system=SYSTEM_PROMPT,
      messages=[{"role": "user", "content": build_prompt(snapshots, book)}],
      output_config={
        "effort": "medium",
        "format": {"type": "json_schema", "schema": VIEW_SCHEMA},
      },
      betas=["server-side-fallback-2026-07-01"],
      fallbacks="default",
    )
  except anthropic.APIError as e:
    logger.warning("Claude call failed: {}", e)
    return BrainResult(None, f"api error: {type(e).__name__}: {e}", model)
  if response.stop_reason != "end_turn":
    return BrainResult(None, f"stop_reason {response.stop_reason}", response.model)
  # After a server-side fallback the last text block is the model that answered.
  text = next((b.text for b in reversed(response.content) if b.type == "text"), None)
  if text is None:
    return BrainResult(None, "no text block", response.model)
  try:
    view = View.model_validate_json(text)
  except ValidationError as e:
    return BrainResult(None, f"invalid answer: {e}", response.model)
  return BrainResult(view, None, response.model)
