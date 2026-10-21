"""Base IB client connection handling."""
import datetime as dt
from loguru import logger
from ib_async import IB
from src.utilities import Settings, setup_logging

setup_logging()

class IBClient:
  """Base IB client connection handling. No public methods."""

  def __init__(self, *, readonly: bool = True) -> None:
    """Initialize IB interface.

    Args:
      readonly: Connect with a read-only API session. Nothing in this repo
        places orders until the guarded executor (PLAN.md Phase 3) exists.

    """
    self.config = Settings()
    self.ib = IB()
    self.readonly = readonly

  async def _connect(self) -> None:
    """Create and connect IB client."""
    if self.ib.isConnected():
      return

    host = self.config.ib_gateway_host
    port = self.config.ib_gateway_port

    try:
      logger.debug("Connecting to IB on {}:{}", host, port)
      await self.ib.connectAsync(
        host=host,
        port=port,
        clientId=self._client_id(),
        timeout=20,
        readonly=self.readonly,
      )
      self.ib.RequestTimeout = 20
      logger.debug("Connected to IB on {}:{}", host, port)
    except Exception as e:
      logger.error("Error connecting to IB: {}", e)
      raise

  def _client_id(self) -> int:
    """Return the configured client ID, or a time-based one so tools can coexist."""
    if self.config.ib_client_id is not None:
      return self.config.ib_client_id
    return int(dt.datetime.now(dt.UTC).strftime("%H%M%S"))

  def __del__(self) -> None:
    """Disconnect from IB."""
    try:
      if self.ib and self.ib.isConnected():
        self.ib.disconnect()
    except Exception:
      logger.debug("Error disconnecting from IB")
