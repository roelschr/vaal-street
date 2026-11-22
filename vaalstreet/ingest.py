"""Turn raw hourly digests into Parquet: `markets` as published, `prices` derived.

    <data>/parquet/markets/realm=<realm>/month=<YYYY-MM>/data.parquet
    <data>/parquet/prices/realm=<realm>/month=<YYYY-MM>/data.parquet

`markets` has one row per market (currency pair), league and hour, in the
orientation GGG publishes (`item_a|item_b`). A ratio is published as a pair of
whole numbers: `ratio_*_a` units of item_a per `ratio_*_b` units of item_b.

`prices` has one row per item, league and hour in which the item traded against
the realm's reference currency (Chaos Orb in PoE1, Exalted Orb in PoE2) or its
bridge (Divine Orb). Columns:

    price            VWAP across both legs in reference currency (one leg only when the
                     two disagree by more than 3x, which means one printed a joke trade)
    volume, value    units traded, and their value in reference currency
    bridge           value of one bridge currency (Divine) that hour
    r_*              the direct leg against the reference currency
    b_*              the leg against the bridge, converted at that hour's bridge rate
      *_price        leg VWAP            *_lo / *_hi       lowest/highest executed ratio
      *_volume       item units traded   *_stock_lo/_hi    item units on offer (asks)
                                         *_bid_lo/_hi      quote on offer for the item (bids), in reference currency

Ingest is incremental: only hours that are on disk but not yet in a month's
Parquet are parsed, and only months that changed are rewritten.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import duckdb

from vaalstreet.paths import REALMS, markets_dir, prices_dir, raw_dir

_CURRENCY = "Metadata/Items/Currency/"
CHAOS = _CURRENCY + "CurrencyRerollRare"
DIVINE = _CURRENCY + "CurrencyModValues"
EXALTED = _CURRENCY + "CurrencyAddModToRare"
# realm -> (reference currency, bridge currency)
ANCHORS = {"poe1": (CHAOS, DIVINE), "poe2": (EXALTED, DIVINE)}

# Bump when the definition of `prices` changes: existing derived files are then rebuilt from `markets`.
PRICES_VERSION = "2"

_MAP = "MAP(VARCHAR, UBIGINT)"
_DIGEST_COLUMNS = (
    "{markets: 'STRUCT(league VARCHAR, market_id VARCHAR, volume_traded " + _MAP + ", lowest_stock " + _MAP
    + ", highest_stock " + _MAP + ", lowest_ratio " + _MAP + ", highest_ratio " + _MAP + ")[]'}"
)

# DuckDB's JSON reader reserves about two buffers of `maximum_object_size` per thread up front, and
# DuckDB starts one thread per core. With 256 MiB buffers, 16 cores ask for 8 GiB before reading a
# byte, and ingest fails with an out-of-memory error. The largest real PoE 1 digest is about 1.7 MB
# uncompressed (measured on Oct-Nov 2026), so 32 MiB leaves ample headroom; the thread cap bounds the
# reservation on big machines (4 x 2 x 32 MiB = 256 MiB) and costs little, as ingest is mostly I/O.
_MAX_DIGEST_BYTES = 32 * 1024 * 1024
INGEST_THREADS = min(4, os.cpu_count() or 1)

_PARSE = """
WITH raw AS (
    SELECT CAST(regexp_extract(filename, '(\\d+)\\.json\\.gz$', 1) AS BIGINT) AS hour_id, unnest(markets) AS m
    FROM read_json($files, columns = {columns}, filename = true, maximum_object_size = {max_object})
), split AS (
    SELECT hour_id, m, split_part(m.market_id, '|', 1) AS a, split_part(m.market_id, '|', 2) AS b FROM raw
)
SELECT to_timestamp(hour_id) AS hour, m.league AS league, a AS item_a, b AS item_b,
       coalesce(m.volume_traded[a], 0) AS vol_a, coalesce(m.volume_traded[b], 0) AS vol_b,
       coalesce(m.lowest_stock[a], 0) AS stock_lo_a, coalesce(m.highest_stock[a], 0) AS stock_hi_a,
       coalesce(m.lowest_stock[b], 0) AS stock_lo_b, coalesce(m.highest_stock[b], 0) AS stock_hi_b,
       coalesce(m.lowest_ratio[a], 0) AS ratio_lo_a, coalesce(m.lowest_ratio[b], 0) AS ratio_lo_b,
       coalesce(m.highest_ratio[a], 0) AS ratio_hi_a, coalesce(m.highest_ratio[b], 0) AS ratio_hi_b
