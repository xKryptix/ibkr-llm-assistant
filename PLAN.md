# PLAN: hands-off, defined-risk options bot on IBKR

**Status:** draft for Sebastian's approval. No bot code is written until this plan is approved.
**Base:** this fork of `omdv/ibkr-llm-assistant` (MIT).
**Principle:** *Claude proposes, code decides.* Claude only gives a view: direction, structure and confidence, or abstain. Deterministic code picks strikes, sizes the trade, checks it against the risk guard, places it and manages the exit. On any error, the bot does nothing.

---

## 1. Decisions so far

| Topic | Decision |
|---|---|
| Broker | Interactive Brokers (IBKR) |
| What it trades | US-listed options on **SPY and QQQ** only, 2–10 DTE at entry |
| Structures | **Long calls/puts and debit verticals only.** No credit spreads, condors or butterflies, no stock, no naked short legs. |
| Capital | **A$500**, converted to USD (see §4) |
| Mode | IBKR **paper** account first, then a small live account only after the go-live criteria in §9 are met |
| Where it runs | **Sebastian's Linux PC** for now. Plain Docker Compose, so it can move to TrueNAS later without changes. |
| Interaction | **Fully automated.** No per-trade approval. Phone notifications only. Sebastian's only ongoing jobs: keep the PC on, and reset the kill switch if it trips. |
| Earlier `optbot/` code | Not available, so everything is written fresh using the designs in §3. |

## 2. What we keep, change and drop from the base repo

| Area | Today | Plan |
|---|---|---|
| `src/ib_helper/` (client, contracts, market_data, positions) | Works on ib_async | **Keep.** It's the IBKR data layer. Fix the `clientId` type (it's a string, it should be an int). Make `_is_market_open` use IBKR `liquidHours` (holidays and half days included). |
| `src/ib_helper/trading.py` | Every order waits for Telegram approval. Any order type, any structure. 10 s fill timeout. | **Rewrite.** Orders come only from the executor (§5.4) after the risk guard passes. Limit orders only. Telegram approval removed. |
| MCP server + `mcp_client.py` | Claude can call `trade_*` tools freely | **Claude never gets order tools.** The MCP server stays for an interactive "ask about my account" CLI, **read-only** (the trade tools are removed). The trading loop calls Claude directly with one forced, structured tool (§5.1). |
| Web UI + APScheduler + **Postgres** | Scheduled free-text prompts | **Drop the web UI and Postgres.** APScheduler runs in-process. The journal moves to **SQLite**. A small read-only status page can come later (Phase 6). |
| `settings.py` | FMP, Telegram and IBC fields required. Model `claude-3-5-sonnet-20241022`. | Make them optional. Default model `claude-sonnet-5-5`, with `claude-opus-5-5` selectable. New risk/strategy settings (§5.3). |
| `docker-compose.yaml` | `extrange/ibkr` image, **`IBC_TradingMode: "live"` by default**, Postgres | Switch to `gnzsnz/ib-gateway-docker` in **paper mode by default**. Drop Postgres. Bot container with `restart: unless-stopped`. See §7. |
| FMP economic calendar | Required API key | Optional context for Claude. The bot works without it. |

## 3. Code borrowed from other repos

Licences were checked on 2026-10-21. Copied files keep their original notices. Repos without a licence are used as **design references only** (ideas rewritten, no code copied).

| Source | Licence | What we use |
|---|---|---|
| `pedrobraiti/agentic-trading-mcp` | MIT | Guard-wrapping-the-broker pattern (`GuardedBroker`), intent-before-send order journal, duplicate-order check, crash reconcile. Ported from Client Portal to ib_async, with an options max-loss check added. |
| `ricardogs26/hackathon-alpaca` (optionwright) | MIT | Gate catalogue with thresholds in one table, sizing that can only shrink, LLM schema limited to direction, confidence and a reason (abstain on error or timeout), exit-replay test harness. |
| `Hellek1/ib-mcp` | BSD-3 | Chain and Greeks fetching on ib_async that avoids stale reused `Ticker` objects. |
| `ferdousbhai/tasty-agent` | MIT | Spread limit-pricing logic: tick-aligned, inside bid/ask, refuse if impossible. Preview then refuse on warnings, which on IBKR means `whatIfOrder`. No blind retries. |
| `vishnu-0011/conviction-spread-agent`, `himanshu2394i/Alpaca` | MIT | Fail-closed rules: never leg into a spread, halt on stale data, `HALT` kill-file, reconcile before closing. |
| `CarloLepelaars/blackscholes` or `py_vollib` | MIT | Fallback Greeks and implied volatility when IBKR sends none. |
| `csingley/ibflex` | MIT | Parse IBKR Flex reports for nightly reconciliation and the tax export. |
| `chrisudf/ibkr-tax-report` | MIT | Starting point for the ATO CGT export (FIFO, RBA FX). |
| `matthewchung74/alpaca-gatekeeper` | **none** | Design reference only: pure-function gates that all run, the exit priority order, and pricing exits at the unfavourable side of the quote. |

