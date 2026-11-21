"""Measurements: claims about the market, tested with pass rules fixed in advance.

Several ideas for the desk are claims about the market rather than features: "resting stock
predicts the price", "the Divine premium follows the league day". With seven or more such
claims and only a handful of leagues, one of them is likely to look good by chance. So the
rules a claim must meet were written down first, in `docs/ideas.md` under "How a measurement
passes", and this module applies them as written. Every constant below that decides a verdict
comes from that page. Changing one is a new setting tried and belongs on that page too.

Three kinds of measurement:

* Forecasting signals (`two-market`, `premium`, `orderbook`) predict the value-weighted price
  over the next `HORIZON` hours. They are tuned on training leagues, which ended before the
  test league began, and scored on the test league against two baselines: the desk's fair value
  and the last print. The error cut is summed per day, and the pooled interval comes from
  resampling whole days within each league, so items that move together are not counted as
  independent evidence. The interval is Bonferroni-corrected over the whole `FAMILY`.
* The detector (`regime`) is checked on the known case and on its false-alarm rate in quiet
  markets.
* Discovery (`relationships`, `recipes`) and report-only measurements (`spread`,
  `queue-vs-share`, `ladder-rates`) list candidates or describe the data. They never feed a plan.

Every measurement is causal: anything fitted at hour t uses hours up to t of the same league, or
earlier leagues. The core functions take `Season`s (a league's panel and its first hour), so the
tests can feed planted signals and pure noise through the same path the archive takes.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from vaalstreet import pricing
from vaalstreet.panel import CORE, HOUR, LEGS, Panel, ffill, rolling

# ---- The rules, as written in docs/ideas.md ("How a measurement passes"). -------------------
HORIZON = 6  # hours ahead: the scorecard's target
WARMUP = 48  # hours skipped at the start of each league
MIN_DAYS = 14  # a league needs this much history to count
MIN_TEST_LEAGUES = 3  # forecasting needs this many test leagues for "a majority" to mean something
FAMILY = 7  # forecasting hypotheses and settings tried, across the whole harness
ALPHA = 0.05
RESAMPLES = 10_000
SEED = 20261121
TWO_MARKET_SHARES = (0.05, 0.1, 0.2)
ORDERBOOK_LOOKBACKS = (3, 6, 24)
# Detector.
Z_CAP, ALLOWANCE, ALARM_AT = 5.0, 1.0, 8.0
GATE_HOURS, GATE_WINDOW = 6, 168
EARLY, LATE = 48, 6  # hours before / after the event hour that count as flagging it
QUIET_MOVE, QUIET_PRINTED, FALSE_ALARM_CEILING = 0.2, 0.5, 0.1
KNOWN_CASES = (("Covens", "Foulborn Exalted Orb"),)
# Discovery.
TOP_ITEMS = 60
MIN_ITEM_VALUE = 10_000  # Chaos traded over the whole league; items below it are left out
RECIPE_MAX, RECIPE_TOLERANCE, RECIPE_MAD, RECIPE_DAYS = 10, 0.02, 0.03, 7
LEAD_CORR, BASKET_CORR, MIN_PAIR_LEAGUES = 0.1, 0.6, 2

PASS, FAIL, THIN, REPORT, CANDIDATES = "pass", "fail", "not enough data", "report only", "candidates"


@dataclass
class Season:
    """One league as a measurement sees it: the panel from the league's first hour, and whether it
    may count toward a verdict (a known start, a challenge league, enough days)."""

    panel: Panel
    eligible: bool = True
    why_not: str = ""

    @property
    def name(self) -> str:
        return self.panel.league

    @property
    def start(self) -> int:
        return int(self.panel.hours[0])

    @property
    def end(self) -> int:
        return int(self.panel.hours[-1])

    @property
    def counts(self) -> bool:
        return self.eligible and len(self.panel.hours) >= MIN_DAYS * 24


@dataclass
class Result:
    name: str
    verdict: str
    settings_tried: int = 1
    family: int | None = None  # the Bonferroni count, for forecasting measurements
    numbers: dict = field(default_factory=dict)
    leagues: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def lines(self) -> list[str]:
        out = [f"{self.name}: {self.verdict}"]
        tried = f"settings tried: {self.settings_tried}"
        if self.family is not None:
            tried += f"; intervals corrected for {self.family} hypotheses and settings in all"
        out.append("  " + tried)
        for k, v in self.numbers.items():
            out.append(f"  {k}: {_fmt(v)}")
        for row in self.leagues:
            out.append("  " + ", ".join(f"{k} {_fmt(v)}" for k, v in row.items()))
        for row in self.candidates[:25]:
            out.append("  - " + ", ".join(f"{k} {_fmt(v)}" for k, v in row.items()))
        if len(self.candidates) > 25:
            out.append(f"  ... and {len(self.candidates) - 25} more")
        out += ["  " + n for n in self.notes]
        out.append(f"  took {self.seconds:.1f} s")
        return out


def _fmt(v) -> str:
    if isinstance(v, float):
        return "n/a" if not math.isfinite(v) else f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(_fmt(x) for x in v) + ")"
    return str(v)


# ---- League splits and the forecast verdict --------------------------------------------------
def splits(seasons: list[Season]) -> list[tuple[Season, list[Season]]]:
    """Each league that has training leagues, with those leagues, by the fold rule of strategy search
    (`search.ended_before`): only leagues whose last hour came before the test league's first hour.
    A league overlapping the test league in time is never used to fit. A test league that counts
    toward a verdict trains only on leagues that count too; the others, shown for information, may
    train on whatever came before them."""
    from vaalstreet import search

    rows = [{"league": s.name, "first_hour": s.start, "last_hour": s.end, "season": s}
            for s in sorted(seasons, key=lambda s: (s.start, s.name))]
    out = []
    for row in rows:
        test = row["season"]
        pool = [r for r in rows if r["season"].counts or not test.counts]
        train = [r["season"] for r in search.ended_before(pool, test.start)]
        if train:
            out.append((test, train))
    return out


def _log(a: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(np.where(a > 0, a, np.nan))


def _day_sums(gain: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-day sum and count of a [T, N] array of per-cell gains (NaN = no cell), after warm-up."""
    g = gain[WARMUP:]
    days = (np.arange(len(g)) + WARMUP) // 24
    ok = np.isfinite(g)
    per_hour_sum = np.where(ok, g, 0.0).sum(axis=1)
    per_hour_n = ok.sum(axis=1)
    n_days = int(days.max()) + 1 if len(days) else 0
    return np.bincount(days, per_hour_sum, n_days), np.bincount(days, per_hour_n, n_days).astype(float)