FROM split
""".format(columns=_DIGEST_COLUMNS, max_object=_MAX_DIGEST_BYTES)

# Every market seen from both sides: the price of `item` in `quote`.
LEGS_SQL = """
SELECT hour, league, item_a AS item, item_b AS quote,
       vol_a AS volume, vol_b AS volume_quote,
       vol_b / nullif(vol_a, 0) AS price,
       least(ratio_hi_b / nullif(ratio_hi_a, 0), ratio_lo_b / nullif(ratio_lo_a, 0)) AS price_lo,
       greatest(ratio_hi_b / nullif(ratio_hi_a, 0), ratio_lo_b / nullif(ratio_lo_a, 0)) AS price_hi,
       stock_lo_a AS stock_lo, stock_hi_a AS stock_hi,
       stock_lo_b AS quote_stock_lo, stock_hi_b AS quote_stock_hi
FROM {markets}
UNION ALL
SELECT hour, league, item_b, item_a,
       vol_b, vol_a,
       vol_a / nullif(vol_b, 0),
       least(ratio_lo_a / nullif(ratio_lo_b, 0), ratio_hi_a / nullif(ratio_hi_b, 0)),
       greatest(ratio_lo_a / nullif(ratio_lo_b, 0), ratio_hi_a / nullif(ratio_hi_b, 0)),
       stock_lo_b, stock_hi_b, stock_lo_a, stock_hi_a
FROM {markets}
"""

_PRICES = """
WITH legs AS ({legs}),
bridge AS (
    SELECT hour, league, price AS rate FROM legs
    WHERE item = $bridge AND quote = $ref AND volume > 0 AND volume_quote > 0
),
r AS (SELECT * FROM legs WHERE quote = $ref AND item <> $ref),
b AS (
    SELECT l.*, g.rate FROM legs l JOIN bridge g USING (hour, league)
    WHERE l.quote = $bridge AND l.item NOT IN ($ref, $bridge)
),
joined AS (
    SELECT coalesce(r.hour, b.hour) AS hour, coalesce(r.league, b.league) AS league,
           coalesce(r.item, b.item) AS item, g.rate AS bridge,
           coalesce(r.volume, 0) AS r_volume, coalesce(r.volume_quote, 0) AS r_value,
           CASE WHEN r.volume > 0 AND r.volume_quote > 0 THEN r.price END AS r_price,
           CASE WHEN r.volume > 0 THEN r.price_lo END AS r_lo, CASE WHEN r.volume > 0 THEN r.price_hi END AS r_hi,
           r.stock_lo AS r_stock_lo, r.stock_hi AS r_stock_hi,
           r.quote_stock_lo AS r_bid_lo, r.quote_stock_hi AS r_bid_hi,
           coalesce(b.volume, 0) AS b_volume, coalesce(b.volume_quote * b.rate, 0) AS b_value,
           CASE WHEN b.volume > 0 AND b.volume_quote > 0 THEN b.price * b.rate END AS b_price,
           CASE WHEN b.volume > 0 THEN b.price_lo * b.rate END AS b_lo,
           CASE WHEN b.volume > 0 THEN b.price_hi * b.rate END AS b_hi,
           b.stock_lo AS b_stock_lo, b.stock_hi AS b_stock_hi,
           b.quote_stock_lo * b.rate AS b_bid_lo, b.quote_stock_hi * b.rate AS b_bid_hi
    FROM r FULL JOIN b ON r.hour = b.hour AND r.league = b.league AND r.item = b.item
    LEFT JOIN bridge g ON g.hour = coalesce(r.hour, b.hour) AND g.league = coalesce(r.league, b.league)
)
SELECT hour, league, item,
       CAST(CASE WHEN use_r AND use_b THEN (r_value + b_value) / (r_volume + b_volume)
                 WHEN use_r THEN r_price ELSE b_price END AS DOUBLE) AS price,
       CAST(CASE WHEN use_r THEN r_volume ELSE 0 END + CASE WHEN use_b THEN b_volume ELSE 0 END AS UBIGINT) AS volume,
       CAST(CASE WHEN use_r THEN r_value ELSE 0 END + CASE WHEN use_b THEN b_value ELSE 0 END AS DOUBLE) AS value,
       bridge,
       r_price, r_lo, r_hi, CAST(r_volume AS UBIGINT) AS r_volume,
       r_stock_lo, r_stock_hi, CAST(r_bid_lo AS DOUBLE) AS r_bid_lo, CAST(r_bid_hi AS DOUBLE) AS r_bid_hi,
       b_price, b_lo, b_hi, CAST(b_volume AS UBIGINT) AS b_volume,
       b_stock_lo, b_stock_hi, CAST(b_bid_lo AS DOUBLE) AS b_bid_lo, CAST(b_bid_hi AS DOUBLE) AS b_bid_hi