Not used: `lumibot` (GPL), `optopsy` (AGPL; may be used later as a separate offline backtester, never imported), `kelvingao/ibkr-mcp` (proprietary), `extrange/ibkr-docker` (no licence).

## 4. Money and sizing

- **Budget in USD:** when the bot starts, it converts A$500 at IBKR's `AUD.USD` rate and saves that USD figure as `capital_usd`. It is not recalculated every day, so currency moves don't change the budget. *(A$500 is roughly US$320–340 at recent rates; the real figure comes from IBKR.)*
- **Fund the account in USD:** convert the A$500 to USD inside IBKR before trading. If you don't, IBKR lends USD for each purchase and charges interest. The guard refuses any order that would make USD cash negative.
- **Max loss per trade:** the cap is `risk_per_trade_pct × capital_usd`. At the default 15% that's about **US$50**.
  - Long option: the premium paid plus commissions.
  - Debit vertical: the net debit × 100 × qty, plus commissions. The width of the spread caps this from above.
- **Budget across the book:** the sum of max loss over open positions plus the new order must be ≤ `capital_usd` minus realised losses.
- **What this means:** in practice the bot will mostly trade **1-lot, $1–$2-wide debit verticals**. Single long options near the money usually cost more than US$50 at 2–10 DTE. Commissions are about US$0.65 per contract leg each way, so roughly US$2.60 per 1-lot vertical round trip. That is a meaningful share of a ~US$50 risk.
  - **Phase 1 deliverable:** a read-only *price survey* script that logs real paper-account quotes for candidate spreads for a week, so we can settle the cap with real numbers rather than guesses.

## 5. How a trade happens

```
every 30 min in US regular hours
  └─ snapshot (code): SPY/QQQ price, recent bars, IV / IV rank, open positions, P&L, calendar
      └─ brain (Claude, one forced tool call): {action: bullish|bearish|abstain, structure: long_option|debit_vertical, confidence, rationale}
          └─ builder (code): picks expiry and strikes by delta and width, prices the combo at mid, computes max loss
              └─ risk guard (code): all gates must pass, otherwise log and stop
                  └─ executor (code): whatIfOrder preview → limit order → reprice ladder → cancel if unfilled
                      └─ journal + phone notification
every 15 min (and on start-up): exit manager (code only, no Claude)
```

### 5.1 Brain (Claude)
- Claude is called with **one tool and `tool_choice` forced to it**. The schema is the object above. Anything else, a timeout, or an API error counts as **abstain**.
- Claude does not see account credentials and cannot place, modify or cancel orders.
- **Default is abstain.** The prompt says so, and code also requires `confidence ≥ min_confidence` (default 0.6).
- Rationale is stored in the journal so decisions can be reviewed.

### 5.2 Builder (code)
- Expiry: the nearest expiry with DTE in `[min_dte, max_dte]` (default 2–10). Prefer ~5–7 DTE.
- Long option: strike nearest a target delta (default 0.40).
- Debit vertical: long leg near 0.45–0.55 delta, short leg `width` further out of the money on the **same right and expiry**. Width is tried as $1, then $2, keeping the first that fits the cap.
- Quantity: the most contracts that fit the cap, at most `max_contracts`. **Sizing can only shrink**: if anything is uncertain, it uses fewer contracts or none.
- Limit price: mid, rounded to tick, and must sit inside the combo's natural bid/ask.

### 5.3 Risk guard (code; Claude cannot reach or override it)
Every gate is a pure function that returns pass or fail plus a reason. **All gates run and every result is journalled.** Any failure means no order.

| Gate | Rule |
|---|---|
| Account | Account ID starts with `DU` (paper). Live needs `ALLOW_LIVE=true` **and** `LIVE_CONFIRM=<account id>`. |
| Underlying | SPY or QQQ only |
| Security types | Options only (`OPT`, or `BAG` made only of `OPT` legs). No `STK`, no futures. |
| Structure | One long option, or exactly two legs: same underlying, same expiry, same right, ratio 1:1, one BUY and one SELL, with the SELL leg further OTM. **Any unpaired short leg is rejected.** |
| Debit only | The net price must be a debit, with 0 < net < width. |
| Order type | `LMT` only, DAY time-in-force. No `MKT`. |
| DTE | Within `[min_dte, max_dte]` at entry |
| Liquidity | Each leg's bid/ask spread ≤ 10% of mid (or ≤ $0.05). Quotes fresher than 30 s. Bid > 0. |
| Max loss per trade | ≤ `risk_per_trade_pct × capital_usd` (default 15%) |
| Book budget | Open max loss plus the new order ≤ `capital_usd` minus realised losses |
| Max open positions | Default 3 |
| Max new trades per day | Default 2 |
| Day trades | If IBKR reports a day-trade limit, same-day closes are allowed only for stop, assignment or ≤1 DTE, with 1 day trade kept in reserve (§8) |
| Cash | USD cash after the order ≥ 0. The `whatIfOrder` preview has no warnings. |
| Duplicates | No identical open order or same-direction position on the same expiry |
| Market hours | Inside regular hours from `liquidHours`. No entries in the first 15 or last 30 minutes. |
| Kill switch | Not tripped (see below) and no `HALT` file present |
| Closing orders | Can only reduce a position the bot holds, with the same legs reversed and the same or smaller quantity |

