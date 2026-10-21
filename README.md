# IBKR options bot

A hands-off, **defined-risk** options bot for SPY and QQQ on Interactive Brokers. It's built on [omdv/ibkr-llm-assistant](https://github.com/omdv/ibkr-llm-assistant) (MIT).

**Principle: Claude proposes, code decides.** Claude only gives a view: bullish, bearish or abstain. Deterministic code picks strikes, sizes the trade, enforces a risk guard Claude can't override, and manages exits. It only trades long calls/puts and debit vertical spreads, so no trade can lose more than it cost and the bot never borrows.

See **[PLAN.md](PLAN.md)** for the full design and phases.

## Status

**Phases 1 and 2 done, still read-only.** Nothing in this repo can place orders yet. The guarded executor arrives in Phase 3.

- **Phase 2 risk guard (`src/guard/`):** every order must pass every gate in PLAN.md §5.3, or it gets no `Approval`, and the Phase 3 executor will only accept an `Approval`. Also in this phase:
  - a persistent kill switch, reset with `main.py --reset-kill-switch`
  - a `data/HALT` file that stops new entries while exits keep running
  - the paper-account check
  - about 100 adversarial tests that try to sneak bad orders past the guard

What works now:
- **Paper IB Gateway in Docker** (`gnzsnz/ib-gateway`, pinned version).
- **Price survey:** during US market hours it logs real SPY/QQQ option and debit-spread prices to SQLite every 30 minutes. It also shows how many candidate trades fit the per-trade cap. This is how the sizing in PLAN.md §4 gets settled with real numbers.
- **Read-only Claude CLI** (`--query`, `--cli`) for asking questions about the account. It has no tools that can place orders.

## Setup (Linux PC)

1. Install Docker with the Compose plugin.
2. Configure your IBKR paper login:
   ```bash
   cp .env.example .env
   ```
   Then fill in `TWS_USERID` and `TWS_PASSWORD` in `.env` with your **paper** login (Client Portal → Settings → Paper Trading Account). Paper accounts use your live account's market-data subscriptions (OPRA and US stocks).
3. Start the Gateway and the price survey:
   ```bash
   docker compose up -d
   docker compose logs -f bot
   ```
4. Stop the PC sleeping during US market hours (about 10:30pm–5am Perth time):
   ```bash
   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
   ```

Data is written to `./data`:
- `bot.sqlite` holds the survey data.
- `settings.toml` holds all the bot's numbers. Edit it at any time and changes are picked up on the next loop. An invalid edit is rejected and the previous settings are kept. See PLAN.md §5.7.

### Run without Docker (development)

```bash
uv sync
docker compose up -d ib-gateway                 # Gateway on 127.0.0.1:4002 (paper)
uv run python main.py --price-survey --once     # one survey now (frozen data outside market hours)
uv run python main.py --cli                     # needs ANTHROPIC_API_KEY
```

### Look at the survey results

```bash
sqlite3 data/bot.sqlite "SELECT run_at, symbol, structure, width, dte, debit_mid, max_loss_mid_usd, fits_cap
                         FROM price_survey ORDER BY id DESC LIMIT 20"
```

## Development

```bash
uv sync
uv run ruff check .
uv run pytest -q                 # unit tests
uv run pytest -q -m integration  # also needs a running Gateway / FMP key
```

CI runs ruff and pytest on every pull request.

## Credits

- [omdv/ibkr-llm-assistant](https://github.com/omdv/ibkr-llm-assistant) (MIT): the original MCP server and IBKR helpers.
- [gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker) (MIT): the Gateway image.
- [ib_async](https://github.com/ib-api-reloaded/ib_async) (BSD-2).
- Design references and code to be borrowed in later phases are listed in PLAN.md §3.
