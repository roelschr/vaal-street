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

import math

from dataclasses import dataclass

import numpy as np

from vaalstreet.panel import rolling, rolling_quantile

_EPS = 1e-12
# A far print confirms a reprice only if it traded at least this share of a typical hour's units.
CONFIRM_UNITS = 0.5


@dataclass
class FairValue:
    fair: np.ndarray  # [T, N] filtered price (not log), NaN before an item's first print
    sd: np.ndarray  # [T, N] posterior sd of log fair value
    z: np.ndarray  # [T, N] standardised surprise of each print vs the forecast made before it
    q: np.ndarray  # [N] latest per-hour variance of the true log price
    r: np.ndarray  # [N] latest observation-noise variance at typical liquidity
    # [T, N] sd of the next hour's print around this hour's fair value, at typical liquidity:
    # the value's own uncertainty, one hour of real movement and the print noise, each with the
    # parameters in force at that hour (so it is causal, unlike rebuilding it from q and r above).
    spread: np.ndarray

    @property
    def low(self) -> np.ndarray:
        """[T, N] fair value one sd below, in price: where the item's worth plausibly sits (about 68%)."""
        return self.fair * np.exp(-self.sd)

    @property
    def high(self) -> np.ndarray:
        """[T, N] fair value one sd above, in price."""
        return self.fair * np.exp(self.sd)

    @property
    def next_low(self) -> np.ndarray:
        """[T, N] one `spread` below fair value: the next hour's print lands above this about 84% of the time."""
        return self.fair * np.exp(-self.spread)

    @property
    def next_high(self) -> np.ndarray:
        """[T, N] one `spread` above fair value."""
        return self.fair * np.exp(self.spread)


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
    spread = np.full((T, N), np.nan)
    z = np.full((T, N), np.nan)
    x = np.full(N, np.nan)
    P = np.full(N, np.nan)
    q = np.full(N, 0.02**2)
    r = np.full(N, 0.05**2)
    typical = np.ones(N)
    typical_units = np.ones(N)
    units = value / np.where(price > 0, price, np.nan) if value is not None else None
    pending = np.full(N, np.nan)  # log price of an unconfirmed far-out print

    for t in range(T):
        if t % refit_every == 0 and t >= 12:
            lo = max(0, t - lookback)
            q, r = _noise_split(d[lo:t])
            if value is not None:
                med = np.nanmedian(value[lo:t], axis=0)
                typical = np.where(np.isfinite(med) & (med > 0), med, 1.0)
                med = np.nanmedian(units[lo:t], axis=0)
                typical_units = np.where(np.isfinite(med) & (med > 0), med, 1.0)
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
        # Confirmed when this print is closer to the one set aside than to the old value, and is not
        # a thin hour. Counted in units, because a joke price inflates the traded value: two joke
        # trades in a row (a few units at half a Divine each) must not reprice an item whose next
        # real hour trades hundreds at the old level.
        confirmed = far & np.isfinite(pending) & (np.abs(y[t] - pending) < 0.5 * np.abs(pending - x))
        if units is not None:
            confirmed &= units[t] >= CONFIRM_UNITS * typical_units
        update = seen & ~far
        z[t] = np.where(seen, score, np.nan)
        x = np.where(update, x + K * innovation, x)
        P = np.where(update, (1 - K) * P_pred, np.where(np.isnan(x), P, P_pred))
        x = np.where(fresh | confirmed, y[t], x)
        P = np.where(fresh | confirmed, R, P)
        pending = np.where(far & ~confirmed, y[t], np.where(obs, np.nan, pending))
        fair[t] = np.exp(x)
        sd[t] = np.sqrt(P)
        spread[t] = np.sqrt(P + q + r)
    return FairValue(fair, sd, z, q, r, spread)