FROM (
    -- The two markets of an item never honestly disagree by a factor of three. When they do, one of
    -- them printed a joke trade; the headline price then follows the market that moved more units.
    SELECT *,
           r_price IS NOT NULL AND (b_price IS NULL OR greatest(r_price, b_price) <= 3 * least(r_price, b_price)
                                    OR r_volume >= b_volume) AS use_r,
           b_price IS NOT NULL AND (r_price IS NULL OR greatest(r_price, b_price) <= 3 * least(r_price, b_price)
                                    OR b_volume > r_volume) AS use_b
    FROM joined
    WHERE r_price IS NOT NULL OR b_price IS NOT NULL
)
ORDER BY league, item, hour
"""


def _write(con: duckdb.DuckDBPyConnection, query: str, target: Path, params: dict | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("data.parquet.part")
    con.execute(f"COPY ({query}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)", params or {})
    os.replace(tmp, target)


def _ingested_hours(con: duckdb.DuckDBPyConnection, target: Path) -> set[int]:
    """Hours already folded into a month's Parquet. The sidecar also remembers hours that
    published no markets at all (league downtime), which leave no rows to find."""
    if not target.exists():
        return set()
    sidecar = target.with_name("hours.json")
    if sidecar.exists():
        return set(json.loads(sidecar.read_text()))
    rows = con.execute(f"SELECT DISTINCT epoch(hour)::BIGINT FROM read_parquet('{target.as_posix()}')").fetchall()
    return {r[0] for r in rows}


def ingest_month(con: duckdb.DuckDBPyConnection, root: Path, realm: str, month_dir: Path) -> int:
    """Add a month's not-yet-ingested hours to Parquet. Returns the number of hours added."""
    month = month_dir.name
    markets = markets_dir(root) / f"realm={realm}" / f"month={month}" / "data.parquet"
    prices = prices_dir(root) / f"realm={realm}" / f"month={month}" / "data.parquet"
    raw = {int(p.name.split(".")[0]): p for p in month_dir.glob("*.json.gz")} if month_dir.is_dir() else {}
    done = _ingested_hours(con, markets)
    new = sorted(set(raw) - done)
    if new:
        con.execute("CREATE OR REPLACE TEMP TABLE fresh AS " + _PARSE, {"files": [raw[h].as_posix() for h in new]})
        source = "SELECT * FROM fresh"
        if markets.exists():
            source += f" UNION ALL BY NAME SELECT * FROM read_parquet('{markets.as_posix()}')"
        _write(con, f"SELECT * FROM ({source}) ORDER BY league, item_a, item_b, hour", markets)
        con.execute("DROP TABLE fresh")
        markets.with_name("hours.json").write_text(json.dumps(sorted(done | set(new))))
    if markets.exists() and (new or not prices.exists()):
        ref, bridge = ANCHORS[realm]
        legs = LEGS_SQL.format(markets=f"read_parquet('{markets.as_posix()}')")
        _write(con, _PRICES.format(legs=legs), prices, {"ref": ref, "bridge": bridge})
    return len(new)


def ingest(root: Path, log: Callable[[str], None] | None = print) -> dict[str, int]:
    """Bring Parquet up to date with the raw archive. Returns hours added per realm."""
    added: dict[str, int] = {}
    stamp = prices_dir(root) / "VERSION"
    if prices_dir(root).exists() and (not stamp.exists() or stamp.read_text().strip() != PRICES_VERSION):
        for stale in prices_dir(root).glob("realm=*/month=*/data.parquet"):
            stale.unlink()
    con = duckdb.connect(config={"threads": INGEST_THREADS})
    con.execute("SET TimeZone = 'UTC'")
    try:
        for realm in REALMS:
            realm_dir = raw_dir(root) / realm
            # Months known from raw digests, plus months restored as Parquet only (their prices may need deriving).
            months = {p.name for p in realm_dir.iterdir() if p.is_dir()} if realm_dir.is_dir() else set()
            months |= {p.name.removeprefix("month=") for p in (markets_dir(root) / f"realm={realm}").glob("month=*")}
            if not months:
                continue
            total = 0
            for month_dir in (realm_dir / month for month in sorted(months)):
                count = ingest_month(con, root, realm, month_dir)
                total += count
                if count and log:
                    log(f"[{realm}] {month_dir.name}: +{count} hours")
            added[realm] = total
        if added:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(PRICES_VERSION)
    finally:
        con.close()
    return added