def bootstrap_interval(groups: list[tuple[np.ndarray, np.ndarray]], *, family: int = FAMILY, alpha: float = ALPHA,
                       resamples: int = RESAMPLES, seed: int = SEED) -> tuple[float, float]:
    """Interval for a pooled mean, from (daily sums, daily counts) per league.

    Whole days are resampled with replacement within each league (blocks of one day, all items
    together), and the pooled mean is the resampled total over the resampled count. The interval
    is two-sided at 1 - alpha / family.
    """
    rng = np.random.default_rng(seed)
    tot = np.zeros(resamples)
    cnt = np.zeros(resamples)
    for sums, counts in groups:
        if len(sums) == 0:
            continue
        idx = rng.integers(0, len(sums), size=(resamples, len(sums)))
        tot += sums[idx].sum(axis=1)
        cnt += counts[idx].sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        means = tot / cnt
    means = means[np.isfinite(means)]
    if len(means) == 0:
        return math.nan, math.nan
    tail = alpha / family / 2
    return float(np.quantile(means, tail)), float(np.quantile(means, 1 - tail))


def forecast_verdict(name: str, scored: list[dict], settings: int, notes: list[str] | None = None) -> Result:
    """Apply the forecasting rule to per-test-league errors.

    Each item of `scored` has `league`, `counts` (whether it may count toward the verdict), and
    [T, N] absolute errors `signal`, `fair` and `last` (NaN where a cell is missing); `setting`
    is the setting chosen on the training leagues. Only cells where all three exist are scored.
    """
    rows, groups = [], {"fair": [], "last": []}
    wins = {"fair": 0, "last": 0}
    used = 0
    totals = {k: [0.0, 0.0] for k in ("signal", "fair", "last")}
    for s in scored:
        ok = np.isfinite(s["signal"]) & np.isfinite(s["fair"]) & np.isfinite(s["last"])
        ok[:WARMUP] = False
        n = int(ok.sum())
        row = {"league": s["league"], "cells": n}
        if "setting" in s:
            row["setting"] = s["setting"]
        for k in ("signal", "fair", "last"):
            row[f"{k} error"] = float(np.mean(s[k][ok])) if n else math.nan
        for base in ("fair", "last"):
            gain = np.where(ok, s[base] - s["signal"], np.nan)
            row[f"cut vs {base}"] = (1 - row["signal error"] / row[f"{base} error"]) if n else math.nan
            if s["counts"] and n:
                groups[base].append(_day_sums(gain))
                wins[base] += row[f"cut vs {base}"] > 0
        if s["counts"] and n:
            used += 1
            for k in totals:
                totals[k][0] += float(np.sum(s[k][ok]))
                totals[k][1] += n
        row["counts toward verdict"] = "yes" if s["counts"] and n else "no"
        rows.append(row)

    numbers: dict = {"test leagues counted": used}
    result = Result(name, THIN, settings, FAMILY, numbers, rows, notes=list(notes or []))
    if used < MIN_TEST_LEAGUES:
        result.notes.append(f"Needs at least {MIN_TEST_LEAGUES} test leagues of {MIN_DAYS}+ days with an earlier "
                            "league to train on; numbers above are for information only.")
        return result
    passed = True
    for base in ("fair", "last"):
        lo, hi = bootstrap_interval(groups[base])
        err_sig = totals["signal"][0] / totals["signal"][1]
        err_base = totals[base][0] / totals[base][1]
        numbers[f"pooled cut vs {base}"] = 1 - err_sig / err_base
        # The interval is on the mean gain per cell; as a share of the baseline's error it reads the same way.
        numbers[f"pooled cut vs {base}, interval"] = (lo / err_base, hi / err_base)
        numbers[f"leagues better than {base}"] = f"{wins[base]} of {used}"
        passed &= wins[base] * 2 > used and lo > 0
    result.verdict = PASS if passed else FAIL
    return result