@dataclass
class TwoMarket:
    """One value per item plus the Divine market's premium over it, all in log terms except `value`."""

    value: np.ndarray  # [T, N] what the item is worth in the Chaos market (price, not log)
    premium: np.ndarray  # [T, N] log(Divine-market price / Chaos-market price); NaN until both markets printed
    value_sd: np.ndarray  # [T, N] posterior sd of log value
    premium_sd: np.ndarray  # [T, N] posterior sd of the log premium

    @property
    def divine(self) -> np.ndarray:
        """[T, N] what the item is worth in the Divine market, in Chaos at that hour's Divine price."""
        return self.value * np.exp(self.premium)

    @property
    def low(self) -> np.ndarray:
        return self.value * np.exp(-self.value_sd)

    @property
    def high(self) -> np.ndarray:
        return self.value * np.exp(self.value_sd)

    @property
    def premium_low(self) -> np.ndarray:
        return self.premium - self.premium_sd

    @property
    def premium_high(self) -> np.ndarray:
        return self.premium + self.premium_sd


def _observe(v, p, a, c, d, y, R, with_premium, mask):
    """One scalar Kalman update of state (v, p), covariance [[a, c], [c, d]], where `mask` holds.

    The observation is v (a Chaos-market print) or v + p (a Divine-market print).
    """
    if with_premium:
        ha, hd = a + c, c + d  # P @ [1, 1]
        e = y - (v + p)
    else:
        ha, hd = a, c  # P @ [1, 0]
        e = y - v
    S = (ha + hd if with_premium else ha) + R
    k1, k2 = ha / (S + _EPS), hd / (S + _EPS)
    out = (v + k1 * e, p + k2 * e, a - ha * ha / (S + _EPS), c - ha * hd / (S + _EPS), d - hd * hd / (S + _EPS))
    return tuple(np.where(mask, new, old) for new, old in zip(out, (v, p, a, c, d)))


