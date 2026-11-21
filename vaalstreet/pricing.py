"""Pricing models over hourly exchange prints.

An hourly VWAP is a noisy observation of what an item is worth: thin hours print
anywhere inside a wide executed range, and a single fat-fingered trade moves it.
Three pieces turn prints into numbers a trader can act on.

`fair_value`
    A local-level Kalman filter on log price. Each item has a slow random-walk
    "true" value (variance q per hour) observed with noise (variance r, scaled
    down in hours that traded more value). q and r come from the item's own
    returns: bounce shows up as negative lag-1 autocovariance (the Roll model),
    what is left is real movement. Parameters are re-fitted on trailing data only
    and outliers are down-weighted, so the output is causal and can be used
    as-is inside a backtest.

`typical_range`
    How far below / above the hour's VWAP trades actually execute. The published
    lowest/highest ratios are polluted by joke trades (a Divine for 1 Chaos), so
    the estimate is a trailing median of each side's log distance - robust to a
    large share of garbage. This is what a patient order can expect to fill at.

`leg_premium`
    The same item costs more against Divine than against Chaos, persistently,
    because the people lifting offers in each market are different crowds. A
    trailing median of the log ratio of the two legs measures that structural gap.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vaalstreet.panel import rolling, rolling_quantile

_EPS = 1e-12


@dataclass
class FairValue:
    fair: np.ndarray  # [T, N] filtered price (not log), NaN before an item's first print
    sd: np.ndarray  # [T, N] posterior sd of log fair value
    z: np.ndarray  # [T, N] standardised surprise of each print vs the forecast made before it
    q: np.ndarray  # [N] latest per-hour variance of the true log price
    r: np.ndarray  # [N] latest observation-noise variance at typical liquidity


def _noise_split(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split the variance of hourly log returns d[T, N] into signal q and noise r per item."""
    med = np.nanmedian(d, axis=0)
    mad = 1.4826 * np.nanmedian(np.abs(d - med), axis=0)
    limit = np.where(mad > 0, 4 * mad, np.inf)
    c = np.clip(d - med, -limit, limit)
    n = np.sum(~np.isnan(c), axis=0)
    var = np.nansum(c * c, axis=0) / np.maximum(n, 1)
    pair = c[1:] * c[:-1]
    n_pair = np.sum(~np.isnan(pair), axis=0)
    cov = np.nansum(pair, axis=0) / np.maximum(n_pair, 1)
    r = np.clip(-cov, 0.02 * var, 0.49 * var)
    q = np.maximum(var - 2 * r, 0.02 * var)
    enough = (n >= 24) & (n_pair >= 12) & (var > 0)
    # Items with too little history borrow the typical split from the rest of the market.
    if enough.any():
        q_fallback, r_fallback = np.median(q[enough]), np.median(r[enough])
    else:
        q_fallback, r_fallback = 0.02**2, 0.05**2
    return np.where(enough, q, q_fallback), np.where(enough, r, r_fallback)


