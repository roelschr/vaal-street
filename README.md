# Vaal Street

A local trading desk for the Path of Exile Currency Exchange. It mirrors the exchange's public hourly history, turns it into prices you can trust, and answers the questions a trader actually has: what is this worth, what can I flip right now, what converts into something dearer, what did this item do at this point of past leagues, and would my strategy have made money.

Everything runs on your machine. The only network traffic is read-only requests for the public exchange digests, an item-name export and item icons. No account, no tokens, no in-game automation.

PoE 1 is the focus. The data layer also mirrors PoE 2, but the desk and models are built and tested against PoE 1.

## Quick start

Requires Python 3.10+.

```sh
pip install -e .
python -m vaalstreet serve
```

Open **http://localhost:3000**. On first launch the desk downloads the last two weeks of exchange hours (a couple of minutes) and then keeps itself current, one new hour every hour.

To compare leagues you need more history. Either press **Download full history** on the League start page, or run:

```sh
python -m vaalstreet sync --all      # every hour since July 2024, newest first, resumable (~1.5 GB)
```

## The desk

| Page | What it answers |
| --- | --- |
| **Desk** | The hour at a glance: Divine price, best flips, sets worth completing, movers, your watchlist, fired alerts. |
| **Markets** | Every traded item: fair value, 24h/7d change, what the Chaos market and the Divine market each pay, the gap between them, traded value, resting bids against resting offers. Click an item for its chart, flip plans, set recipes and the same league day in past leagues. |
| **Flips** | Ranked maker plans with the exact whole-number orders to post, margin, how often the levels are reached, expected units and profit per hour, capital needed and how crowded each side's queue is. |
| **Arbitrage** | Full sets (divination cards, shards) against what they turn into, and item-for-item side markets that disagree with the items' own prices. |
| **League start** | The Divine price by league day across leagues, a playbook of what rose and fell over the next 3/7/14/30 days from today's league day, and what is cheap or dear compared with the same day of past leagues. |
| **Backtest lab** | Replay a strategy over any league and compare it with simply holding Divines. |
| **Ledger** | Record your trades; positions are marked at fair value with realised and unrealised profit. |

**Hideout mode** (press `H`) opens a small always-on-top window with the top flips, your watchlist and alerts, so it can sit over the game. Prices can be shown in Chaos or Divine anywhere. `/` jumps to search.

## How the numbers are made

**The data.** For every market (pair of items), league and hour, the exchange publishes the volume traded on each side, the lowest and highest executed ratio, and the lowest and highest stock on offer. There is no order book and no individual trades. An item usually has two markets that matter: against Chaos and against Divine.

**Prices.** The hourly price of a market is its volume-weighted average (Chaos paid ÷ items received). Divine-market prices are converted at that same hour's Divine price. The two markets of an item are kept apart, because they are not the same price: the Divine market is typically several percent dearer. The published lowest/highest ratios are not used to set levels — single-unit rounding and joke trades (a Divine for one Chaos) pollute them in most hours.

**Fair value.** A local-level Kalman filter on log price per item. The item's own returns are split into real movement and bounce (negative lag-one autocovariance), hours that traded more value count for more, and prints far outside the forecast are down-weighted. Parameters are re-fitted on trailing data only, so the series is causal: the value shown for an hour used nothing from after it.

**Flip plans.** For each item and pair of markets, the desk looks at where each market's *hourly average* has traded relative to fair value over the last two days. A plan buys at the level the buy market averaged at or below in a given share of hours, and sells at the level the sell market averaged at or above in the same share — levels real volume went through. Three kinds: within the Chaos market, within the Divine market, and **Chaos → Divine**, which buys from players dumping loot for Chaos and sells to players paying Divines. The patience level with the highest expected profit is kept. Orders are given as whole-number ratios the exchange accepts.

**Set conversions.** Read from the game data: a divination card's stack size and reward, and shards that combine into their orb. A set is compared with its reward at fair value, and again "in a hurry" (buy the set in the dearer market, sell the reward in the cheaper one).

**League start.** Leagues are aligned on days since their first trade. The playbook takes every item on today's league day in each past league and measures its move over the chosen horizon, in Divine by default (Chaos loses value through a league, so Chaos returns flatter everything). Every row shows the per-league outcomes and how many leagues it rests on. A league whose start is not on disk is left out rather than misaligned.

**Backtests.** One league is replayed hour by hour. Orders posted at hour *t* meet hour *t+1*.

- A resting order fills at its limit only in an hour whose average price traded through it, and never at a better price.
- A market order pays the hour's average plus slippage (default 3%).
- A fill takes at most a set share of that hour's volume (default 10%).
- No leverage and no shorting. Sizing is on a fixed bankroll unless you ask to compound.
- `attention` is how many hours pass between your visits; orders stand in between.

### What the model cannot see

Read flip and backtest figures as a ceiling, not a forecast.