def two_market(
    r_price: np.ndarray,
    b_price: np.ndarray,
    r_value: np.ndarray | None = None,
    b_value: np.ndarray | None = None,
    *,
    lookback: int = 168,
    refit_every: int = 24,
    premium_share: float = 0.1,
    outlier_z: float = 3.0,
    reject_z: float = 6.0,
) -> TwoMarket:
    """Causal joint fit of an item's two markets as one log value v plus a slow log premium p.

    State (v, p) per item follows a random walk. v moves with the variance q that `_noise_split`
    finds in the Chaos market's own returns (the Divine market's where the Chaos market is too thin);
    p moves with variance (premium_share**2) * q, so with the default the premium's hourly drift is a
    tenth of the value's. That ratio is a modelling choice, not a fit. A Chaos-market print observes
    v and a Divine-market print (already in Chaos at that hour's Divine price) observes v + p, each
    with its own market's bounce noise, scaled down in hours that traded more value. Both prints of
    an hour are scored against the forecast made before either is used. Parameters are re-fitted on
    trailing data only, and a far-out print is set aside until a second print in the same market
    confirms it, as in `fair_value`.

    Items with one market keep a NaN premium: before the Chaos market has printed, Divine-market
    prints stand in for the value directly.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        yr = np.log(np.where(r_price > 0, r_price, np.nan))
        yb = np.log(np.where(b_price > 0, b_price, np.nan))
    T, N = yr.shape
    nan_row = np.full((1, N), np.nan)
    dr = np.vstack([nan_row, yr[1:] - yr[:-1]])
    db = np.vstack([nan_row, yb[1:] - yb[:-1]])
    out_v, out_p, out_vsd, out_psd = (np.full((T, N), np.nan) for _ in range(4))
    v, p, a, c, d = (np.full(N, np.nan) for _ in range(5))
    seen_r = np.zeros(N, bool)  # the Chaos market has printed, so v is anchored to it
    q = np.full(N, 0.02**2)
    rr, rb = np.full(N, 0.05**2), np.full(N, 0.05**2)
    typ_r, typ_b = np.ones(N), np.ones(N)
    pend_r, pend_b = np.full(N, np.nan), np.full(N, np.nan)

    def weight(val, typical, obs):
        if val is None:
            return np.ones(N)
        w = np.clip(np.where(obs, val, np.nan) / typical, 0.2, 5.0)
        return np.where(np.isfinite(w), w, 1.0)

    def typical(val, lo, t):
        med = np.nanmedian(val[lo:t], axis=0)
        return np.where(np.isfinite(med) & (med > 0), med, 1.0)

    for t in range(T):
        if t % refit_every == 0 and t >= 12:
            lo = max(0, t - lookback)
            q_r, rr = _noise_split(dr[lo:t])
            q_b, rb = _noise_split(db[lo:t])
            q = np.where(np.sum(np.isfinite(dr[lo:t]), axis=0) >= 24, q_r, q_b)
            if r_value is not None:
                typ_r = typical(r_value, lo, t)
            if b_value is not None:
                typ_b = typical(b_value, lo, t)
        ok_r, ok_b = np.isfinite(yr[t]), np.isfinite(yb[t])
        Rr = rr / weight(None if r_value is None else r_value[t], typ_r, ok_r)
        Rb = rb / weight(None if b_value is None else b_value[t], typ_b, ok_b)
        known = np.isfinite(p)
        # Predict: both parts drift; the premium much more slowly.
        a = a + q
        d = np.where(known, d + premium_share**2 * q, d)
        # Score both prints against the same forecast, before either is used.
        f_b = np.where(known, v + p, v)
        S_r = a + Rr
        S_b = np.where(known, a + 2 * c + d, a) + Rb
        e_r, e_b = yr[t] - v, yb[t] - f_b
        z_r, z_b = e_r / np.sqrt(S_r + _EPS), e_b / np.sqrt(S_b + _EPS)
        live = np.isfinite(v)
        far_r = live & ok_r & seen_r & (np.abs(z_r) > reject_z)
        far_b = live & ok_b & (np.abs(z_b) > reject_z)
        conf_r = far_r & np.isfinite(pend_r) & (np.abs(yr[t] - pend_r) < 0.5 * np.abs(pend_r - v))
        conf_b = far_b & np.isfinite(pend_b) & (np.abs(yb[t] - pend_b) < 0.5 * np.abs(pend_b - f_b))
        pend_r = np.where(far_r & ~conf_r, yr[t], np.where(ok_r, np.nan, pend_r))
        pend_b = np.where(far_b & ~conf_b, yb[t], np.where(ok_b, np.nan, pend_b))
        Rr_eff = Rr * np.maximum(1.0, (np.abs(z_r) / outlier_z) ** 2)
        Rb_eff = Rb * np.maximum(1.0, (np.abs(z_b) / outlier_z) ** 2)

        # Chaos-market print.
        upd_r = live & ok_r & seen_r & ~far_r
        v, p, a, c, d = _observe(v, p, a, c, d, yr[t], Rr_eff, False, upd_r & ~known)
        v, p, a, c, d = _observe(v, p, a, c, d, yr[t], Rr_eff, False, upd_r & known)
        # First Chaos print, or the Chaos market confirmed a repricing: re-anchor v, keep the premium.
        # If v so far came from Divine prints, the gap between them is the first premium estimate.
        first_r = ok_r & ~seen_r
        from_b = first_r & live & ~known
        p = np.where(from_b, v - yr[t], p)
        d = np.where(from_b, a + Rr, d)
        c = np.where(from_b, -Rr, np.where(conf_r | first_r, 0.0, c))
        reset_r = first_r | conf_r
        v = np.where(reset_r, yr[t], v)
        a = np.where(reset_r, Rr, a)
        seen_r |= ok_r
        known = np.isfinite(p)

        # Divine-market print.
        live = np.isfinite(v)
        upd_b = live & ok_b & ~far_b & ~conf_r
        v, p, a, c, d = _observe(v, p, a, c, d, yb[t], Rb_eff, True, upd_b & known)
        # Without a premium yet: before any Chaos print the Divine market stands in for the value;
        # after one, the first Divine print sets the premium as the gap to the current value.
        v, p, a, c, d = _observe(v, p, a, c, d, yb[t], Rb_eff, False, upd_b & ~known & ~seen_r)
        start_p = ok_b & live & ~known & seen_r & ~conf_r
        p = np.where(start_p, yb[t] - v, p)
        d = np.where(start_p, a + Rb, d)
        c = np.where(start_p, -a, c)
        # The Divine market confirmed a repricing the Chaos market has not shown: move the value.
        jump_b = conf_b & ~conf_r
        v = np.where(jump_b, yb[t] - np.where(np.isfinite(p), p, 0.0), v)
        a = np.where(jump_b, Rb, a)
        c = np.where(jump_b, 0.0, c)
        fresh = ok_b & ~live & ~ok_r
        v = np.where(fresh, yb[t], v)
        a = np.where(fresh, Rb, a)

        out_v[t] = np.exp(v)
        out_p[t] = p
        out_vsd[t] = np.sqrt(np.maximum(a, 0))
        out_psd[t] = np.sqrt(np.maximum(d, 0))
    return TwoMarket(out_v, out_p, out_vsd, out_psd)


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


def fit_ratio(
    price: float, max_currency: float, max_items: float = 5000, side: str | None = None
) -> tuple[int, int] | None:
    """The whole-number (currency, items) order closest to `price` that a small purse can post.

    `max_currency` caps the currency side and `max_items` the item side. Returns None when even
    the smallest order does not fit (one item costs more than the purse holds). With `side`
    "buy" the ratio never exceeds `price`, with "sell" it never falls below it, so a resting
    order states no worse a price than its limit.
    """
    if side is not None:
        return _fit_ratio_side(price, max_currency, max_items, side == "buy")
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


def _fit_ratio_side(price: float, max_currency: float, max_items: float, buy: bool) -> tuple[int, int] | None:
    """fit_ratio, rounding only in the order's favour: down for a buy, up for a sell."""
    if not np.isfinite(price) or price <= 0 or max_currency < 1 or max_items < 1:
        return None
    best, best_err = None, np.inf
    eps = 1e-9  # keeps an exact ratio such as 3.3 * 10 from flooring to 32
    if price >= 1:
        for items in range(1, int(min(max_items, 400)) + 1):
            currency = math.floor(price * items + eps) if buy else math.ceil(price * items - eps)
            if currency < 1:
                continue
            if currency > max_currency:
                break
            err = abs(currency / items - price) / price
            if err < best_err - 1e-12:
                best, best_err = (currency, items), err
            if err <= 0.004:
                break
    else:
        for currency in range(1, int(min(max_currency, 400)) + 1):
            items = math.ceil(currency / price - eps) if buy else math.floor(currency / price + eps)
            if items < 1:
                continue
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


