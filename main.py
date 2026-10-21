"""Entry point for the application."""
import asyncio
import argparse
from rich.console import Console
from src.utilities import Settings
from src.application import RichApp

async def main() -> None:
  """Launch the application."""
  parser = argparse.ArgumentParser(description="IBKR options bot")
  parser.add_argument(
    "--query",
    type=str,
    help="Ask Claude a single read-only question about the account and exit")
  parser.add_argument(
    "--cli",
    action="store_true",
    help="Chat with Claude in the CLI (read-only tools)")
  parser.add_argument(
    "--price-survey",
    action="store_true",
    help="Log real option/spread prices to SQLite every survey interval")
  parser.add_argument(
    "--once",
    action="store_true",
    help="With --price-survey: run one survey now and exit")
  parser.add_argument(
    "--trade",
    action="store_true",
    help="Run the trading bot (dry run unless DRY_RUN=false)")
  parser.add_argument(
    "--tax-export",
    type=int,
    metavar="FY",
    help="Write AUD tax CSVs for a financial year (2027 = Jul 2026 to Jun 2027)")
  parser.add_argument(
    "--reset-kill-switch",
    action="store_true",
    help="Re-enable new entries after the kill switch tripped")

  args = parser.parse_args()
  settings = Settings()

  if args.reset_kill_switch:
    from pathlib import Path
    from src.guard import KillSwitch
    from src.storage import connect
    switch = KillSwitch(connect(Path(settings.data_dir) / "bot.sqlite"))
    reason = switch.reason
    if switch.reset():
      Console().print(f"Kill switch reset (it had tripped because: {reason})")
    else:
      Console().print("Kill switch was not tripped; nothing to reset")
  elif args.tax_export:
    from pathlib import Path
    from src.bot.store import Store
    from src.bot.tax import write_tax_export
    from src.storage import connect
    data_dir = Path(settings.data_dir)
    store = Store(connect(data_dir / "bot.sqlite"))
    for path in write_tax_export(store, data_dir, args.tax_export):
      Console().print(f"Wrote {path}")
  elif args.trade:
    from src.bot import run_bot
    await run_bot(settings)
  elif args.price_survey:
    from src.survey import run_price_survey
    await run_price_survey(settings, once=args.once)
  elif args.query or args.cli:
    from mcp_client import MCPClient
    mcp_client = MCPClient(settings)
    if args.query:
      await mcp_client.connect_to_server()
      result = await mcp_client.process_query(args.query)
      Console().print(result)
    else:
      app = RichApp(mcp_client)
      await app.run()
    await mcp_client.cleanup()
  else:
    parser.print_help()

if __name__ == "__main__":
  asyncio.run(main())
