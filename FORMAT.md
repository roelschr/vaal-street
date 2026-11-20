# Normalized observations

Send UTF-8 JSON to `POST /api/import` (`Content-Type: application/json`), use **Data sources → Choose a snapshot file**, or drop a completed `.json` file in `VAAL_IMPORT_DIR`. Maximum file size: 20 MiB. Partition long histories into batches.

Download `/example.json` from the running desk for a complete, immediately usable fixture. The following describes its fields:

| Field | Meaning |
| --- | --- |
| `game` | `1` or `2`. |
| `league` | Exact league name; games and leagues are partitioned. |
| `target` | Omit for an import workspace. Set `"exchange"` to enrich the matching official exchange workspace with listing observations, metadata, or stash inventory. |
| `asOf` | Snapshot timestamp (Unix seconds or ISO-8601); defaults to receipt time. |
| `leagueStart` | Optional league-start timestamp for lifecycle comparison. |
| `assets` | Definitions such as `{"id":"thread-breach","name":"Breach Thread","symbol":"BTHR","kind":"Commodity"}`. IDs are lowercase slugs up to 80 characters; kinds: `Currency`, `Commodity`, `Craft`, `Card`. `chaos` always exists. Use `/api/desk?w=…` to find existing IDs. |
| `bars` | Hourly Chaos prices: `{"asset":"divine","t":1795183200,"open":"180","high":"188.1","low":"179.4","close":"186.4","volume":"1200"}`. The timestamp must be UTC-hour aligned. Key: workspace, asset, timestamp. |
| `pairs` | Independent settled reference: `{"a":"chaos","b":"divine","t":1795183200,"volumeA":"9320","volumeB":"50"}`. Later observations replace the latest reference; raw history remains archived. |
| `quotes` | **Full replacement book** for this workspace. Example: `{"id":"listing-17","from":"chaos","to":"divine","pay":"187","get":"1","lots":"6","gold":"75","seen":1795183200,"online":true,"confirmed":true}`. This means *pay 187 Chaos to receive 1 Divine*, up to 6 lots, spending 75 Gold per executed lot. Both flags must be explicitly supplied. |
| `stash` | **Full inventory snapshot**, e.g. `[{"asset":"chaos","qty":"1000"},{"asset":"divine","qty":"3"}]`. Supply each asset once with its aggregate units. Never mutates trade lots or ledger balances. |
| `demand` | Observed build-specific sale cohort, e.g. `[{"asset":"astrolabe","build":"League starter cohort","listed":"120","sold":"89","minutes":15,"seen":1795183200}]`. Supply confirmed sales, not delistings. The advisor can use `sold/listed` for the same sale window. Samples older than seven days are excluded. |

Include one or more of `bars`, `pairs`, `quotes`, `stash`, or `demand`. Omitting `quotes` or `stash` preserves that snapshot; sending `[]` explicitly clears it. Quotes and stash snapshots reject out-of-order replacements. Bars can be backfilled in any order. A duplicate file returns `duplicate:true`.

Send prices as **decimal strings with at most six fractional digits**, and large integer quantities as strings. Binary floating-point values should not be used to construct ratios. All observations must be at or before the snapshot, allowing one minute of clock skew. Stale imported listings still appear in liquidity review.

## Local API quick reference

Most routes take `?w=<workspace-id>`. Without `w`, the API uses the personal PoE 1 Standard workspace. GET requests are read-only. POST requests below change only the local desk or initiate documented read-only upstream requests.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/worlds`, `/api/desk`, `/api/data` | Workspace catalog, selected desk, and source health. |
| `GET /api/chart?a=divine&range=7D&w=…` | Aggregated prices and volume, lifecycle scenario, and observed bid/ask depth. Ranges: `24H`, `7D`, `30D`, `90D`. |
| `POST /api/workspace` | `{game,league,start?}` creates or selects a personal workspace. |
| `POST /api/demo` | `{game:1}` or `{game:2}` initializes the separate sample datasets on demand and returns the selected demo workspace ID. Repeat requests reuse the same datasets. |
| `POST /api/trade` | `{id?,side,asset,qty,price?,gold?,session?,note?,t?}`. Sides: `deposit` (`chaos` or `gold`), `opening`, `buy`, `sell`. Include a stable `id` for idempotent retries. |
| `POST /api/quote`, `/api/scan` | Record one observed lot without replacing the book; run a funded scan with optional `capital`, `gold`, `legs`. |
| `POST /api/backtest` | `{asset,strategy,days,capital,gold,…}`. Strategies: `reversion`, `momentum`, `hold`, `rotation`. Date bounds and execution parameters are supported. |
| `POST /api/craft`, `/api/transform` | Compare settlement EV or a whole-batch conversion; probabilities are basis points (`10,000 = 100%`). |
| `POST /api/alerts` | `{kind:"price",asset,op:"above",threshold}` or `{kind:"route",op:"above",threshold}`. Manage with `{action:"toggle" \| "delete",id}`. |
| `POST /api/sync`, `/api/listings-sync`, `/api/stash-sync` | Read available market digests, optional public listings, or configured account tabs. `sync` and `listings-sync` support `pull:true` and an `automatic` boolean. |
| `POST /api/settings` | Cost-basis `method`, `tax` per million Gold, `maxLegs`, `staleHours`, listing `ttl` (seconds), and reference `tolerance` (bps). |
| `GET /api/events`, `/api/backup` | Server-sent updates; consistent downloadable SQLite database. |