def market_scorecard(
    price: np.ndarray, value: np.ndarray,
    r_price: np.ndarray, r_value: np.ndarray, b_price: np.ndarray, b_value: np.ndarray,
    horizon: int = 6, warmup: int = 48,
) -> list[dict]:
    """Each market's next `horizon` hours predicted four ways, to judge the two-market model.

    For the Chaos market and the Divine market separately, the target is that market's own
    value-weighted price over the following hours. The predictors, all known at hour t:
    the two-market model (value, or value plus premium), `fair_value` of that market alone
    (what the desk quotes for each market today), `fair_value` of the blended price, and the
    market's last print. Every mean is over the same cells, where all four and the target exist.
    """
    from vaalstreet.panel import ffill

    tm = two_market(r_price, b_price, r_value, b_value)
    with np.errstate(divide="ignore", invalid="ignore"):
        blended = np.log(fair_value(price, value).fair)
        guesses = {
            "Chaos": (np.log(tm.value), r_price, r_value),
            "Divine": (np.log(tm.divine), b_price, b_value),
        }
        rows = []
        for market, (model, mp, mv) in guesses.items():
            target = forward_vwap(mp, mv, horizon)
            preds = {
                "two_market": model,
                "own_market": np.log(fair_value(mp, mv).fair),
                "blended": blended,
                "last": np.log(ffill(mp)),
            }
            printed = np.mean(np.isfinite(mp), axis=0)
            for label, lo, hi in (("trades almost every hour", 0.9, 1.01), ("trades most hours", 0.5, 0.9),
                                  ("trades some hours", 0.1, 0.5)):
                group = (printed >= lo) & (printed < hi)
                errs = {k: np.abs(target[warmup:, group] - v[warmup:, group]) for k, v in preds.items()}
                ok = np.logical_and.reduce([np.isfinite(e) for e in errs.values()])
                if not ok.any():
                    continue
                row = {"market": market, "items": label, "count": int(group.sum()), "cells": int(ok.sum())}
                row.update({f"{k}_mean": float(np.mean(e[ok])) for k, e in errs.items()})
                row["improvement"] = float(1 - row["two_market_mean"] / row["own_market_mean"])
                rows.append(row)
    return rows
