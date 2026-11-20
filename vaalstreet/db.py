"""DuckDB views over the Parquet archive.

    markets        one row per market, league and hour, as published
    legs           every market seen from both sides: the price of `item` in `quote`
    prices         one reference-currency price per item, league and hour (see ingest.py)
    items          item id -> name, category, stack size
    leagues        first/last hour, traded value and kind per league
    league_prices  prices plus name, category and hours/days since the league's first trade

Reference currency: Chaos Orb in PoE1, Exalted Orb in PoE2. Every process opens
its own in-memory DuckDB over the same Parquet files, so the desk, the CLI and a
notebook can all read while an ingest replaces month files atomically.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa

from vaalstreet import items
from vaalstreet.ingest import ANCHORS, LEGS_SQL
from vaalstreet.paths import data_dir, markets_dir, prices_dir

PERMANENT_LEAGUES = ("Standard", "Hardcore", "Ruthless", "Hardcore Ruthless")

_VIEWS = r"""
CREATE VIEW markets AS
SELECT * EXCLUDE (realm, month) FROM read_parquet('{markets}', hive_partitioning = true) WHERE realm = '{realm}';

CREATE VIEW legs AS {legs};

CREATE VIEW prices AS
SELECT * EXCLUDE (realm, month) FROM read_parquet('{prices}', hive_partitioning = true) WHERE realm = '{realm}';

CREATE VIEW leagues AS
SELECT league,
       min(hour) AS first_hour, max(hour) AS last_hour, count(DISTINCT hour) AS hours,
       sum(value) AS value,
       CASE WHEN regexp_matches(league, '\(PL\d+\)$') THEN 'private'
            WHEN league IN {permanent} THEN 'permanent'
            WHEN regexp_matches(league, '^(Hardcore|HC|Ruthless|SSF|Solo) ') THEN 'variant'
            ELSE 'challenge' END AS kind
FROM prices
GROUP BY league;

CREATE VIEW league_prices AS
SELECT p.*, i.name, i.category,
       date_diff('hour', l.first_hour, p.hour) AS league_hour,
       date_diff('hour', l.first_hour, p.hour) // 24 AS league_day
FROM prices p
JOIN leagues l USING (league)
LEFT JOIN items i USING (item);
"""


def has_data(root: Path | None = None, realm: str = "poe1") -> bool:
    root = root or data_dir()
    return any((prices_dir(root) / f"realm={realm}").glob("month=*/data.parquet"))


def connect(root: Path | None = None, realm: str = "poe1") -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with the views above. Run `vaal sync` first."""
    root = root or data_dir()
    if not has_data(root, realm):
        raise FileNotFoundError(f"No {realm} data under {root}. Run `python -m vaalstreet sync` first.")

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")

    meta = items.load(root, realm)
    ref, _bridge = ANCHORS[realm]
    rows = [
        {"item": item_id, "name": e["name"], "category": e["category"], "stack": e.get("stack")}
        for item_id, e in meta.items()
    ]
    schema = pa.schema([("item", pa.string()), ("name", pa.string()), ("category", pa.string()), ("stack", pa.int64())])
    known = pa.Table.from_pylist(rows, schema=schema)  # noqa: F841 (read by DuckDB)
    con.execute("CREATE TABLE known_items AS SELECT * FROM known")

    markets = (markets_dir(root) / "realm=*" / "month=*" / "data.parquet").as_posix()
    prices = (prices_dir(root) / "realm=*" / "month=*" / "data.parquet").as_posix()
    # Names for every traded item, falling back to one derived from the ID.
    con.execute(
        f"""
        CREATE TABLE items AS
        SELECT t.item,
               coalesce(k.name, regexp_replace(regexp_replace(split_part(t.item, '/', -1),
                        '^(Currency|DivinationCard)', ''), '([a-z0-9])([A-Z])', '\\1 \\2', 'g')) AS name,
               coalesce(k.category, 'Currency') AS category, k.stack
        FROM (SELECT DISTINCT item FROM read_parquet('{prices}', hive_partitioning = true) WHERE realm = '{realm}'
              UNION SELECT '{ref}') t
        LEFT JOIN known_items k USING (item)
        """
    )
    con.execute(
        _VIEWS.format(
            markets=markets, prices=prices, realm=realm, permanent=PERMANENT_LEAGUES,
            legs=LEGS_SQL.format(markets="markets"),
        )
    )
    return con
