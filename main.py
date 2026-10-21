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

  args = parser.parse_args()
  settings = Settings()

  if args.price_survey:
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