def _choose(train_scores: dict, settings: tuple) -> object:
    """The setting with the lowest mean error on the training leagues."""
    return min(settings, key=lambda s: train_scores[s])


def _mean_err(err: np.ndarray, *others: np.ndarray) -> tuple[float, int]:
    ok = np.isfinite(err)
    for o in others:
        ok &= np.isfinite(o)
    ok[:WARMUP] = False
    return float(np.sum(err[ok])), int(ok.sum())


def _baselines(price: np.ndarray, value: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Target (log forward VWAP), the desk's fair value and the last print, all in logs."""
    target = pricing.forward_vwap(price, value, HORIZON)
    fair = _log(pricing.fair_value(price, value).fair)
    last = _log(ffill(price))
    return target, fair, last


def _markets(panel: Panel) -> list[tuple[np.ndarray, np.ndarray]]:
    """(price, traded value) per market: the Chaos market, then the Divine market in Chaos."""
    return [(panel["r_price"], panel["r_price"] * panel["r_volume"]),
            (panel["b_price"], panel["b_price"] * panel["b_volume"])]


# ---- two-market --------------------------------------------------------------------------------
def two_market(seasons: list[Season], shares: tuple = TWO_MARKET_SHARES) -> Result:
    """Does one value plus a slow Divine premium predict each market better than that market alone?"""
    t0 = time.perf_counter()
    cache: dict[str, dict] = {}

    def errors(s: Season) -> dict:
        if s.name not in cache:
            (rp, rv), (bp, bv) = _markets(s.panel)
            tr, fr, lr = _baselines(rp, rv)
            tb, fb, lb = _baselines(bp, bv)
            target = np.hstack([tr, tb])
            out = {"fair": np.abs(target - np.hstack([fr, fb])), "last": np.abs(target - np.hstack([lr, lb]))}
            for share in shares:
                tm = pricing.two_market(rp, bp, rv, bv, premium_share=share)
                out[share] = np.abs(target - np.hstack([_log(tm.value), _log(tm.divine)]))
            cache[s.name] = out
        return cache[s.name]

    scored = []
    for test, train in splits(seasons):
        sums = {sh: [0.0, 0] for sh in shares}
        for s in train:
            e = errors(s)
            for sh in shares:
                total, n = _mean_err(e[sh], *(e[x] for x in shares))
                sums[sh][0] += total
                sums[sh][1] += n
        if all(n == 0 for _, n in sums.values()):
            continue
        best = _choose({sh: tot / max(n, 1) for sh, (tot, n) in sums.items()}, shares)
        e = errors(test)
        scored.append({"league": test.name, "counts": test.counts, "setting": best,
                       "signal": e[best], "fair": e["fair"], "last": e["last"]})
    r = forecast_verdict("two-market", scored, len(shares),
                         ["Signal: one value per item plus a Divine-market premium drifting at the chosen share of "
                          "the value's movement; baselines are each market's own fair value and last print."])
    r.seconds = time.perf_counter() - t0
    return r


# ---- premium -----------------------------------------------------------------------------------
def premium_target(panel: Panel) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The Divine premium over the next hours (log), its trailing 24-hour value and its last value."""
    (rp, rv), (bp, bv) = _markets(panel)
    target = pricing.forward_vwap(bp, bv, HORIZON) - pricing.forward_vwap(rp, rv, HORIZON)
    trailing = pricing.leg_premium(rp, bp)
    with np.errstate(divide="ignore", invalid="ignore"):
        gap = np.log(bp / rp)
    last = ffill(np.where(np.isfinite(gap), gap, np.nan))
    return target, trailing, last


def premium_curve(seasons: list[Season]) -> dict[str, np.ndarray]:
    """Per item id, the median over leagues of each league day's median premium: {item: [days]}."""
    by_item: dict[str, list[np.ndarray]] = {}
    for s in seasons:
        (rp, _), (bp, _) = _markets(s.panel)
        with np.errstate(divide="ignore", invalid="ignore"):
            gap = np.log(bp / rp)
        gap = np.where(np.isfinite(gap), gap, np.nan)
        T = len(gap)
        n_days = (T + 23) // 24
        padded = np.full((n_days * 24, gap.shape[1]), np.nan)
        padded[:T] = gap
        daily = _nanmedian(padded.reshape(n_days, 24, -1), axis=1)
        for j, item in enumerate(s.panel.items):
            by_item.setdefault(item, []).append(daily[:, j])
    out = {}
    for item, curves in by_item.items():
        width = max(len(c) for c in curves)
        stack = np.full((len(curves), width), np.nan)
        for k, c in enumerate(curves):
            stack[k, :len(c)] = c
        out[item] = _nanmedian(stack, axis=0)
    return out


def _nanmedian(a: np.ndarray, axis: int) -> np.ndarray:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmedian(a, axis=axis)


def premium(seasons: list[Season]) -> Result:
    """Does a curve of the premium by league day, from earlier leagues, forecast it better than its
    trailing value? The curve uses training leagues only, so it is causal by construction."""
    t0 = time.perf_counter()
    scored = []
    for test, train in splits(seasons):
        curve = premium_curve(train)
        target, trailing, last = premium_target(test.panel)
        T, N = target.shape
        day = np.arange(T) // 24
        signal = np.full((T, N), np.nan)
        for j, item in enumerate(test.panel.items):
            c = curve.get(item)
            if c is not None:
                signal[:, j] = np.where(day < len(c), c[np.minimum(day, len(c) - 1)], np.nan)
        scored.append({"league": test.name, "counts": test.counts, "signal": np.abs(target - signal),
                       "fair": np.abs(target - trailing), "last": np.abs(target - last)})
    r = forecast_verdict("premium", scored, 1,
                         ["Target: the Divine market's premium over the Chaos market across the next 6 hours. "
                          "Baselines: the trailing 24-hour premium (fair) and the last observed premium (last)."])
    r.seconds = time.perf_counter() - t0
    return r


# ---- orderbook ---------------------------------------------------------------------------------
def book_pressure(panel: Panel, lookback: int) -> np.ndarray:
    """[T, 2N] change over `lookback` hours of log(buyers' stock / sellers' stock), per market.

    Buyers' stock is the quote currency on offer (`*_bid_lo`, in Chaos) divided by the last price,
    to count it in items. A zero means the side was not published, not an empty book, so it is
    treated as missing. Uses hours up to t only.
    """
    out = []
    for key, (price, _) in zip(("r", "b"), _markets(panel)):
        bid = panel[f"{key}_bid_lo"]
        ask = panel[f"{key}_stock_lo"]
        last = ffill(price)
        with np.errstate(divide="ignore", invalid="ignore"):
            level = np.log(np.where(bid > 0, bid, np.nan) / last) - np.log(np.where(ask > 0, ask, np.nan))
        change = np.full_like(level, np.nan)
        change[lookback:] = level[lookback:] - level[:-lookback]
        out.append(change)
    return np.hstack(out)


def _residual_fit(x: np.ndarray, y: np.ndarray) -> float:
    """Slope of y on x through the origin, with x capped at +-3 so a few wild hours cannot set it."""
    ok = np.isfinite(x) & np.isfinite(y)
    ok[:WARMUP] = False
    xs = np.clip(x[ok], -3, 3)
    den = float(np.sum(xs * xs))
    return float(np.sum(xs * y[ok]) / den) if den > 0 else 0.0


def orderbook(seasons: list[Season], lookbacks: tuple = ORDERBOOK_LOOKBACKS) -> Result:
    """Do changes in resting stock predict the next hours' price beyond fair value?

    The signal is fair value plus slope * pressure, the slope fitted on training leagues for each
    lookback, and the lookback chosen by its fitted error on the training leagues.
    """
    t0 = time.perf_counter()
    cache: dict[str, dict] = {}

    def parts(s: Season) -> dict:
        if s.name not in cache:
            (rp, rv), (bp, bv) = _markets(s.panel)
            tr, fr, lr = _baselines(rp, rv)
            tb, fb, lb = _baselines(bp, bv)
            cache[s.name] = {"target": np.hstack([tr, tb]), "fair": np.hstack([fr, fb]),
                             "last": np.hstack([lr, lb]),
                             **{k: book_pressure(s.panel, k) for k in lookbacks}}
        return cache[s.name]

    scored = []
    for test, train in splits(seasons):
        fits, errs = {}, {}
        for k in lookbacks:
            xs = np.vstack([parts(s)[k] for s in train])
            ys = np.vstack([parts(s)["target"] - parts(s)["fair"] for s in train])
            fits[k] = _residual_fit(xs, ys)
            total = n = 0
            for s in train:
                p = parts(s)
                pred = p["fair"] + fits[k] * np.clip(np.nan_to_num(p[k]), -3, 3)
                tot_k, n_k = _mean_err(np.abs(p["target"] - pred), p["fair"])
                total, n = total + tot_k, n + n_k
            errs[k] = total / max(n, 1)
        best = _choose(errs, lookbacks)
        p = parts(test)
        signal = p["fair"] + fits[best] * np.clip(np.nan_to_num(p[best]), -3, 3)
        scored.append({"league": test.name, "counts": test.counts, "setting": f"{best} h, slope {fits[best]:+.4f}",
                       "signal": np.abs(p["target"] - signal), "fair": np.abs(p["target"] - p["fair"]),
                       "last": np.abs(p["target"] - p["last"])})
    r = forecast_verdict(
        "orderbook", scored, len(lookbacks),
        ["Pressure: the change in log(buyers' stock in items / sellers' stock), from the hour's lowest "
         "published stock on each side.",
         "GGG's API reference names lowest_stock and highest_stock but does not define them. We read them as "
         "the least and most stock on offer during the hour, for the whole side of the book, as the "
         "backtest's queue model does. That reading is an assumption."])
    r.seconds = time.perf_counter() - t0
    return r


# ---- regime ------------------------------------------------------------------------------------
def regime_alarms(price: np.ndarray, value: np.ndarray, volume: np.ndarray, *, cap: float | None = None,
                  allowance: float | None = None, alarm_at: float | None = None) -> np.ndarray:
    """[T, N] True in hours where the detector raises an alarm. Causal: row t uses rows up to t.

    Each print's surprise against fair value is capped at `cap` standard deviations and summed
    two ways (up and down), less `allowance` per hour. An alarm needs a sum at `alarm_at` and an
    active market: the last `GATE_HOURS` hours traded at least the trailing 7-day median of such
    spans, counted in items (in value, a crash would look like a quiet market). After an alarm
    both sums restart.
    """
    cap = Z_CAP if cap is None else cap
    allowance = ALLOWANCE if allowance is None else allowance
    alarm_at = ALARM_AT if alarm_at is None else alarm_at
    z = np.clip(pricing.fair_value(price, value).z, -cap, cap)
    traded = pricing.trailing_sum(volume, GATE_HOURS)
    typical = rolling(np.where(traded > 0, traded, np.nan), GATE_WINDOW, np.nanmedian, 1)
    active = traded >= np.nan_to_num(typical, nan=np.inf)
    T, N = z.shape
    up, down = np.zeros(N), np.zeros(N)
    alarms = np.zeros((T, N), bool)
    for t in range(T):
        seen = np.isfinite(z[t])
        zt = np.where(seen, z[t], 0.0)
        up = np.where(seen, np.maximum(0.0, up + zt - allowance), up)
        down = np.where(seen, np.maximum(0.0, down - zt - allowance), down)
        fire = ((up >= alarm_at) | (down >= alarm_at)) & active[t]
        alarms[t] = fire
        up = np.where(fire, 0.0, up)
        down = np.where(fire, 0.0, down)
    return alarms


def event_hour(price: np.ndarray) -> int | None:
    """First hour whose price is below half its trailing 24-hour median (of the hours before it)."""
    med = rolling(price[:, None], 24, np.nanmedian, 1)[:, 0]
    prior = np.concatenate([[np.nan], med[:-1]])
    hit = np.flatnonzero(np.isfinite(price) & np.isfinite(prior) & (price < 0.5 * prior))
    return int(hit[0]) if len(hit) else None


def quiet_weeks(price: np.ndarray, value: np.ndarray) -> np.ndarray:
    """[weeks, N] True for quiet item-weeks: printed in half the hours, fair value moved under 20%."""
    fair = ffill(pricing.fair_value(price, value).fair)
    T = len(price)
    weeks = T // 168
    out = np.zeros((weeks, price.shape[1]), bool)
    for w in range(weeks):
        lo, hi = w * 168, (w + 1) * 168
        printed = np.mean(np.isfinite(price[lo:hi]), axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            move = np.abs(np.log(fair[hi - 1] / fair[lo]))
        out[w] = (printed >= QUIET_PRINTED) & (move < QUIET_MOVE)
    return out


def regime(seasons: list[Season], known: tuple = KNOWN_CASES) -> Result:
    """Does the detector flag the known crash early, without crying wolf in quiet markets?"""
    t0 = time.perf_counter()
    rows, quiet_total, false_total = [], 0, 0
    case_rows: list[dict] = []
    for s in seasons:
        p = s.panel
        alarms = regime_alarms(p["price"], p["value"], p["volume"])
        quiet = quiet_weeks(p["price"], p["value"])
        weeks = quiet.shape[0]
        per_week = alarms[: weeks * 168].reshape(weeks, 168, -1).sum(axis=1) if weeks else np.zeros((0, 0))
        false = int(per_week[quiet].sum()) if weeks else 0
        n_quiet = int(quiet.sum())
        row = {"league": s.name, "quiet item-weeks": n_quiet, "alarms in them": false,
               "counts toward verdict": "yes" if s.counts else f"no ({s.why_not or 'under 14 days'})"}
        if s.counts:
            quiet_total += n_quiet
            false_total += false
        rows.append(row)
        for league, item in known:
            if league != s.name:
                continue
            try:
                j = p.index(item)
            except KeyError:
                continue
            ev = event_hour(p["price"][:, j])
            fired = np.flatnonzero(alarms[:, j])
            first = [int(h) for h in fired if ev is not None and ev - EARLY <= h <= ev + LATE]
            case_rows.append({"league": s.name, "item": item, "event hour": ev,
                              "event day": None if ev is None else round(ev / 24, 1),
                              "first alarm in window": first[0] if first else None,
                              "hours before event": None if not first else ev - first[0],
                              "flagged": bool(first), "counts": s.counts})
    rate = false_total / quiet_total if quiet_total else math.nan
    numbers = {"false alarms per quiet item-week": rate, "ceiling": FALSE_ALARM_CEILING,
               "quiet item-weeks counted": quiet_total}
    result = Result("regime", THIN, 1, None, numbers, rows, case_rows,
                    ["Known case: flagged if an alarm falls between 48 hours before and 6 hours after the event "
                     "hour (first hour below half the trailing 24-hour median)."])
    counted_cases = [c for c in case_rows if c["counts"]]
    if not counted_cases or quiet_total == 0:
        if not counted_cases:
            result.notes.append("The known case is not in a league that counts toward the verdict.")
        if quiet_total == 0:
            result.notes.append("No quiet item-weeks in leagues that count.")
    else:
        ok = all(c["flagged"] for c in counted_cases) and rate <= FALSE_ALARM_CEILING
        result.verdict = PASS if ok else FAIL
    result.seconds = time.perf_counter() - t0
    return result


# ---- discovery ---------------------------------------------------------------------------------
def _top(panel: Panel, k: int = TOP_ITEMS) -> list[str]:
    totals = np.nansum(panel["value"], axis=0)
    return [panel.items[i] for i in np.argsort(-totals)[:k] if totals[i] > 0]


def _daily_price(panel: Panel) -> np.ndarray:
    T, N = panel.shape
    n_days = T // 24
    v = np.nan_to_num(panel["value"][: n_days * 24]).reshape(n_days, 24, N).sum(axis=1)
    q = np.nan_to_num(panel["volume"][: n_days * 24]).reshape(n_days, 24, N).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(q > 0, v / q, np.nan)


def simple_ratio(ratio: float, max_term: int = RECIPE_MAX, tolerance: float = RECIPE_TOLERANCE) -> Fraction | None:
    """The simple ratio p/q (p, q at most `max_term`) within `tolerance` of `ratio`, if any."""
    if not (ratio > 0 and math.isfinite(ratio)):
        return None
    best = Fraction(ratio).limit_denominator(max_term)
    if best.numerator == 0 or best.numerator > max_term:
        return None
    return best if abs(math.log(ratio / float(best))) <= math.log(1 + tolerance) else None


def _universe(seasons: list[Season]) -> list[str]:
    seen: dict[str, None] = {}
    for s in seasons:
        for item in _top(s.panel):
            seen.setdefault(item)
    return list(seen)


def _names(seasons: list[Season]) -> dict[str, str]:
    out = {}
    for s in seasons:
        out.update(zip(s.panel.items, s.panel.names))
    return out


def _holds(per_league: dict, key) -> tuple[int, int]:
    """(leagues where it holds, leagues where both items trade) for one pair."""
    rows = per_league.get(key, [])
    return sum(1 for r in rows if r), len(rows)


def recipes(seasons: list[Season]) -> Result:
    """Pairs whose price ratio sits at a fixed simple ratio across leagues: candidates to check in game."""
    t0 = time.perf_counter()
    seasons = [s for s in seasons if s.counts]
    universe = _universe(seasons)
    found: dict[tuple, list] = {}
    for s in seasons:
        daily = _daily_price(s.panel)
        cols = [s.panel._by_id.get(i) for i in universe]
        for a in range(len(universe)):
            for b in range(a + 1, len(universe)):
                ia, ib = cols[a], cols[b]
                if ia is None or ib is None:
                    continue
                lr = np.log(daily[:, ia] / daily[:, ib])
                lr = lr[np.isfinite(lr)]
                if len(lr) < RECIPE_DAYS:
                    continue
                med = float(np.median(lr))
                steady = float(np.median(np.abs(lr - med))) <= RECIPE_MAD
                frac = simple_ratio(math.exp(med)) if steady else None
                found.setdefault((universe[a], universe[b]), []).append(frac)
    names = _names(seasons)
    cands = []
    for (a, b), fracs in found.items():
        for frac in {f for f in fracs if f is not None}:
            held = sum(1 for f in fracs if f == frac)
            if len(fracs) >= MIN_PAIR_LEAGUES and held * 2 > len(fracs):
                cands.append({"pair": f"{names[a]} / {names[b]}", "ratio": f"{frac.numerator}:{frac.denominator}",
                              "held in leagues": f"{held} of {len(fracs)}"})
    r = _discovery("recipes", seasons, cands, ["A ratio p:q means one of the first item is worth p/q of the second. "
                                               "Check in game before trusting any of these."])
    r.seconds = time.perf_counter() - t0
    return r


def _discovery(name: str, seasons: list[Season], cands: list[dict], notes: list[str]) -> Result:
    if len(seasons) < MIN_PAIR_LEAGUES:
        return Result(name, THIN, 1, None, {"leagues": len(seasons)}, notes=notes + [
            f"Needs at least {MIN_PAIR_LEAGUES} leagues of {MIN_DAYS}+ days with a known start."])
    return Result(name, CANDIDATES, 1, None, {"leagues": len(seasons), "candidates": len(cands)},
                  candidates=cands, notes=notes)


def _corr(x: np.ndarray, y: np.ndarray, min_n: int) -> np.ndarray:
    """Pairwise-complete correlation of each column of x with each column of y: [Kx, Ky]."""
    mx, my = np.isfinite(x).astype(float), np.isfinite(y).astype(float)
    x0, y0 = np.nan_to_num(x), np.nan_to_num(y)
    n = mx.T @ my
    sx, sy = x0.T @ my, mx.T @ y0
    sxx, syy = (x0 * x0).T @ my, mx.T @ (y0 * y0)
    sxy = x0.T @ y0
    with np.errstate(divide="ignore", invalid="ignore"):
        cov = sxy / n - (sx / n) * (sy / n)
        vx = sxx / n - (sx / n) ** 2
        vy = syy / n - (sy / n) ** 2
        r = cov / np.sqrt(vx * vy)
    return np.where(n >= min_n, r, np.nan)


def relationships(seasons: list[Season]) -> Result:
    """Leaders and laggards, and baskets that move together: candidates that hold across leagues."""
    t0 = time.perf_counter()
    seasons = [s for s in seasons if s.counts]
    universe = _universe(seasons)
    K = len(universe)
    lead: dict[tuple, list] = {}
    basket: dict[tuple, list] = {}
    for s in seasons:
        p = s.panel
        cols = [p._by_id.get(i) for i in universe]
        present = [k for k, c in enumerate(cols) if c is not None]
        idx = [cols[k] for k in present]
        price, value = p["price"][:, idx], p["value"][:, idx]
        fair = _log(pricing.fair_value(price, value).fair)
        target = pricing.forward_vwap(price, value, HORIZON)
        move = np.full_like(fair, np.nan)
        move[HORIZON:] = fair[HORIZON:] - fair[:-HORIZON]
        ahead = target - fair
        rows = np.arange(WARMUP, len(fair), HORIZON)  # every 6th hour, so windows do not overlap
        lc = _corr(move[rows], ahead[rows], 30)
        n_days = len(fair) // 24
        daily = ffill(fair)[23: n_days * 24: 24]
        dchg = np.diff(daily, axis=0)
        bc = _corr(dchg, dchg, RECIPE_DAYS)
        for a_pos, a in enumerate(present):
            for b_pos, b in enumerate(present):
                if a == b:
                    continue
                key = (universe[a], universe[b])
                if np.isfinite(lc[a_pos, b_pos]):
                    lead.setdefault(key, []).append(float(lc[a_pos, b_pos]))
                if a < b and np.isfinite(bc[a_pos, b_pos]):
                    basket.setdefault(key, []).append(float(bc[a_pos, b_pos]))
    names = _names(seasons)
    cands = []
    for (a, b), cs in lead.items():
        for sign in (1, -1):
            held = sum(1 for c in cs if c * sign >= LEAD_CORR)
            if len(cs) >= MIN_PAIR_LEAGUES and held * 2 > len(cs):
                cands.append({"kind": "leads", "pair": f"{names[a]} -> {names[b]}",
                              "direction": "same way" if sign > 0 else "opposite way",
                              "median correlation": float(np.median(cs)), "held in leagues": f"{held} of {len(cs)}"})
    for (a, b), cs in basket.items():
        held = sum(1 for c in cs if c >= BASKET_CORR)
        if len(cs) >= MIN_PAIR_LEAGUES and held * 2 > len(cs):
            cands.append({"kind": "moves with", "pair": f"{names[a]} + {names[b]}",
                          "median correlation": float(np.median(cs)), "held in leagues": f"{held} of {len(cs)}"})
    cands.sort(key=lambda c: -abs(c["median correlation"]))
    r = _discovery("relationships", seasons, cands,
                   [f"Universe: {K} items, the {TOP_ITEMS} most traded of each league. Lead: A's 6-hour move in fair value against B's next "
                    "6 hours relative to its fair value. Candidates only; check before trusting."])
    r.seconds = time.perf_counter() - t0
    return r


# ---- report only -------------------------------------------------------------------------------
def roll_spread(price: np.ndarray, min_pairs: int = 24) -> tuple[np.ndarray, np.ndarray]:
    """Weekly Roll estimate per item: 2 * sqrt(-cov) of consecutive hourly log changes.

    Returns ([weeks, N] spread, NaN where the covariance is not negative or too few pairs;
    [weeks, N] True where the covariance came out positive, so the estimate does not apply)."""
    y = _log(price)
    d = np.vstack([np.full((1, y.shape[1]), np.nan), np.diff(y, axis=0)])
    weeks = len(d) // 168
    est = np.full((weeks, y.shape[1]), np.nan)
    positive = np.zeros((weeks, y.shape[1]), bool)
    for w in range(weeks):
        seg = d[w * 168:(w + 1) * 168]
        a, b = seg[1:], seg[:-1]
        ok = np.isfinite(a) & np.isfinite(b)
        n = ok.sum(axis=0)
        ma = np.where(ok, a, 0).sum(axis=0) / np.maximum(n, 1)
        mb = np.where(ok, b, 0).sum(axis=0) / np.maximum(n, 1)
        cov = np.where(ok, (a - ma) * (b - mb), 0).sum(axis=0) / np.maximum(n - 1, 1)
        enough = n >= min_pairs
        est[w] = np.where(enough & (cov < 0), 2 * np.sqrt(np.maximum(-cov, 0)), np.nan)
        positive[w] = enough & (cov >= 0)
    return est, positive


def _rank_corr(a: np.ndarray, b: np.ndarray) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 5:
        return math.nan
    ra = np.argsort(np.argsort(a[ok])).astype(float)
    rb = np.argsort(np.argsort(b[ok])).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def spread(seasons: list[Season]) -> Result:
    """Each market's typical bid-ask spread from bounce (Roll), and how steady it is week to week."""
    t0 = time.perf_counter()
    rows = []
    for s in seasons:
        for market, (price, _) in zip(("Chaos", "Divine"), _markets(s.panel)):
            est, positive = roll_spread(price)
            if est.size == 0:
                continue
            per_item = _nanmedian(est, axis=0)
            stability = [_rank_corr(est[w], est[w + 1]) for w in range(len(est) - 1)]
            tried = np.isfinite(est) | positive
            rows.append({
                "league": s.name, "market": market, "items": int(np.isfinite(per_item).sum()),
                "median spread": float(np.nanmedian(per_item)) if np.isfinite(per_item).any() else math.nan,
                "week-to-week rank correlation": float(np.nanmedian(stability)) if stability else math.nan,
                "item-weeks without bounce": float(positive.sum() / max(tried.sum(), 1)),
            })
    r = Result("spread", REPORT if rows else THIN, 1, None, {}, rows, notes=[
        "Spread is a log width (0.02 is about 2%) from the Roll model: bounce between buyers and sellers makes "
        "consecutive hourly changes negatively correlated. Weeks with no bounce give no estimate.",
        "Future work: check these against spreads seen in game (the typed trade check can collect them)."])
    r.seconds = time.perf_counter() - t0
    return r


def queue_vs_share(con, leagues: list[str] | None = None, strategies: tuple[str, ...] | None = None) -> Result:
    """The built-in strategies under both fill models, and how far apart their returns are.

    Leagues come from `search.league_rows` (every challenge league by default) and each backtest runs
    through `search.Runner`, so this is the same league set and the same runs a strategy search sees.
    Nothing is tuned here, so there are no folds to hold out."""
    from vaalstreet import backtest, search

    t0 = time.perf_counter()
    rows = []
    league_names = [r["league"] for r in search.league_rows(con, leagues)]
    for name in strategies or tuple(backtest.STRATEGIES):
        runner = search.Runner(con, name, {})
        for league in league_names:
            out = {m: runner(league, {"fill_model": m}) for m in backtest.FILL_MODELS}
            rows.append({"league": league, "strategy": name,
                         "return, share of volume": out["share"]["return"], "return, queue": out["queue"]["return"],
                         "gap": out["queue"]["return"] - out["share"]["return"],
                         "fills, share": out["share"]["fills"], "fills, queue": out["queue"]["fills"]})
    r = Result("queue-vs-share", REPORT if rows else THIN, 1, None, {}, rows,
               notes=["Return is profit over the starting stake for the whole run. A large gap means the fill "
                      "model, not the strategy, drives the result."])
    r.seconds = time.perf_counter() - t0
    return r


def ladder_rates(con, league: str) -> Result:
    """The ladder's promised Chaos per hour against what the same picks made in last week's replay."""
    from vaalstreet import ladder
    from vaalstreet.desk import Desk

    t0 = time.perf_counter()
    desk = Desk.build(con, league)
    rows = []
    for rung in ladder.ladder(desk):
        sess = rung["session"]
        promised, replayed = sess["expected_per_hour"], sess["replayed_per_hour"]
        against = sess["replayed_vs_divine_per_hour"]
        rows.append({"purse": rung["label"], "picks": len(sess["picks"]), "promised per hour": float(promised),
                     "replayed per hour": float(replayed), "replayed vs Divines per hour": float(against),
                     "replayed / promised": float(replayed / promised) if promised else math.nan,
                     "vs Divines / promised": float(against / promised) if promised else math.nan,
                     "Divines over those hours": sess.get("divines")})
    r = Result("ladder-rates", REPORT if rows else THIN, 1, None, {"league": league}, rows,
               notes=["Promised is what today's order levels promise; replayed is what the same rule made over the "
                      "last week at each pick's own stake, in whole lots; vs Divines is that less what holding "
                      "Divines made over the same hours. One league, as of its latest hour."])
    r.seconds = time.perf_counter() - t0
    return r


# ---- loading from the archive -------------------------------------------------------------------
def seasons_from(con, leagues: list[str] | None = None) -> list[Season]:
    """Challenge leagues (or the ones named), each loaded from its first hour, with whether it counts:
    family "challenge" (from `leagues.classify`), a start inside the archive, and enough days."""
    from vaalstreet import search
    from vaalstreet.leagues import LeagueBook

    known = {r["league"]: r for r in LeagueBook(con).leagues()}
    out = []
    for row in search.league_rows(con, leagues):
        name, info = row["league"], known.get(row["league"])
        days = (row["last_hour"] - row["first_hour"]) // (24 * HOUR)
        if row["family"] != "challenge":
            why = f"family {row['family']}, not challenge"
        elif info is None or info["truncated"]:
            why = "started before the archive"
        elif days < MIN_DAYS:
            why = f"only {days} days"
        else:
            why = ""
        panel = Panel.load(con, name, CORE + LEGS, min_value=MIN_ITEM_VALUE)
        out.append(Season(panel, eligible=not why, why_not=why))
    return out


MEASUREMENTS = ("two-market", "premium", "orderbook", "spread", "regime", "relationships", "recipes",
                "queue-vs-share", "ladder-rates")


def run(con, name: str, leagues: list[str] | None = None) -> Result:
    """Run one measurement against the archive. Items with empty columns make numpy warn about
    all-NaN slices inside the models; those cells are NaN by design, so the warnings are muted."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return _run(con, name, leagues)


def _run(con, name: str, leagues: list[str] | None) -> Result:
    if name == "ladder-rates":
        league = (leagues or [_latest(con)])[0]
        return ladder_rates(con, league)
    if name == "queue-vs-share":
        return queue_vs_share(con, leagues)
    seasons = seasons_from(con, leagues)
    fn = {"two-market": two_market, "premium": premium, "orderbook": orderbook, "spread": spread,
          "regime": regime, "relationships": relationships, "recipes": recipes}[name]
    result = fn(seasons)
    skipped = [f"{s.name} ({s.why_not})" for s in seasons if not s.eligible]
    if skipped:
        result.notes.append("Shown but not counted toward the verdict: " + ", ".join(skipped) + ".")
    return result


def _latest(con) -> str:
    from vaalstreet.leagues import LeagueBook

    rows = LeagueBook(con).leagues()
    if not rows:
        raise LookupError("No challenge league in the archive")
    return rows[0]["league"]
