# IBKR options bot

A hands-off, **defined-risk** options bot for SPY and QQQ on Interactive Brokers. It's built on [omdv/ibkr-llm-assistant](https://github.com/omdv/ibkr-llm-assistant) (MIT).

**Principle: Claude proposes, code decides.** Claude only gives a view: bullish, bearish or abstain. Deterministic code picks strikes, sizes the trade, enforces a risk guard Claude can't override, and manages exits. It only trades long calls/puts and debit vertical spreads, so no trade can lose more than it cost and the bot never borrows.

See **[PLAN.md](PLAN.md)** for the full design and phases.

## Status

**Phases 1–3 done. The bot trades your paper account, in dry run by default.**

How it works:

- **Every 30 minutes during US market hours (entries).** Claude gets SPY/QQQ price action and implied volatility and answers bullish, bearish or abstain.
  - If its view is confident enough, code picks a 3–10 DTE long option or debit spread.
  - Code sizes it to the per-trade cap.
  - The risk guard (`src/guard/`) checks it.
  - The executor works a limit order: mid price first, then up to 3 one-cent steps, then it cancels.
- **Every 15 minutes during US market hours (exits).** These run on code only, with no Claude involved.
  - Stock left by an early assignment is closed first.
  - Then, in this order: close at 2 DTE, stop loss, profit target, time stop.
  - From the day after entry, a GTC take-profit order rests at IBKR. It's cancelled before any other close.
- **Kill switch.** It trips on a 30% drawdown or 4 losses in a row, then closes everything and stops new entries until `python main.py --reset-kill-switch`.
- **HALT file.** A `data/HALT` file stops new entries; exits keep running.
- **Journal.** Everything goes into `data/bot.sqlite`: Claude's decisions, every guard check, orders and positions.

## Setup (Linux PC)

1. Install Docker with the Compose plugin.
2. Configure the bot:
   ```bash
   cp .env.example .env
   ```
   Then fill in `.env`:
   - **IBKR paper login:** Client Portal → Settings → Paper Trading Account.
   - **`ANTHROPIC_API_KEY`.**
   - **Market data:** the paper account uses your live account's subscriptions (OPRA and US stocks).
3. **Convert some AUD to USD in the paper account.** Use Client Portal's currency conversion, or a forex order for USD.AUD in TWS. The bot only trades with USD cash and never borrows, so it won't open anything until there is USD cash.
4. Start the Gateway and the bot:
   ```bash
   docker compose up -d
   docker compose logs -f bot
   ```
   It starts in **dry run**. It asks Claude, plans trades and runs every risk check, but sends no orders. Watch a day of logs, then set `DRY_RUN=false` in `.env` and run `docker compose up -d` again.
5. Stop the PC sleeping during US market hours (about 10:30pm–5am Perth time):
   ```bash
   sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
   ```

**Settings** are in `data/settings.toml`, created on first run. Edit it at any time: changes apply on the next loop, and an invalid edit is rejected while the previous settings are kept. See PLAN.md §5.7.

### Commands

```bash
python main.py --trade                 # the bot (dry run unless DRY_RUN=false)
python main.py --reset-kill-switch     # re-enable entries after a kill-switch trip
python main.py --price-survey [--once] # log real option prices (PLAN.md §4)
python main.py --cli                   # ask Claude read-only questions about the account
```

To stop new entries without stopping exits, run `touch data/HALT`. To resume entries, run `rm data/HALT`.

### What happened today?

```bash
sqlite3 data/bot.sqlite "SELECT decided_at, underlying, direction, confidence, outcome FROM decisions ORDER BY id DESC LIMIT 10"
sqlite3 data/bot.sqlite "SELECT id, symbol, structure, long_strike, short_strike, entry_price, status, realized_pnl_usd FROM positions"
```

## Development

```bash
uv sync
uv run ruff check .
uv run pytest -q                 # unit tests, including the engine against a fake broker
uv run pytest -q -m integration  # also needs a running Gateway / FMP key
```

CI runs ruff and pytest on every pull request.

## Credits

- [omdv/ibkr-llm-assistant](https://github.com/omdv/ibkr-llm-assistant) (MIT): the original MCP server and IBKR helpers.
- [gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker) (MIT): the Gateway image.
- [ib_async](https://github.com/ib-api-reloaded/ib_async) (BSD-2).
- Design references are listed in PLAN.md §3.
