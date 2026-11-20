# Vaal Street

A single-player, local-first trading terminal for Path of Exile 1 and 2. The browser is the desk; a loopback Node server owns the market database, calculations, feeds, and alert rules. No account or cloud service is required to start.

## Research layer (Python)

`vaalstreet/` mirrors GGG's full public Currency Exchange history and exposes it as DuckDB views for backtesting and league-start studies. It is independent of the Node terminal below.

```sh
pip install -e .[dev]
python -m vaalstreet archive   # every published hour since July 2024, both games; resumable
python -m vaalstreet items     # readable item names
python -m vaalstreet ingest    # raw hours -> Parquet
python -m vaalstreet price "Divine Orb" --league Covens
```

Views: `markets` (as published), `legs`, `prices` (Chaos in PoE 1, Exalted in PoE 2, with hourly low/high band and volume), `leagues`, and `league_prices` (adds day-of-league). Use `python -m vaalstreet sql "..."` or `from vaalstreet import connect`. Run `pytest` for the tests.

## Terminal (Node)

### Run

Requires **Node.js 22.13+** with built-in `node:sqlite`. There are no npm dependencies, package installation, or build step.

```sh
npm start
```

Open **http://localhost:3000**. On Windows, `npm.cmd start` or `node server.js` works even when PowerShell blocks npm's script shim. `npm run dev` restarts the server after backend changes.

The first launch creates `data/terminal.sqlite` and opens a personal **PoE 1 Standard** ledger with no transactions, balances, or holdings recorded. Select your game and league before recording entries. The terminal remembers your last personal workspace. Public exchange sync brings in market prices; it does not populate your ledger.

To try a funded example, choose **Data sources → Open demo**. This loads separate synthetic replay workspaces with example trades and balances. **Return to personal desk** switches back; visiting a demo does not change your startup workspace.

### Start using your own data

1. In **Data sources**, choose **Sync both games**. Open a league marked **Exchange feed**. Enable **Auto-sync** to continue following both realms while the server runs. Without a feed, **New workspace** starts an empty personal ledger instead.
2. In **Portfolio → Record entry**, deposit your available Chaos and Gold. Add existing items as **Opening inventory** with their known unit cost basis. Record purchases and sales as you execute them.
3. In **Arbitrage scanner → Record listing**, enter the whole-unit exchange ratio, confirmed lot count, and actual Gold per lot. Or import a complete snapshot. Hourly settlements do **not** create executable inventory.
4. Click a profitable loop for its leg-by-leg worksheet. In **Signal center**, set a price or net-return target. Press **H** for Hideout mode; its pop-out control opens document picture-in-picture where supported or a separate compact window. **Ctrl/Cmd K** searches pages and assets.

## What the desk computes

| Desk | Behavior |
| --- | --- |
| Market explorer | Candles or lines, traded volume, four time ranges, crosshairs, searchable and pinnable instruments, cumulative bid/ask depth after Gold costs. |
| Market integrity | Rejects offline, unconfirmed, expired, future-dated, unreferenced, and out-of-band listings. A recent direct settled pair takes precedence over cross-valued marks. Exclusions remain inspectable. |
| League lifecycle | Matches the current league day against earlier leagues in the same game. A 7-day scenario applies the median matched forward change to the current price. Requires known league starts and sufficient history; demo and real observations never mix. |
| Arbitrage | Searches simple 2–6-leg cycles from Chaos, sizes them against actual ledger cash, Gold, and per-leg depth, floors each leg to whole lots, and ranks positive net returns. Reports residual units, gross/net proceeds, return on capital, and net Chaos per million Gold. |
| Crafting | Compares settling against a reroll with success/fallback prices, material cost, sale probability, and time. Can populate inputs from market marks and imported build-specific sell-through cohorts. Whole-batch evaluation supports card sets, vendor transformations, and bulk spreads. |
| Replay | Mean reversion, momentum, buy-and-hold, and multi-asset relative strength. Configurable signal lag, participation, size impact, slippage, decay, and Gold burn. Includes equity, realized P&L, turnover, drawdown, Sharpe/Sortino equivalents, capital lockup, partial fills, and execution details. |
| Portfolio | Separate cash and Gold balances; FIFO or weighted-average cost with exact residual allocation; realized, unrealized, and session P&L; marked allocation by asset class. Idle positions receive a progressive markdown and a whole-unit hedge suggestion when an appreciating, affordable candidate exists. |
| Signals | Durable threshold rules rearm on exit. Server-sent events deliver changes to open terminals. Visual alerts work locally; desktop notifications require browser permission. An optional webhook works with the server running and the browser closed. |