**Kill switch (persistent):** if equity falls below `(1 − max_drawdown_pct) × capital_usd` (default 30%), or after `max_consecutive_losses` (default 4), the bot closes everything and writes the trip to SQLite. **It stays off until Sebastian resets it by hand** (`main.py --reset-kill-switch`).

### 5.4 Executor (code)
1. Preview with `whatIfOrder`. Any warning, or a margin/commission result outside expectations, means refuse.
2. Place a limit order at mid. If unfilled, move one tick toward the natural price every 20 s, at most 3 times, never beyond the guard's price cap. Then cancel.
3. **Never place spread legs separately.** It is always one BAG order.
4. If the order's status is unknown (for example, a disconnect), there is **no retry**. The bot reconciles with IBKR's positions and orders first.
5. The intent is journalled *before* sending and resolved after. On start-up, any unresolved intent is reconciled.

### 5.5 Exit manager (code only)
It runs every 15 minutes in regular hours, and once at start-up. Rules, first match wins:

1. **Stock position appears** (early assignment or exercise): close the stock and the remaining leg at once, then notify.
2. **DTE ≤ 1:** close. The bot never holds into expiry.
3. **Stop loss:** the position is worth ≤ 50% of entry cost. Close.
4. **Profit target:** the position is worth ≥ 150% of entry cost for a long option, or ≥ 80% of the spread's width for a vertical. Close.
5. **Time stop:** held more than `max_hold_days` (default 4 trading days). Close.

Exits are valued at the unfavourable side of each quote (bid for longs, ask for shorts), not mid. Closing orders go through the same guard (the "closing orders" gate) and the same executor.

*All thresholds are proposed defaults, to be tuned using paper results.*

## 6. Journal, alerts and tax

- **SQLite** (WAL mode) with these tables:
  - `decisions`: every Claude call, including abstains and rationale
  - `gate_results`
  - `orders`: intent, placement, fills
  - `positions`
  - `equity`: a daily snapshot
  - `kill_switch`
- **Notifications** via **ntfy** (no account needed; Telegram optional). It sends:
  - entries and exits
  - guard rejections, summarised
  - errors
  - kill-switch trips
  - a daily summary after the close (around 7am Perth time)
- **Heartbeat:** each loop pings a free dead-man's-switch service (e.g. healthchecks.io), so Sebastian's phone gets an alert if **the PC or the bot stops**. The bot can't send that alert itself if it's down.
- **Tax CSV:** each closed trade gets open and close dates, legs, USD proceeds and cost, commissions, the **RBA AUD/USD rate** for each date, and AUD amounts. These are CGT events for the ATO.
  - Cross-check with Flex reports via `ibflex`.
  - Whether frequent short-dated trading is treated as capital or revenue (trader) is a question for a tax agent. The export provides the raw data either way.

## 7. Running on a Linux PC

- **Docker Compose** with two services:
  - `ib-gateway` (`gnzsnz/ib-gateway-docker`, paper mode, ports bound to `127.0.0.1` only)
  - `bot`
  - Both use `restart: unless-stopped`.
  - Data (SQLite, logs) lives in `./data`.
  - No TrueNAS-specific settings, so the same file works there later.
