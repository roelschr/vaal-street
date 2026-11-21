"""Fill calibration: how much of the flow your own orders actually caught, per item and market.

A flip plan assumes you get a fixed share (10% by default) of the units that trade at or beyond
your order's price. Your tracked orders say what really filled and when, so the share can be
measured instead of assumed.

The estimator
-------------
For each order, take the hours it stood: from when you posted it to when it stopped (its last
fill if it filled completely, the time you cancelled or closed it, or now if it is still open).
In each of those hours, the market's traded units count as "eligible" when that hour's average
price was at or beyond your order's price - at or below it for a buy, at or above it for a sell -
which is how the flip plan counts the volume that went through a level. Hours the order stood
only part of count in proportion. For a Divine-market order, the price is compared at that
hour's Divine price, since the two markets are different prices.

The measured share for an item's market is pooled over its orders:

    measured = total units filled / total eligible units

Different orders carry different information. A cancelled order, or one still open, filled what
it filled while it stood: an unbiased sample. An order that filled completely might have taken
more if it had been larger, so it is a lower bound; pooling it in pulls the estimate down, never
up, so the desk errs on the slow side. An order still open counts only once it has stood for
`MIN_HOURS` or filled something, since a fresh order with nothing filled says little.

Few orders are noisy, so the share used is shrunk toward the default:

    used = (n * measured + PRIOR_ORDERS * default) / (n + PRIOR_ORDERS)

with n the number of orders. The default counts as `PRIOR_ORDERS` (3) orders, so one order
moves the share a quarter of the way to what it measured, and ten orders about 77%.

Causality: `calibrate(..., as_of=t)` uses only orders posted before t, eligible volume from
hours before t, and fills recorded before t (from the trades each fill wrote). A plan at hour t
therefore never learns from a fill that came later.
"""

from __future__ import annotations

import numpy as np

from vaalstreet.panel import HOUR, Panel

PRIOR_ORDERS = 3.0
MIN_HOURS = 3.0
MARKETS = {"chaos": "r", "divine": "b"}


def _reading(used: float, default: float) -> str:
    ratio = used / default if default > 0 else 0.0
    if ratio >= 1.15:
        return f"Your orders filled about {ratio:.1f}× faster than the desk assumed."
    if ratio <= 1 / 1.15:
        return f"Your orders filled about {1 / ratio:.1f}× slower than the desk assumed." if ratio > 0 else \
            "Your orders here have not filled yet, so the desk now expects them to fill slowly."
    return "Your orders filled about as fast as the desk assumed."


def order_sample(order: dict, fills: list[tuple[int, float]], panel: Panel, as_of: int) -> dict | None:
    """One order's (filled, eligible units), or None when it carries no information yet.

    `fills` is the order's recorded fills as (time, units). `as_of` is a unix time; nothing at or
    after it is used.
    """
    try:
        i = panel.index(order["item"])
    except KeyError:
        return None
    start = int(order["created"])
    if start >= as_of:
        return None
    known = [(t, u) for t, u in fills if t < as_of]
    if fills:
        filled = sum(u for _, u in known)
        last_fill = max((t for t, _ in known), default=None)
    elif order["filled"] > 0:
        # Fills recorded without a trade: only the order row knows them, and only if it is old news.
        if order["updated"] >= as_of:
            return None
        filled, last_fill = float(order["filled"]), int(order["updated"])
    else:
        filled, last_fill = 0.0, None
    complete = filled >= order["units"] - 1e-9
    if complete:
        end = last_fill if last_fill is not None else int(order["updated"])
    elif order["status"] in ("cancelled", "closed") and order["updated"] < as_of:
        end = int(order["updated"])
    else:
        end = as_of
    end = min(end, as_of)
    if end <= start:
        end = start + 1  # filled within the hour it was posted
    if filled <= 0 and (end - start) / HOUR < MIN_HOURS:
        return None

    hours = panel.hours.astype(np.int64)
    overlap = np.clip(np.minimum(hours + HOUR, end) - np.maximum(hours, start), 0, None) / HOUR
    rows = np.flatnonzero(overlap > 0)
    leg = MARKETS[order["market"]]
    price = panel[f"{leg}_price"][rows, i]
    volume = np.nan_to_num(panel[f"{leg}_volume"][rows, i])
    level = order["currency_units"] / order["item_units"]
    if order["market"] == "divine":
        level = level * panel.bridge[rows]
    with np.errstate(invalid="ignore"):
        through = price <= level if order["side"] == "buy" else price >= level
    eligible = float(np.sum(volume * overlap[rows] * np.nan_to_num(through)))
    return {"filled": float(filled), "eligible": eligible, "complete": complete}


def calibrate(orders: list[dict], fills: dict[int, list[tuple[int, float]]], panel: Panel,
              as_of: int, default: float = 0.1) -> list[dict]:
    """Measured and shrunk share of the flow per (item, market), from your orders.

    `orders` are ledger order rows (all statuses), `fills` maps an order id to its recorded
    fills as (time, units). Returns one row per item and market with at least one usable order.
    """
    pooled: dict[tuple[str, str], dict] = {}
    for order in orders:
        sample = order_sample(order, fills.get(order["id"], []), panel, as_of)
        if sample is None:
            continue
        p = pooled.setdefault((order["item"], order["market"]),
                              {"filled": 0.0, "eligible": 0.0, "orders": 0, "complete": 0})
        p["filled"] += sample["filled"]
        p["eligible"] += sample["eligible"]
        p["orders"] += 1
        p["complete"] += sample["complete"]
    out = []
    for (item, market), p in sorted(pooled.items()):
        if p["eligible"] <= 0:
            continue  # the market never traded through your price: nothing to compare with
        measured = p["filled"] / p["eligible"]
        n = p["orders"]
        used = (n * measured + PRIOR_ORDERS * default) / (n + PRIOR_ORDERS)
        out.append({
            "item": item, "market": market, "orders": n, "complete": p["complete"],
            "filled": round(p["filled"], 4), "eligible": round(p["eligible"], 4),
            "measured": round(measured, 4), "share": round(used, 4), "default": default,
            "reading": _reading(used, default),
        })
    return out


def shares(rows: list[dict]) -> dict[str, dict[str, list]]:
    """The calibration as `Desk.flips` takes it: item -> market -> [share used, orders]."""
    out: dict[str, dict[str, list]] = {}
    for r in rows:
        out.setdefault(r["item"], {})[r["market"]] = [r["share"], r["orders"]]
    return out


def from_ledger(ledger, league: str, panel: Panel, as_of: int, default: float = 0.1) -> list[dict]:
    """`calibrate` on a ledger's orders and the fills their trades recorded."""
    fills: dict[int, list[tuple[int, float]]] = {}
    for tr in ledger.trades(league):
        if tr.get("order_id") is not None:
            fills.setdefault(tr["order_id"], []).append((int(tr["t"]), float(tr["units"])))
    return calibrate(ledger.orders(league, include_done=True), fills, panel, as_of, default)