Personal workspaces have separate balances for every **game + league + source**. A stash observation is a valuation, not a fabricated purchase or sale. In **Portfolio → Stash**, unpriced assets remain visible and are omitted from the priced subtotal.

## Data sources and persistence

- **Public hourly exchange:** Uses GGG's documented `web.poecdn.com/api/currency-exchange` endpoints for PC and `poe2`. A new installation starts from the most recently completed hour. A persisted cursor catches up after downtime. Hitting the tip waits for the next hourly boundary; failures retry with backoff and respect `Retry-After`.
- **Prices:** Each available direct Chaos mark is an hourly volume ratio. For assets without a direct market, valuation uses up to three same-hour settled links, preferring shorter paths and deeper bridges. Stock-only rows are archived but never create prices or depth. For exchange data, a displayed candle aggregates hourly **marks**, not individual trade ticks; chart volume refers to the chosen valuation leg.
- **Reading the chart:** Expand the source label below **Price terminal** to see the workspace's source, stored UTC window, and number of hourly observations in the selected range. A fresh sync can contain just one hour. Short histories use smaller candle intervals; missing intervals remain gaps. Market history is independent of your ledger and can be loaded using the backfill command below.
- **Listings:** Manual observations and normalized file imports work for both games. An optional PoE 1 public-stash connector reads changed and withdrawn tabs, parses supported currency price notes, and replaces their prior offers transactionally. Public observations are delayed and initially **unconfirmed**. Review availability, ratio, depth, and Gold in the scanner before using them. The public adapter recognizes catalogued currencies/cards; normalized imports also support new assets and league mechanics.
- **Inventory:** Import a full normalized stash snapshot, or configure the official PoE 1 account-stash reader for selected tabs. The public API does not provide equivalent PoE 2 account stash access.
- **Durability:** SQLite WAL, `synchronous=FULL`, foreign keys, unique observation IDs, and atomic batch commits. Exchange data, compressed raw digest, and cursor commit together. Invalid imports roll back the entire batch. File imports are content-deduplicated, and ledger submissions accept idempotency IDs.

**Backups:** Use **Data sources → Download database backup** for a consistent SQLite snapshot, including committed WAL data. To restore, stop the app, place the backup in a **new directory** named as `terminal.sqlite`, and launch with `VAAL_DATA_DIR` pointing to that directory. Pins, display preferences, and selected workspace are browser-local.

### Optional configuration

Set environment variables before starting the server. Tokens stay server-side.

| Variable | Purpose |
| --- | --- |
| `PORT`, `VAAL_DATA_DIR` | Port (default `3000`) and SQLite directory (default `./data`). Server binds to `127.0.0.1` only. |
| `VAAL_IMPORT_DIR` | Existing folder watched for normalized `.json` files. Write a temporary file, then rename to `.json` when complete. Processing errors appear in Data sources. |
| `POE_USER_AGENT` | Your application's identification/contact string for outbound GGG requests. |
| `POE_LEAGUES_TOKEN` | Optional `service:leagues` bearer token. Discovers main/event leagues, including quiet ones, and their start dates during feed sync. Without it, leagues are discovered from the hourly exchange. |
| `POE_PUBLIC_STASH_TOKEN` | Optional `service:psapi` bearer token. Enables **Public listing stream** controls for PoE 1. |
| `POE_STASH_TOKEN`, `POE_STASH_TABS` | `account:stashes` bearer token and comma-separated selected tab IDs, optionally `tab/subtab`. Enables manual stash reads in a personal PoE 1 workspace. |
| `POE_STASH_AUTO_WORLD` | Optional workspace ID for automatic selected-tab valuation every 15 minutes. |
| `VAAL_WEBHOOK_URL` | Optional webhook receiving `{content,event}` on non-demo rule crossings and newly opened routes. A failed webhook is logged; the local signal remains available. |

