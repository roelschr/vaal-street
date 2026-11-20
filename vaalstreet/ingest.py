"""Turn raw hourly digests into one Parquet file per realm and month.

Each row is one market (currency pair) in one league for one hour, kept in the
orientation GGG publishes (`item_a|item_b`). Nothing is derived here; prices and
bands are computed in the DuckDB views (see db.py).

A ratio is published as a pair of whole numbers: `ratio_*_a` units of item_a per
`ratio_*_b` units of item_b. "Lowest"/"highest" order that a/b value.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from vaalstreet.paths import REALMS, markets_dir, raw_dir

_TEXT = pa.dictionary(pa.int32(), pa.string())
SCHEMA = pa.schema(
    [
        ("hour", pa.timestamp("s", tz="UTC")),
        ("league", _TEXT),
        ("item_a", _TEXT),
        ("item_b", _TEXT),
        ("vol_a", pa.uint64()),
        ("vol_b", pa.uint64()),
        ("stock_lo_a", pa.uint64()),
        ("stock_hi_a", pa.uint64()),
        ("stock_lo_b", pa.uint64()),
        ("stock_hi_b", pa.uint64()),
        ("ratio_lo_a", pa.uint64()),
        ("ratio_lo_b", pa.uint64()),
        ("ratio_hi_a", pa.uint64()),
        ("ratio_hi_b", pa.uint64()),
    ]
)
_PER_ITEM_FIELDS = {
    "vol": "volume_traded",
    "stock_lo": "lowest_stock",
    "stock_hi": "highest_stock",
    "ratio_lo": "lowest_ratio",
    "ratio_hi": "highest_ratio",
}


def parse_digest(raw: bytes, hour: int) -> list[dict]:
    """Flatten one digest into rows matching SCHEMA."""
    when = datetime.fromtimestamp(hour, timezone.utc)
    rows = []
    for market in json.loads(raw)["markets"]:
        pair = market.get("market_pair") or market["market_id"].split("|")
        item_a, item_b = pair
        row = {"hour": when, "league": market["league"], "item_a": item_a, "item_b": item_b}
        for column, field in _PER_ITEM_FIELDS.items():
            values = market.get(field) or {}
            row[f"{column}_a"] = int(values.get(item_a, 0))
            row[f"{column}_b"] = int(values.get(item_b, 0))
        rows.append(row)
    return rows


def _month_is_stale(raw_files: list[Path], target: Path) -> bool:
    if not target.exists():
        return True
    built = target.stat().st_mtime
    return any(f.stat().st_mtime > built for f in raw_files)


def ingest(root: Path) -> dict[str, int]:
    """Rebuild every month whose raw files changed. Returns months rebuilt per realm."""
    rebuilt = {}
    for realm in REALMS:
        realm_dir = raw_dir(root) / realm
        if not realm_dir.is_dir():
            continue
        count = 0
        for month_dir in sorted(p for p in realm_dir.iterdir() if p.is_dir()):
            raw_files = sorted(month_dir.glob("*.json.gz"))
            target = markets_dir(root) / f"realm={realm}" / f"month={month_dir.name}" / "data.parquet"
            if not raw_files or not _month_is_stale(raw_files, target):
                continue
            rows = []
            for raw_file in raw_files:
                hour = int(raw_file.name.split(".")[0])
                rows.extend(parse_digest(gzip.decompress(raw_file.read_bytes()), hour))
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name("data.parquet.part")
            pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), tmp, compression="zstd")
            os.replace(tmp, target)
            count += 1
            print(f"[{realm}] {month_dir.name}: {len(raw_files)} hours, {len(rows):,} market rows", flush=True)
        rebuilt[realm] = count
    return rebuilt