- **The PC must stay awake** from about 10:15pm to 5:15am Perth time on weeknights (9:15pm–4:15am during US daylight saving).
  - Disable suspend: `systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
  - Plan OS updates and reboots outside those hours.
  - Docker starts at boot.
- **If the PC is off or the bot is down:**
  - Losses are still capped: the most any position can lose is what it cost.
  - What's lost is timely exits, and the ≤1 DTE close.
  - Mitigations:
    - Entries need DTE ≥ 2.
    - The exit manager runs first on start-up.
    - From the session after entry, the bot keeps a **resting GTC take-profit limit order** on the combo, held by IBKR, so profits can be taken while the PC is down. The exit manager cancels it, and confirms the cancel, before sending any other close for that position.
    - The heartbeat alert tells Sebastian to start the PC again.
- **IBC (Gateway auto-login) was archived in September 2026.** The gnzsnz image still bundles it, and it works for paper today. If a future Gateway version breaks it, the planned replacement is `ibg-controller` (MIT). Pin the Gateway image version and don't auto-update it.
- **Secrets** live in `.env`, which is git-ignored: IBKR paper login, Anthropic key, ntfy topic.

## 8. Pattern-day-trader (PDT) rule

Researched on 2026-10-21. **It most likely doesn't limit this account, but the bot is built to be safe either way.**

- **FINRA has replaced PDT.** The SEC approved the change on 14 April 2026 ([FINRA Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)), and it took effect on **4 June 2026**.
  - The 4-day-trades-in-5-days rule and the US$25k minimum are gone. They're replaced by *intraday margin* rules: an account that repeatedly fails to cover intraday margin deficits can be restricted for 90 days.
  - Brokers may keep applying the old PDT rules until **20 October 2027**.
- **IBKR has said** that non-US residents whose accounts are carried by IBKR Australia are not subject to PDT ([IBKR on X, Feb 2025](https://x.com/IBKR/status/1889449440223584524)). That's a social-media post, not a formal policy page.
- **Not confirmed:**
  - whether a same-day vertical counts as one day trade or two at IBKR
  - whether an Australian *cash* account can trade verticals at all
  - So the plan stays with a margin account (needed for spreads). The guard still requires USD cash ≥ 0, so the bot never borrows, and an intraday margin deficit shouldn't arise from debit trades.

**Built into the bot regardless:**
- The guard reads IBKR's `DayTradesRemaining` account value. If IBKR reports a limit, the bot only closes a position on the day it opened it for a stop, assignment or ≤1 DTE. It keeps 1 day trade in reserve for those.
- The resting take-profit order (§7) is placed from the **next** session onward, so it can't create a same-day round trip.
- Max 2 new trades a day. Holding periods are measured in days (§5.5), not minutes.
- **Sebastian:** once the account is open, check the "Day Trades Left" field in Client Portal, or ask IBKR Australia support.

## 9. Testing and go-live

1. **Unit tests that attack the guard.** Each must be rejected:
   - naked short, short-only BAG, ratio 1:2
   - different expiries or rights, a credit "vertical", `MKT`
   - `STK` leg, wrong underlying, over-budget, live account without both flags
   - a close larger than the position held
2. **Exit replay tests:** recorded price paths, including an assignment case, run through `exit manager` → expected actions.
3. **Dry-run mode:** the full loop against paper Gateway data, where the executor logs and doesn't send.
4. **Paper trading for 4–8 weeks.**
5. **Go-live criteria** *(proposed; Sebastian to confirm the numbers before paper trading starts):*
   - at least **40 closed trades**
   - max drawdown **≤ 25%** of `capital_usd`
   - net P&L after commissions **> 0**, with the paper-fill haircut applied: subtract US$0.02 per leg per fill, because IBKR paper fills combos too generously
   - **zero** guard bypasses
   - **zero** unreconciled positions
   - **zero** unhandled crashes in the final 2 weeks

## 10. Phases and deliverables

| Phase | Deliverables |
|---|---|
| **1. Modernise** | Settings made optional, new model default, Postgres and web UI removed, SQLite added. Compose switched to gnzsnz in paper mode. Read-only MCP CLI. **Price-survey script.** Replace the inherited `docker-publish.yml`, which pushes to the original author's registry, with a PR workflow that runs ruff and pytest. Tests and ruff pass. |
| **2. Risk guard** | `guard/` package with every §5.3 gate, the kill switch, the `HALT` file and the paper/live check. Adversarial unit tests. |
| **3. Strategy and execution** | Snapshot, brain (forced tool), builder, executor (preview, reprice ladder, reconcile), exit manager with assignment handling, scheduler. Dry-run mode. |
| **4. Journal and alerts** | SQLite journal, ntfy alerts, heartbeat, daily summary, tax CSV with RBA rates. |
| **5. Paper trading** | 4–8 weeks with a weekly review of the journal. Tune the thresholds. |
| **6. Optional** | Read-only status page (Tailscale), move to TrueNAS, SPY Swing Lab signal hook. |

Each phase is its own PR, with tests.

## 11. Open items for Sebastian

1. **Approve this plan,** or say what to change.
2. **Confirm the default numbers:**
   - 15% risk per trade
   - 3 open positions
   - 2 trades a day
   - 30% drawdown kill switch
   - the exit thresholds in §5.5
   - the go-live criteria in §9
3. **Phase 0 (your side):**
   - IBKR account with options and spread permissions
   - paper account login
   - OPRA and US stock market data subscriptions (these apply to paper too)
   - Anthropic API key
   - an ntfy topic on your phone