GGG access is **GET-only**. The desk does not send whispers, orders, input events, or in-game actions. OAuth credentials, if used, must have the applicable access; the app does not acquire or refresh tokens for you. See the [official API reference](https://www.pathofexile.com/developer/docs/reference).

## Import and historical replay

**Data sources → Download example** creates an importable practice file with recent timestamps, bars, quotes, a demand cohort, and a stash. See [FORMAT.md](FORMAT.md) for the wire format. Workspace IDs are available at `/api/worlds`.

Replay any imported date range from the CLI. `--from` and `--to` accept ISO-8601 or Unix seconds. Output is JSON, one result per league.

```sh
npm run replay -- --world demo-1 --asset divine --days 90 --strategy momentum
npm run replay -- --world demo-1 --asset divine --module examples/rotation.js
npm run replay -- --worlds demo-1,demo-2 --assets divine,exalted --module examples/equal-weight.js
```

`--asset` supplies a single-instrument callback with completed prices, the bankroll, and `previousClose(n)`; return an invested/cash target. `--assets` supplies all observed instruments and accepts **arbitrary whole-unit target allocations** under a shared bankroll. Each league gets its own cash, Gold, and strategy-module instance. Decisions execute only on later available opens. Local modules are trusted JavaScript explicitly selected on the CLI, never evaluated from browser input or imported data.

To backfill published historical exchange hours:

```sh
npm run backfill -- --game 1 --from 2026-10-01T00:00:00Z --to 2026-10-07T23:00:00Z --limit 24
```

This is paced at 12 seconds between requests. The same range resumes from its saved checkpoint without rewinding the live cursor. Historical coverage depends on what GGG retains; there is no bundled multi-year archive.

## Units, execution assumptions, and measured limits

- Money uses integer **micro-Chaos** (`1 c = 1,000,000`), units and Gold use `BigInt`, and ratios are compared by integer cross multiplication. Basis allocation retains rounding residuals until the final unit is sold. Floating-point numbers are used for display coordinates and timing, not monetary settlement.
- Gold is a separate constrained resource. The scanner uses the **observed fee per lot**. Its Chaos opportunity cost is configurable under workspace settings (default `12 c / 1M Gold`). Ledger entries preserve the valuation in effect when recorded.
- A route's net return is returned Chaos less starting Chaos and the valued Gold burn. Intermediate dust is reported but not counted as realized profit. The solver tests sizes around the maximum feasible whole-lot fill; it does not assert a globally optimal allocation across competing routes. Each worksheet is independently funded, and multiple displayed routes may compete for the same liquidity.
- The graph has a 30,000-edge expansion budget and explicitly reports truncated searches. Screening is explainable evidence filtering, not proof of seller intent or a measured near-zero false-positive rate.
- Bar replay models execution instead of reconstructing an unavailable historical order book. Participation limits use the preceding observed volume. Drawdown is marked to market; open positions are not force-sold at the end. Sharpe and Sortino are unannualized per-observation ratios with zero risk-free rate. Missing variance or zero Gold burn yields an undefined ratio rather than infinity.

Run `npm test` and `npm run bench`. The tests cover allocation conservation, stack floors, cash/Gold/depth limits, no-lookahead fills, multi-asset funding, cursor and import rollback, stash withdrawal, alert rearming, backup restoration, and recovery from an interrupted SQLite writer.

Representative local run on Windows x64, Node 26.13.1:

| Workload | p95 |
| --- | ---: |
| Sample scanner including SQLite reads and quote screening | 0.65 ms |
| 128 assets + Chaos, 629 quotes, four legs, 49 profitable cycles, complete search | 2.40 ms |
| 90-day hourly replay, including 2,160 SQLite bars | 4.97 ms |
| Parse, normalize, compress, and durably commit a 4,000-pair digest | 96.55 ms |

These are measured local workloads, not publication-to-client latency guarantees. Network delays, upstream publication cadence, rate limits, storage failures, and denser graphs have different bounds.

## Project map

`server.js` serves the local API, assets, and SSE. `src/store.js` owns storage and portfolio queries; `src/engine.js` and `src/basket.js` implement financial math and simulation. `src/feed.js` and `src/listings.js` contain read-only adapters. `public/` contains the offline-capable UI, bundled fonts, and icons. `examples/`, `scripts/`, and `test/` contain strategies, import/replay utilities, and verification.

## License and credits

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.

The code is released under the [MIT License](LICENSE). That license does not cover the following, which stay with their owners:

- Path of Exile, its item names, and the item icons in `public/items/` are the property of Grinding Gear Games. They are included only to label items in this free, non-commercial tool and are not licensed for reuse.
- Geist and Geist Mono are distributed under the font licenses in `public/fonts/`.
