"""Environment settings: connections, credentials and file locations.

Strategy and risk numbers live in `data/settings.toml` (see
`src/utilities/strategy_settings.py`), not here.
"""
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
  """Configuration read from environment variables (or `.env`)."""

  # Global settings
  verbose: bool = False
  log_file: str = "logs/app.log"
  data_dir: str = "data"
  mcp_server_script: str = "./mcp_server.py"
  timeout_seconds: int = 10
  server_timezone: str = "America/New_York"

  # IB Gateway. 4002 is the paper port published by docker-compose on the host;
  # inside the compose network the bot uses ib-gateway:4004 instead.
  ib_gateway_host: str = "127.0.0.1"
  ib_gateway_port: int = 4002
  ib_client_id: int | None = None

  # Dry run: the bot does everything except send orders. Turn off with DRY_RUN=false.
  dry_run: bool = True

  # Live trading needs BOTH: ALLOW_LIVE=true and LIVE_CONFIRM=<the live account id>.
  allow_live: bool = False
  live_confirm: str | None = None

  # Phone notifications via ntfy (install the ntfy app, subscribe to the topic).
  ntfy_topic: str | None = None
  ntfy_server: str = "https://ntfy.sh"
  # Dead-man's switch (e.g. a healthchecks.io ping URL), pinged during US hours.
  heartbeat_url: str | None = None

  # Optional FMP quotes/economic calendar API (MCP CLI only)
  quotes_api_key: str | None = None

  # Claude (MCP CLI now, trading brain from Phase 3)
  anthropic_api_key: str | None = None
  chat_model: str = "claude-sonnet-5-5"
  chat_model_max_tokens: int = 4096

  model_config = {"env_file": ".env", "extra": "ignore"}
