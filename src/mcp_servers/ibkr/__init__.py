"""IBKR MCP server: read-only tools (no order placement)."""
from fastmcp import FastMCP

from src.utilities import setup_logging
from src.ib_helper import IBInterface

setup_logging()
ibkr = FastMCP("ibkr")

# Initialize shared interface
ib_interface = IBInterface()

# Import all tools
from .positions import *
from .scanner import *
from .contracts import *
