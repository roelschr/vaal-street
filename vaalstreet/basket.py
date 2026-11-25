"""Baskets: what a list of items costs now, and how that cost has moved.

A basket is a named list of items with counts: what one map run uses, a crafting kit, what a
character needs each day. The player wants one number for the whole list and one chart of it.

Cost now is given two ways. At fair value, the desk's best estimate of what each item is worth,
and "buying each item in its cheaper market": an item's Chaos market and its Divine market are
different prices, so a buyer pays the lower of the two. A market counts only if it trades
steadily, the same bar the stash and the Markets table use (`check.MIN_ACTIVE`, `check.MAX_AGE`);
the Divine market's price is already in Chaos at the same hour's Divine price, as every leg is.
An item with a fair value but no steady market is counted at fair value there, and said so.

An item with no price in the league is named and left out of both totals, never counted as zero:
a basket priced as cheaper than it is would be worse than one that says it is incomplete.

Cost over time is the sum of units x the item's fair value at each hour, from the same arrays an
item's own chart draws. Fair value at hour t uses only data up to t, so the series does not look
ahead. Hours before every priced item has a fair value would show a sum of fewer items, so they are
left out, and `since` says from when the cost is complete.
"""

from __future__ import annotations

import math
import warnings

import numpy as np

from vaalstreet.check import MARKETS, MAX_AGE, MIN_ACTIVE
from vaalstreet.desk import DAY, SETTLE_HOURS, Desk, _num
from vaalstreet.ingest import ANCHORS


def _cheaper(desk: Desk, item_id: str) -> tuple[str | None, float | None]:
    """The market that sells the item for less right now, and its price in Chaos; (None, None) if neither
    trades steadily."""
    ref, bridge_id = ANCHORS[desk.realm]
    if item_id == ref:
        return None, 1.0
    i = desk.panel.index(item_id)
    best: tuple[str | None, float | None] = (None, None)
    for name, key in MARKETS.items():
        if item_id == bridge_id and name == "divine":
            continue  # the Divine Orb is not priced in Divines
        leg = desk.legs[key]
        price = float(leg.fair[-1, i])
        if not (math.isfinite(price) and leg.active[i] >= MIN_ACTIVE and leg.age[i] < MAX_AGE):
            continue
        if best[1] is None or price < best[1]:
            best = (name, price)
    return best


def _change(cost: np.ndarray, start: int, back: int) -> float | None:
    """The cost's move over `back` hours, between two 3-hour medians, as the Markets table measures one item.

    None when an hour of either window is incomplete, or when the earlier window starts within
    SETTLE_HOURS of the first complete hour: launch prices are not a level to compare with.
    """
    T = len(cost)
    if T < back + 3 or T - 3 - back < start + SETTLE_HOURS:
        return None
    now, then = cost[-3:], cost[T - 3 - back: T - back]
    if not (np.isfinite(now).all() and np.isfinite(then).all()):
        return None
    return _num(float(np.median(now) / np.median(then) - 1), 4)


def cost(desk: Desk, items: list[tuple[str, float]]) -> dict:
    """A basket's cost now (at fair value and in each item's cheaper market), its changes and its series.

    `items` is [(item id, units), ...]. Every hour of the series is in Chaos; `bridge` is the Divine
    price at each of its hours, so the page can show it in Divines the way an item's chart does.
    """
    ref = ANCHORS[desk.realm][0]
    rows, priced = [], []
    total = cheaper = 0.0
    no_market = 0
    for item_id, units in items:
        units = float(units)
        row = {"id": item_id, "name": desk._name(item_id), "units": _num(units)}
        fair = desk.fair(item_id)
        if fair is None:
            rows.append(row | {"fair": None, "value": None, "note": f"No price in {desk.league}."})
            continue
        market, price = _cheaper(desk, item_id)
        if price is None:
            no_market += 1
            price = fair  # nothing trades steadily to buy from: the fair value is the best guess
        row.update(fair=_num(fair), value=_num(units * fair), market=market, market_price=_num(price),
                   market_value=_num(units * price), currency=item_id == ref)
        total += units * fair
        cheaper += units * price
        priced.append((item_id, units))
        rows.append(row)
    unpriced = [r["name"] for r in rows if r["value"] is None]
    out = {
        "league": desk.league, "items": rows, "unpriced": unpriced, "no_market": no_market,
        "cost": _num(total) if priced else None, "cheaper": _num(cheaper) if priced else None,
        "change_24h": None, "change_7d": None, "since": None, "series": {"t": [], "cost": [], "bridge": []},
    }
    if not priced:
        return out

    # Units x fair value at every hour of the desk's window; Chaos itself is worth 1 at every hour.
    p = desk.panel
    series = np.zeros(len(p.hours))
    for item_id, units in priced:
        series = series + units * (1.0 if item_id == ref else desk.fv.fair[:, p.index(item_id)])
    complete = np.isfinite(series)
    start = int(complete.argmax())  # the latest priced item's first fair value (the last hour always has one)
    keep = complete & (np.arange(len(series)) >= start)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        out.update(change_24h=_change(series, start, DAY), change_7d=_change(series, start, 7 * DAY))
    out["since"] = int(p.hours[start])
    out["series"] = {"t": p.hours[keep].tolist(), "cost": [_num(v) for v in series[keep]],
                     "bridge": [_num(v) for v in p.bridge[keep]]}
    return out