def fair_value(
    price: np.ndarray,
    value: np.ndarray | None = None,
    *,
    lookback: int = 168,
    refit_every: int = 24,
    outlier_z: float = 3.0,
    reject_z: float = 6.0,
) -> FairValue:
    """Causal fair value for price[T, N] (NaN = no print). `value` is traded value, for weighting.

    A print more than `reject_z` forecast deviations away is not believed on its own: it is
    set aside, and only if the next print lands near it does the model accept that the item
    really repriced (a crash after a patch) rather than someone fat-fingering a trade.
    """
    y = np.log(np.where(price > 0, price, np.nan))
    T, N = y.shape
    d = np.vstack([np.full((1, N), np.nan), y[1:] - y[:-1]])
    fair = np.full((T, N), np.nan)
    sd = np.full((T, N), np.nan)
    z = np.full((T, N), np.nan)
    x = np.full(N, np.nan)
    P = np.full(N, np.nan)
    q = np.full(N, 0.02**2)
    r = np.full(N, 0.05**2)
    typical = np.ones(N)
    pending = np.full(N, np.nan)  # log price of an unconfirmed far-out print

    for t in range(T):
        if t % refit_every == 0 and t >= 12:
            lo = max(0, t - lookback)
            q, r = _noise_split(d[lo:t])
            if value is not None:
                med = np.nanmedian(value[lo:t], axis=0)
                typical = np.where(np.isfinite(med) & (med > 0), med, 1.0)
        obs = ~np.isnan(y[t])
        fresh = obs & np.isnan(x)
        P_pred = P + q
        if value is not None:
            weight = np.clip(np.where(obs, value[t], np.nan) / typical, 0.2, 5.0)
        else:
            weight = np.ones(N)
        R = r / np.where(np.isfinite(weight), weight, 1.0)
        innovation = y[t] - x
        S = P_pred + R
        score = innovation / np.sqrt(S + _EPS)
        # Huber-style: a print far outside the forecast band is treated as much noisier.
        R_eff = R * np.maximum(1.0, (np.abs(score) / outlier_z) ** 2)
        K = P_pred / (P_pred + R_eff + _EPS)
        seen = obs & ~fresh
        far = seen & (np.abs(score) > reject_z)
        # Confirmed when this print is closer to the one set aside than to the old value.
        confirmed = far & np.isfinite(pending) & (np.abs(y[t] - pending) < 0.5 * np.abs(pending - x))
        update = seen & ~far
        z[t] = np.where(seen, score, np.nan)
        x = np.where(update, x + K * innovation, x)
        P = np.where(update, (1 - K) * P_pred, np.where(np.isnan(x), P, P_pred))
        x = np.where(fresh | confirmed, y[t], x)
        P = np.where(fresh | confirmed, R, P)
        pending = np.where(far & ~confirmed, y[t], np.where(obs, np.nan, pending))
        fair[t] = np.exp(x)
        sd[t] = np.sqrt(P)
    return FairValue(fair, sd, z, q, r)


@dataclass
class Range:
    down: np.ndarray  # [T, N] typical log distance from VWAP to the low end of executed trades
    up: np.ndarray  # [T, N] typical log distance from VWAP to the high end


