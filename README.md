# Webull Paper Trading Bot

A rules-based equity trading bot with a hard risk layer. Runs in two modes:

| Mode | Broker | Data | Credentials | Progress visible in |
|---|---|---|---|---|
| `sim` | Local simulator | Synthetic random walk | None | `logs/` |
| `live-data-sim` | Local simulator | **Real Webull prices** | App key + secret | `logs/` |
| `webull-paper` | Webull OpenAPI | Real Webull prices | Key + secret + account ID | Webull, if reachable |

**`live-data-sim` is the recommended mode.** Real market data, locally
simulated fills, no brokerage account needed. Orders never leave your machine.

Note on Webull environments: `api.sandbox.webull.com` serves developer test
accounts (API-only, no UI, often unfunded). The $1M PaperTrade account inside
the Webull app is a separate system and may not be API-reachable yet. If
`webull-paper` returns ACCOUNT_ACCESS_DENIED for your PaperTrade account ID,
use `live-data-sim` instead -- you lose nothing that matters, since paper fills
are simulated either way.

## Quick start (no account needed)

```bash
cd webull-paper-bot
python3 run.py --mode sim --ticks 300 --strategy mean_reversion
```

Then, with credentials, the recommended mode:

```bash
python3 run.py --mode live-data-sim --cash 100 --symbols SPY,QQQ --poll 60
```

That runs 300 simulated minutes instantly and writes `logs/fills.csv` and
`logs/equity.csv`. Zero dependencies, zero network, zero risk.

## Requirements

Python 3.8-3.14 and the official SDK: `webull-openapi-python-sdk` (module
`webull.*`). If you previously installed the old `webull-python-sdk-*`
packages, uninstall them -- they are deprecated and 404 against the sandbox.

## Connecting your paper account

1. `cp .env.example .env`
2. Paste your paper app key, secret, and account ID into `.env`.
   **`.env` is gitignored — keep it that way, and don't paste keys into chats.**
3. `pip install -r requirements.txt`
4. `python3 run.py --mode webull-paper --ticks 1` (verifies the connection first)

Endpoints: paper keys authenticate ONLY against `api.sandbox.webull.com`;
production is `api.webull.com`. Set `WEBULL_API_ENDPOINT` in `.env`. Using paper
keys against production returns 401; using the wrong SDK returns 404.

On startup the bot calls `get_account_list()` and prints your valid account
IDs. If `WEBULL_ACCOUNT_ID` isn't in that list, copy one that is.

## Stopping it

Create a file named `HALT` in this directory:

```bash
touch HALT
```

The bot checks for it every tick and stops opening positions immediately.
`Ctrl+C` also works. Delete `HALT` to resume.

## Backtesting

```bash
py -3.11 fetch_history.py --symbols SPY,QQQ --timespan D --bars 1200
py -3.11 backtest.py --file data/history.csv --compare
```

`fetch_history.py` downloads bars (paging past Webull's 1200-per-request cap)
into a CSV. `backtest.py` replays them through the SAME strategy, risk manager,
and simulated broker the live bot uses -- so what you measure is your bot, not
a separate model of it.

`ReplayFeed` only ever exposes bars up to the current cursor, which makes
lookahead bias structurally impossible rather than merely unlikely.

The benchmark printed is buy-and-hold, because that is the real alternative.
Beating zero is not the test; beating "do nothing" is.

## Researching a strategy idea

```bash
py -3.11 analyze_overnight.py --file data/history.csv
```

Decomposes each day into the overnight move (close -> next open) and the
intraday move (open -> close), then reports mean, Sharpe, t-stat, and what
happens once trading costs are charged.

The workflow this encodes, which matters more than this particular idea:

1. State a hypothesis AND why it might be true structurally.
2. Measure the effect size and whether it is distinguishable from noise.
3. Charge realistic costs BEFORE deciding anything.
4. Only then consider building a strategy around it.

Most published anomalies survive step 2 and die at step 3.

## Architecture

```
run.py                  CLI, config, wiring
bot/engine.py           the loop: fetch -> evaluate -> risk-check -> execute
bot/risk.py             sizing, stops, daily loss halt, kill switch, PDT counter
bot/models.py           Bar, Signal, Order, Fill, Position
bot/journal.py          CSV audit trail
bot/brokers/base.py     broker interface
bot/brokers/simulated.py  local fills w/ slippage + commission
bot/brokers/webull.py     Webull OpenAPI adapter
bot/data/feeds.py       SyntheticFeed (offline) and WebullFeed (live bars)
bot/strategies/starters.py  SmaCrossover, MeanReversion
fetch_history.py        download historical bars to CSV
backtest.py             replay history through the live code paths
analyze_overnight.py    overnight vs intraday return research tool
diagnose.py             dump raw Webull API responses (for debugging)
```

Strategies emit *signals*, never orders. Only `risk.py` can create an order.
This is the single most important design decision in the project: it means a
buggy or over-eager strategy cannot bypass position limits or stops.

## Risk controls (all on by default)

- Max 25% of equity in any one position
- Max 3 open positions
- 3% stop loss, 6% take profit per position
- 5% daily loss halts trading for the day
- Rolling 5-day day-trade counter (PDT rule for accounts under $25k)
- `HALT` kill-switch file
- Long only — no shorting, no margin, no pyramiding

Protective exits (stop loss / take profit) deliberately override the PDT
counter. A blocked stop loss is worse than a PDT flag.

## Adding a strategy

Subclass `Strategy`, implement `lookback` and `evaluate(bars, holding)`, and add
it to `REGISTRY` in `bot/strategies/starters.py`. That's the whole contract.

## What this is and isn't

The two included strategies are baselines, not edges. Simple moving-average and
z-score rules are the most widely published signals in existence, which means
they are also the most heavily arbitraged. Expect them to lose slowly to
slippage. Their job is to give you a benchmark and to prove the plumbing works.

The synthetic feed is a random walk with a mean-reverting pull, which is *kind*
to mean-reversion strategies by construction. Profit in `sim` mode tells you the
code runs. It tells you nothing about whether a strategy works.

Nothing here is financial advice, and no configuration of it makes past results
predict future ones.