- **Gold.** Every order costs Gold, and the fee schedule is not published. It is not modelled. Gold is often the real limit on how much you can flip.
- **Queue position.** Orders at the same ratio fill in time order. The desk shows how many hours of volume already rest on each side, but cannot know where you would stand.
- **Hourly granularity.** A level is counted as reached only when the whole hour's average traded through it, which is conservative; the path inside the hour is unknown.
- **Small samples.** There have only been a handful of leagues since the exchange launched. League-start patterns are precedents, not odds.

Check the in-game order book before committing size.

## Command line

Everything the desk shows is also available from the shell.

```sh
python -m vaalstreet status                      # leagues and coverage
python -m vaalstreet price "Divine Orb"          # latest hourly prices, both markets
python -m vaalstreet flips --kind cross          # maker plans for the live league
python -m vaalstreet arb                         # sets against their rewards
python -m vaalstreet playbook --horizon 14       # what past leagues did next from today's league day
python -m vaalstreet backtest swing --league Mirage --league Allflame --attention 4
python -m vaalstreet sql "SELECT league, kind, hours FROM leagues ORDER BY first_hour"
```

`--league` defaults to the live challenge league. Run any command with `-h` for its options.

### Your own strategy

A strategy is a class with `step(ctx)`. It sees only what was published up to the current hour.

```python
# my_strategy.py
import numpy as np

class CheapVsDivine:
    """Buy liquid items in the Chaos market, offer them 8% higher in the Divine market."""

    def step(self, ctx):
        if ctx.t < 48:
            return
        traded = np.nansum(ctx.history("value", 48), axis=0)
        liquid = traded >= np.sort(traded)[-30]
        fair = ctx.fair
        units = np.where(liquid, 0.03 * ctx.bankroll / fair, 0.0)
        ctx.quote_buys("r", np.where(liquid, fair * 0.96, np.nan), units)       # "r" = Chaos market
        ctx.quote_sells("b", ctx.average_cost * 1.08, ctx.position)             # "b" = Divine market
```

```sh
python -m vaalstreet backtest my_strategy.py:CheapVsDivine --league Allflame
```

`ctx` offers `fair`, `z` (how surprising this hour's print was), `history(column, hours)`, `swing(leg, reach)`, `position`, `average_cost`, `cash`, `bankroll`, and `quote_buys` / `quote_sells` / `buy` / `sell`. Built-in strategies (`hold`, `swing`, `reversion`, `momentum`) are in `vaalstreet/backtest.py` and are short enough to read as examples.

### In a notebook

```python
from vaalstreet import connect
from vaalstreet.panel import Panel
from vaalstreet import pricing

con = connect()                                   # DuckDB views over the Parquet archive
con.sql("SELECT * FROM league_prices WHERE name = 'Divine Orb' AND league = 'Mirage' LIMIT 5")

panel = Panel.load(con, "Mirage", ("price", "value"))   # dense hour x item arrays
fair = pricing.fair_value(panel["price"], panel["value"])
```

Views: `markets` (as published), `legs` (each market from both sides), `prices` (per item, league and hour, with both markets), `leagues`, `items`, `league_prices` (prices plus names and league day).

## Files

All data lives under `./data` (override with `VAAL_DATA_DIR`).

```
data/raw/poe1/<YYYY-MM>/<hour>.json.gz     the exchange's digests, bytes as served
data/parquet/markets|prices/...            derived, rebuilt from raw at any time
data/items/poe1.json                       item names, categories, set recipes
data/icons/                                item icons, cached on first view
data/desk.sqlite                           your ledger, watchlist, alerts — the only file worth backing up
```

Other settings: `PORT` (default 3000) and `POE_USER_AGENT` (the identification string sent with exchange requests). The server binds to `127.0.0.1` and refuses requests from other sites.

```
vaalstreet/archive.py   download         vaalstreet/desk.py      market table, flips, arbitrage
vaalstreet/ingest.py    raw -> Parquet   vaalstreet/leagues.py   league-start analytics
vaalstreet/db.py        DuckDB views     vaalstreet/backtest.py  simulator and strategies
vaalstreet/panel.py     dense arrays     vaalstreet/ledger.py    trades, watchlist, alerts
vaalstreet/pricing.py   models           vaalstreet/server.py    local API; vaalstreet/web/ is the UI
```

Run the tests with `pip install -e .[dev] && pytest`. They build a small synthetic exchange archive with known structure and check every layer against it; none touch the network.

## License and credits

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.

The code is released under the [MIT License](LICENSE). That license does not cover the following, which stay with their owners:

- Path of Exile, its item names and item icons are the property of Grinding Gear Games. Icons are fetched from GGG's CDN at run time to label items in this free, non-commercial tool; none are distributed with it.
- Exchange history comes from GGG's documented [Currency Exchange API](https://www.pathofexile.com/developer/docs/reference).
- Item metadata comes from the community [RePoE](https://repoe-fork.github.io/) export of the game's data files.
- Geist and Geist Mono are distributed under the font licenses in `vaalstreet/web/fonts/`.
