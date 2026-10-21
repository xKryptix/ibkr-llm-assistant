"""Main IB interface combining all functionality."""
from .market_data import MarketDataClient
from .contracts import ContractClient
from .scanners import ScannerClient
from .positions import PositionClient

class IBInterface(
    MarketDataClient,
    ContractClient,
    ScannerClient,
    PositionClient,
):
    """Main IB interface combining all functionality."""
