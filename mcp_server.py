"""MCP Server for IBKR data (read-only; Claude cannot place orders through it)."""

from loguru import logger
from fastmcp import FastMCP

from src.mcp_servers import ibkr, fmp, calendar
from src.utilities import Settings

main_mcp = FastMCP(
  name="main_mcp",
  log_level="WARNING",
)

main_mcp.mount("ibkr", ibkr)
if Settings().quotes_api_key:
  main_mcp.mount("fmp", fmp)
main_mcp.mount("calendar", calendar)

if __name__ == "__main__":
  try:
    main_mcp.run(transport="stdio")
  except Exception as e:
    logger.error("MCP server error: {}", str(e))
    raise
