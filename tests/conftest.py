"""Shared test configuration."""
import pytest

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