def typical_range(
    price: np.ndarray, low: np.ndarray, high: np.ndarray, window: int = 24, min_count: int = 6, quantile: float = 0.5
) -> Range:
    """Robust trailing estimate of how far executed prices spread around the hourly VWAP.

    `quantile` picks the distance reached in that share of hours: 0.5 is the typical
    hour, lower is a closer (faster-filling) level, higher a more patient one.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        down = np.log(price / low)
        up = np.log(high / price)
    down = np.where(np.isfinite(down) & (down >= 0), down, np.nan)
    up = np.where(np.isfinite(up) & (up >= 0), up, np.nan)
    return Range(
        rolling_quantile(down, window, quantile, min_count), rolling_quantile(up, window, quantile, min_count)
    )


def leg_premium(r_price: np.ndarray, b_price: np.ndarray, window: int = 24, min_count: int = 6) -> np.ndarray:
    """Trailing median of log(bridge-leg price / reference-leg price): [T, N]."""
    with np.errstate(divide="ignore", invalid="ignore"):
        gap = np.log(b_price / r_price)
    return rolling(np.where(np.isfinite(gap), gap, np.nan), window, np.nanmedian, min_count)


def activity(price: np.ndarray, window: int = 24) -> np.ndarray:
    """Share of the trailing `window` hours in which the item printed: [T, N] in 0..1."""
    printed = (~np.isnan(price)).astype(np.float64)
    csum = np.cumsum(np.vstack([np.zeros((1, printed.shape[1])), printed]), axis=0)
    idx = np.arange(1, printed.shape[0] + 1)
    start = np.maximum(idx - window, 0)
    return (csum[idx] - csum[start]) / np.minimum(idx, window)[:, None]


def trailing_sum(a: np.ndarray, window: int) -> np.ndarray:
    """NaN-as-zero trailing sum down each column."""
    filled = np.nan_to_num(a)
    csum = np.cumsum(np.vstack([np.zeros((1,) + filled.shape[1:]), filled]), axis=0)
    idx = np.arange(1, filled.shape[0] + 1)
    return csum[idx] - csum[np.maximum(idx - window, 0)]


def nice_ratio(price: float, max_units: int = 500, tolerance: float = 0.004) -> tuple[int, int]:
    """Whole-number (currency, items) pair with currency/items close to `price`.

    The exchange takes orders as integer ratios, so 0.3157 Chaos each has to become
    "6 Chaos for 19". Returns the smallest pair within `tolerance` (relative), or the
    closest pair whose smaller side is at most `max_units`.
    """
    if not np.isfinite(price) or price <= 0:
        raise ValueError("price must be positive")
    if price < 1:
        items, currency = nice_ratio(1 / price, max_units, tolerance)
        return currency, items
    best = (max(1, round(price)), 1)
    best_err = abs(best[0] / best[1] - price) / price
    for items in range(1, max_units + 1):
        if best_err <= tolerance:
            break
        currency = max(1, round(price * items))
        err = abs(currency / items - price) / price
        if err < best_err - 1e-12:
            best, best_err = (currency, items), err
    return best


def fit_ratio(price: float, max_currency: float, max_items: float = 5000) -> tuple[int, int] | None:
    """The whole-number (currency, items) order closest to `price` that a small purse can post.

    `max_currency` caps the currency side and `max_items` the item side. Returns None when even
    the smallest order does not fit (one item costs more than the purse holds).
    """
    if not np.isfinite(price) or price <= 0 or max_currency < 1 or max_items < 1:
        return None
    best, best_err = None, np.inf
    if price >= 1:
        for items in range(1, int(min(max_items, 400)) + 1):
            currency = max(1, round(price * items))
            if currency > max_currency:
                break
            err = abs(currency / items - price) / price
            if err < best_err - 1e-12:
                best, best_err = (currency, items), err
            if err <= 0.004:
                break
    else:
        for currency in range(1, int(min(max_currency, 400)) + 1):
            items = max(1, round(currency / price))
            if items > max_items:
                break
            err = abs(currency / items - price) / price
            if err < best_err - 1e-12:
                best, best_err = (currency, items), err
            if err <= 0.004:
                break
    return best


def forward_vwap(price: np.ndarray, value: np.ndarray, horizon: int) -> np.ndarray:
    """Value-weighted mean log price over the `horizon` hours after each row: [T, N], NaN if none traded."""
    seen = np.isfinite(price) & (price > 0)
    weight = np.where(seen, np.nan_to_num(value), 0.0)
    logp = np.where(seen, np.log(np.where(seen, price, 1.0)), 0.0)
    zero = np.zeros((1, price.shape[1]))
    num = np.cumsum(np.vstack([zero, weight * logp]), axis=0)
    den = np.cumsum(np.vstack([zero, weight]), axis=0)
    T = price.shape[0]
    start = np.minimum(np.arange(T) + 1, T)
    end = np.minimum(start + horizon, T)
    total = den[end] - den[start]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(total > 0, (num[end] - num[start]) / total, np.nan)


def scorecard(price: np.ndarray, value: np.ndarray, horizon: int = 6, warmup: int = 48) -> list[dict]:
    """How well fair value predicts where an item trades next, against simply using the last print.

    The target is the value-weighted price over the following `horizon` hours - what you would
    actually transact at. Errors are absolute log differences (0.05 is about 5%), reported for
    items grouped by how regularly they trade. Everything is out of sample: the estimate at
    hour t uses hours up to t only.
    """
    from vaalstreet.panel import ffill

    target = forward_vwap(price, value, horizon)
    with np.errstate(divide="ignore", invalid="ignore"):
        last = np.log(ffill(price))
        fair = np.log(fair_value(price, value).fair)
    printed = np.mean(np.isfinite(price), axis=0)
    rows = []
    for label, lo, hi in (("trades almost every hour", 0.9, 1.01), ("trades most hours", 0.5, 0.9),
                          ("trades some hours", 0.1, 0.5)):
        group = (printed >= lo) & (printed < hi)
        err_fair = np.abs(target[warmup:, group] - fair[warmup:, group])
        err_last = np.abs(target[warmup:, group] - last[warmup:, group])
        ok = np.isfinite(err_fair) & np.isfinite(err_last)
        if not ok.any():
            continue
        rows.append({
            "items": label, "count": int(group.sum()),
            "fair_median": float(np.median(err_fair[ok])), "last_median": float(np.median(err_last[ok])),
            "fair_mean": float(np.mean(err_fair[ok])), "last_mean": float(np.mean(err_last[ok])),
            "improvement": float(1 - np.mean(err_fair[ok]) / np.mean(err_last[ok])),
        })
    return rows
