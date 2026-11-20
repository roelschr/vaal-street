"""A league's price history as dense hour x item matrices.

Models and backtests want aligned arrays, not rows. `Panel.load` pulls one
league out of the `prices` view and scatters each requested column into a
`[hours, items]` float array on a continuous hourly grid. Cells are NaN where the
item did not trade that hour, so "no print" stays distinguishable from a price.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import duckdb
import numpy as np

HOUR = 3600
CORE = ("price", "value", "volume")
LEGS = (
    "r_price", "r_lo", "r_hi", "r_volume", "r_stock_lo", "r_stock_hi", "r_bid_lo", "r_bid_hi",
    "b_price", "b_lo", "b_hi", "b_volume", "b_stock_lo", "b_stock_hi", "b_bid_lo", "b_bid_hi",
)


@dataclass
class Panel:
    league: str
    hours: np.ndarray  # [T] unix seconds, continuous hourly grid
    items: list[str]  # [N] item ids
    names: list[str]
    categories: list[str]
    bridge: np.ndarray  # [T] value of the bridge currency (Divine) in reference currency, forward-filled
    data: dict[str, np.ndarray] = field(default_factory=dict)  # column -> [T, N]

    def __getitem__(self, column: str) -> np.ndarray:
        return self.data[column]

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.hours), len(self.items)

    def index(self, item: str) -> int:
        """Column index for an item id or (case-insensitive) name."""
        if item in self._by_id:
            return self._by_id[item]
        key = item.casefold()
        if key in self._by_name:
            return self._by_name[key]
        raise KeyError(f"{item!r} did not trade in {self.league}")

    def __post_init__(self) -> None:
        self._by_id = {item: i for i, item in enumerate(self.items)}
        self._by_name = {name.casefold(): i for i, name in enumerate(self.names)}

    def window(self, start: int | None = None, end: int | None = None) -> "Panel":
        """Row slice [start, end) as a new panel sharing item metadata."""
        rows = slice(start, end)
        return Panel(
            self.league, self.hours[rows], self.items, self.names, self.categories, self.bridge[rows],
            {k: v[rows] for k, v in self.data.items()},
        )

    @classmethod
    def load(
        cls,
        con: duckdb.DuckDBPyConnection,
        league: str,
        columns: tuple[str, ...] = CORE,
        *,
        since: int | None = None,
        until: int | None = None,
        min_value: float = 0.0,
    ) -> "Panel":
        """Load one league. `since`/`until` are unix seconds; `min_value` drops items whose
        total traded value over the window is below it."""
        columns = tuple(dict.fromkeys(columns))
        where = ["league = $league"]
        params: dict = {"league": league}
        if since is not None:
            where.append("hour >= to_timestamp($since)")
            params["since"] = since
        if until is not None:
            where.append("hour <= to_timestamp($until)")
            params["until"] = until
        table = con.sql(
            f"SELECT epoch(hour)::BIGINT AS t, item, bridge, {', '.join(columns)} FROM prices WHERE {' AND '.join(where)}",
            params=params,
        ).to_arrow_table()
        if table.num_rows == 0:
            raise LookupError(f"No prices for league {league!r}")
        t = table["t"].to_numpy()
        item_col = table["item"].combine_chunks().dictionary_encode()
        codes = item_col.indices.to_numpy(zero_copy_only=False)
        ids = item_col.dictionary.to_pylist()

        if min_value > 0:
            totals = np.bincount(codes, weights=np.nan_to_num(table["value"].to_numpy(zero_copy_only=False))
                                 if "value" in columns else None, minlength=len(ids))
            keep = totals >= min_value
        else:
            keep = np.ones(len(ids), dtype=bool)
        remap = np.cumsum(keep) - 1
        mask = keep[codes]
        ids = [i for i, k in zip(ids, keep) if k]

        first, last = int(t.min()), int(t.max())
        hours = np.arange(first, last + HOUR, HOUR, dtype=np.int64)
        rows = ((t - first) // HOUR)[mask]
        cols = remap[codes][mask]
        data = {}
        for column in columns:
            values = table[column].to_numpy(zero_copy_only=False).astype(np.float64)[mask]
            grid = np.full((len(hours), len(ids)), np.nan)
            grid[rows, cols] = values
            data[column] = grid

        bridge = np.full(len(hours), np.nan)
        b = table["bridge"].to_numpy(zero_copy_only=False).astype(np.float64)
        ok = ~np.isnan(b)
        bridge[(t[ok] - first) // HOUR] = b[ok]
        bridge = ffill(bridge[:, None])[:, 0]

        meta = dict(con.execute("SELECT item, (name, category) FROM items").fetchall())
        names = [meta.get(i, (i, "Currency"))[0] for i in ids]
        categories = [meta.get(i, (i, "Currency"))[1] for i in ids]
        return cls(league, hours, ids, names, categories, bridge, data)


def ffill(a: np.ndarray, limit: int | None = None) -> np.ndarray:
    """Forward-fill NaNs down each column, optionally at most `limit` rows."""
    a = np.asarray(a, dtype=np.float64)
    seen = ~np.isnan(a)
    idx = np.where(seen, np.arange(a.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    out = np.take_along_axis(a, idx, axis=0)
    if limit is not None:
        age = np.arange(a.shape[0])[:, None] - idx
        out[age > limit] = np.nan
    out[~np.maximum.accumulate(seen, axis=0)] = np.nan
    return out


def rolling(a: np.ndarray, window: int, fn, min_count: int = 1) -> np.ndarray:
    """Trailing-window NaN-aware reduction (e.g. np.nanmedian) down each column.

    Row t summarises rows (t-window, t]. Rows with fewer than `min_count` observations are NaN.
    """
    view = _windows(a, window)
    counts = np.sum(~np.isnan(view), axis=-1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        out = fn(view, axis=-1)
    return np.where(counts >= min_count, out, np.nan)


def rolling_quantile(a: np.ndarray, window: int, q: float, min_count: int = 1) -> np.ndarray:
    """Trailing-window NaN-aware quantile down each column (linear interpolation). Much faster
    than `rolling(a, window, np.nanquantile)` because it sorts each window once."""
    ordered = np.sort(_windows(a, window), axis=-1)  # NaNs sort last
    counts = np.sum(~np.isnan(ordered), axis=-1)
    pos = q * np.maximum(counts - 1, 0)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, np.maximum(counts - 1, 0))
    low = np.take_along_axis(ordered, lo[..., None], axis=-1)[..., 0]
    high = np.take_along_axis(ordered, hi[..., None], axis=-1)[..., 0]
    out = low + (high - low) * (pos - lo)
    return np.where(counts >= min_count, out, np.nan)


def _windows(a: np.ndarray, window: int) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    pad = np.full((window - 1,) + a.shape[1:], np.nan)
    return np.lib.stride_tricks.sliding_window_view(np.concatenate([pad, a], axis=0), window, axis=0)
