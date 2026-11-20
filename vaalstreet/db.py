"""DuckDB views over the Parquet archive.

    markets        one row per market, league and hour, as published
    legs           every market seen from both sides: the price of `item` in `quote`
    anchor_rates   value of each anchor currency in the realm's reference currency
    prices         one reference-currency price per item, league and hour
    leagues        first/last hour and flags per league
    league_prices  prices plus hours and days since the league's first trade

Reference currency: Chaos Orb in PoE1, Exalted Orb in PoE2. An item with no direct
market against the reference is valued through the anchor market that carried the
most value that hour, using that same hour's anchor rate.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

from vaalstreet import items
from vaalstreet.paths import REALMS, data_dir, markets_dir

_CURRENCY = "Metadata/Items/Currency/"
# (realm, item, rank): rank 0 is the reference currency, the rest are bridges.
ANCHORS = [
    ("poe1", _CURRENCY + "CurrencyRerollRare", 0),  # Chaos Orb
    ("poe1", _CURRENCY + "CurrencyModValues", 1),  # Divine Orb
    ("poe2", _CURRENCY + "CurrencyAddModToRare", 0),  # Exalted Orb
    ("poe2", _CURRENCY + "CurrencyModValues", 1),  # Divine Orb
    ("poe2", _CURRENCY + "CurrencyRerollRare", 2),  # Chaos Orb
]
PERMANENT_LEAGUES = ("Standard", "Hardcore", "Ruthless", "Hardcore Ruthless")

_VIEWS = r"""
CREATE VIEW markets AS
SELECT * FROM read_parquet('{glob}', hive_partitioning = true);

CREATE VIEW legs AS
SELECT hour, realm, league, item_a AS item, item_b AS quote,
       vol_a AS volume, vol_b AS volume_quote,
       vol_b / nullif(vol_a, 0) AS price,
       ratio_hi_b / nullif(ratio_hi_a, 0) AS price_lo,
       ratio_lo_b / nullif(ratio_lo_a, 0) AS price_hi,
       stock_lo_a AS stock_lo, stock_hi_a AS stock_hi,
       stock_lo_b AS quote_stock_lo, stock_hi_b AS quote_stock_hi
FROM markets
UNION ALL
SELECT hour, realm, league, item_b, item_a,
       vol_b, vol_a,
       vol_a / nullif(vol_b, 0),
       ratio_lo_a / nullif(ratio_lo_b, 0),
       ratio_hi_a / nullif(ratio_hi_b, 0),
       stock_lo_b, stock_hi_b, stock_lo_a, stock_hi_a
FROM markets;

CREATE VIEW anchor_rates AS
SELECT l.hour, l.realm, l.league, l.item AS anchor, l.price AS rate
FROM legs l
JOIN anchors ref ON ref.realm = l.realm AND ref.rank = 0 AND ref.item = l.quote
JOIN anchors bridge ON bridge.realm = l.realm AND bridge.rank > 0 AND bridge.item = l.item
WHERE l.volume > 0 AND l.volume_quote > 0;

CREATE VIEW prices AS
WITH valued AS (
    SELECT l.*, CASE WHEN l.quote = ref.item THEN 1.0 ELSE r.rate END AS quote_rate
    FROM legs l
    JOIN anchors q ON q.realm = l.realm AND q.item = l.quote
    JOIN anchors ref ON ref.realm = l.realm AND ref.rank = 0
    LEFT JOIN anchor_rates r
      ON r.hour = l.hour AND r.realm = l.realm AND r.league = l.league AND r.anchor = l.quote
    WHERE l.volume > 0 AND l.volume_quote > 0 AND l.item <> ref.item
), priced AS (
    SELECT hour, realm, league, item,
           arg_max(quote, volume_quote * quote_rate) AS via,
           arg_max(price * quote_rate, volume_quote * quote_rate) AS price,
           arg_max(price_lo * quote_rate, volume_quote * quote_rate) AS price_lo,
           arg_max(price_hi * quote_rate, volume_quote * quote_rate) AS price_hi,
           sum(volume) AS volume,
           sum(volume_quote * quote_rate) AS volume_ref,
           arg_max(stock_lo, volume_quote * quote_rate) AS stock_lo,
           arg_max(stock_hi, volume_quote * quote_rate) AS stock_hi
    FROM valued
    WHERE quote_rate IS NOT NULL
    GROUP BY ALL
)
SELECT p.hour, p.realm, p.league, p.item,
       coalesce(n.name, regexp_replace(
           regexp_replace(split_part(p.item, '/', -1), '^(Currency|DivinationCard)', ''),
           '([a-z0-9])([A-Z])', '\1 \2', 'g')) AS name,
       p.* EXCLUDE (hour, realm, league, item)
FROM priced p
LEFT JOIN item_names n ON n.realm = p.realm AND n.item = p.item;

CREATE VIEW leagues AS
SELECT realm, league,
       min(hour) AS first_hour, max(hour) AS last_hour, count(DISTINCT hour) AS hours,
       regexp_matches(league, '\(PL\d+\)$') AS is_private,
       league IN {permanent} AS is_permanent
FROM markets
GROUP BY ALL;

CREATE VIEW league_prices AS
SELECT p.*,
       date_diff('hour', l.first_hour, p.hour) AS league_hour,
       date_diff('hour', l.first_hour, p.hour) // 24 AS league_day
FROM prices p
JOIN leagues l USING (realm, league);
"""


def connect(root: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with the views above. Run `vaal ingest` first."""
    root = root or data_dir()
    parquet_root = markets_dir(root)
    if not any(parquet_root.glob("realm=*/month=*/data.parquet")):
        raise FileNotFoundError(f"No Parquet data under {parquet_root}. Run `python -m vaalstreet ingest` first.")

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE anchors (realm VARCHAR, item VARCHAR, rank INTEGER)")
    con.executemany("INSERT INTO anchors VALUES (?, ?, ?)", ANCHORS)

    names = [
        {"realm": realm, "item": item_id, "name": entry["name"], "class": entry.get("class")}
        for realm in REALMS
        for item_id, entry in items.load(root, realm).items()
    ]
    name_schema = pa.schema([("realm", pa.string()), ("item", pa.string()), ("name", pa.string()), ("class", pa.string())])
    item_names_arrow = pa.Table.from_pylist(names, schema=name_schema)  # noqa: F841 (read by DuckDB)
    con.execute("CREATE TABLE item_names AS SELECT * FROM item_names_arrow")

    glob = (parquet_root / "realm=*" / "month=*" / "data.parquet").as_posix()
    con.execute(_VIEWS.format(glob=glob, permanent=PERMANENT_LEAGUES))
    return con
