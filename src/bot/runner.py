"""Build the engine from settings and run it."""
from pathlib import Path

import anthropic
from loguru import logger

from src.bot.broker import IBBroker
from src.bot.engine import Engine
from src.bot.notify import Notifier
from src.bot.store import Store
from src.guard import KillSwitch
from src.storage import connect
from src.utilities import Settings
from src.utilities.strategy_settings import StrategySettingsStore

async def run_bot(settings: Settings) -> None:
  """Run the trading loop until stopped."""
  data_dir = Path(settings.data_dir)
  conn = connect(data_dir / "bot.sqlite")
  claude = None
  if settings.anthropic_api_key:
    claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key,
                                      timeout=120.0, max_retries=2)
  else:
    logger.warning("ANTHROPIC_API_KEY not set: exits run, but no new entries")
  engine = Engine(
    broker=IBBroker(dry_run=settings.dry_run),
    store=Store(conn),
    kill_switch=KillSwitch(conn),
    settings_store=StrategySettingsStore(data_dir / "settings.toml"),
    settings=settings,
    claude=claude,
    data_dir=data_dir,
    notifier=Notifier(topic=settings.ntfy_topic, server=settings.ntfy_server,
                      heartbeat_url=settings.heartbeat_url),
  )
  if not settings.ntfy_topic:
    logger.warning("NTFY_TOPIC not set: notifications only go to the log")
  await engine.run()
