"""Shared test configuration."""
import pytest

from src.bot import brain as brain_module
from src.bot.brain import BrainResult, View

def pytest_collection_modifyitems(
  config: pytest.Config, items: list[pytest.Item],
) -> None:
  """Skip tests marked `integration` unless `-m integration` is given."""
  if "integration" in (config.getoption("-m") or ""):
    return
  skip = pytest.mark.skip(
    reason="needs IB Gateway or external APIs; run with -m integration",
  )
  for item in items:
    if "integration" in item.keywords:
      item.add_marker(skip)

@pytest.fixture
def claude_says(monkeypatch: pytest.MonkeyPatch) -> list:
  """Make brain.ask return the given view; records each call."""
  calls: list = []
  def set_view(view: View | None, error: str | None = None) -> None:
    async def fake_ask(*args: object) -> BrainResult:
      calls.append(args)
      return BrainResult(view, error, "claude-sonnet-5-5")
    monkeypatch.setattr(brain_module, "ask", fake_ask)
  calls.append(set_view)
  return calls
