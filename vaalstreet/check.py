"""Typed trade check and stash valuation: answers about one offer, or about what you hold.

Trade check
-----------
The player reads an offer off the in-game order window ("64 items for 1 Divine") and asks
whether taking it is a good idea. The offer is judged against *its own market's* fair value,
not the item's blended one: an item's Chaos market and its Divine market are different prices
(the Divine market is usually several percent dearer), so a fair Chaos-market price looks like
a bargain next to the blended value and a fair Divine-market price looks dear. The blended fair
value and its range are still reported, as the reference most of the desk shows.

The verdict is about the side you would take. A buyer wants the price under the market's value,
a seller wants it over. "go" means the price is on your side of the value by more than the
model's uncertainty; "no-go" means it is on the wrong side by more than that; anything in
between is "caution". A market that has not traded recently has no value worth trusting, so it
never gets more than "caution".

Stash
-----
Each holding is valued at the item's fair value with its range, and at each market's own value,
so the player sees which market pays more for it. Its size is compared with that market's
hourly volume: a holding worth many hours of a thin market cannot be sold at that price without
moving it.
"""

from __future__ import annotations

import difflib
import math
import re

from vaalstreet.desk import Desk, _num
from vaalstreet.ingest import ANCHORS

MARKETS = {"chaos": "r", "divine": "b"}
# The narrowest band counted as "about fair". Whole-number ratios are rarely exactly on a price,
# and every order costs Gold, so a price within 2% of the value is not a clear win either way,
# even when the model is very sure of the value.
MIN_BAND = 0.02
# A market is trusted only if it printed in at least this share of the last day's hours and
# within the last few hours; the same bar decides whether the market table quotes it.
MIN_ACTIVE = 0.25
MAX_AGE = 6
# An offer counts as "at the plan's level" within this, as for open orders in the ledger.
PLAN_TOLERANCE = 0.04
# A holding or an offer bigger than this many hours of its market's volume moves the price.
THIN_HOURS = 3.0


def resolve(desk: Desk, item: str) -> str:
    """The item id for an id or a (case-insensitive) name; a LookupError lists close names."""
    item = item.strip()
    ref, _ = ANCHORS[desk.realm]
    if item == ref or item.casefold() == "chaos orb":
        return ref
    try:
        return desk.panel.items[desk.panel.index(item)]
    except KeyError:
        pass
    close = difflib.get_close_matches(item.casefold(), [n.casefold() for n in desk.panel.names], n=5, cutoff=0.5)
    names = {n.casefold(): n for n in desk.panel.names}
    hint = f" Did you mean: {', '.join(names[c] for c in close)}?" if close else ""
    raise LookupError(f"No item called {item!r} traded in {desk.league}.{hint}")


_RATIO = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:items?)?\s*(?:[:=/]|for|per)\s*(\d+(?:\.\d+)?)\s*(chaos|divines?|c|d)?\s*$", re.I)


def parse_ratio(text: str, market: str | None = None) -> tuple[float, float]:
    """(item units, currency units) from text with the item side first.

    Accepts "64:1", "64 = 1 divine", "64 items for 1 divine" and "1 item = 140 chaos".
    A currency word that contradicts `market` is an error, so a reversed ratio is caught.
    """
    m = _RATIO.match(text or "")
    if not m:
        raise ValueError(f"Cannot read the ratio {text!r}. Give the items first, e.g. \"64:1\" or \"64 items = 1 divine\".")
    items, currency, word = float(m.group(1)), float(m.group(2)), (m.group(3) or "").lower()
    if items <= 0 or currency <= 0:
        raise ValueError("Both sides of the ratio must be more than zero.")
    named = "divine" if word.startswith("d") else "chaos" if word else None
    if named and market and named != market:
        raise ValueError(f"The ratio names {named.title()} but the market is {market.title()}.")
    return items, currency


def _plan_level(plans: list[dict], item_id: str, side: str, market: str) -> dict | None:
    """The best plan's order on this side and market (a cross plan buys in Chaos and sells in Divine)."""
    markets = {"chaos": ("chaos", "chaos"), "divine": ("divine", "divine"), "cross": ("chaos", "divine")}
    for plan in plans:  # best first
        if plan["id"] != item_id:
            continue
        if markets[plan["kind"]][0 if side == "buy" else 1] == market:
            return {"kind": plan["kind"], "order": plan[f"{side}_order"], "price": plan[f"{side}_order"]["price"]}
    return None


def check(desk: Desk, item: str, market: str, side: str, item_units: float, currency_units: float,
          stock: float | None = None, plans: list[dict] | None = None) -> dict:
    """Go / caution / no-go for taking one offer from the order window, with plain-words reasons."""
    market, side = market.lower(), side.lower()
    if market not in MARKETS:
        raise ValueError("Market must be chaos or divine.")
    if side not in ("buy", "sell"):
        raise ValueError("Side must be buy or sell.")
    item_units, currency_units = float(item_units), float(currency_units)
    if not (item_units > 0 and currency_units > 0):
        raise ValueError("Both sides of the ratio must be more than zero.")
    item_id = resolve(desk, item)
    if item_id == ANCHORS[desk.realm][0]:
        raise ValueError("Chaos Orb is the currency the desk counts in; check the item on the other side instead.")
    if item_id == ANCHORS[desk.realm][1] and market == "divine":
        raise ValueError("Divine Orb only trades in the Chaos market.")
    i = desk.panel.index(item_id)
    leg = desk.legs[MARKETS[market]]
    unit = desk.bridge if market == "divine" else 1.0
    price = currency_units / item_units * unit  # Chaos per item, Divine at the current Divine price
    reasons: list[str] = []

    market_fair = float(leg.fair[-1, i])
    trusted = math.isfinite(market_fair) and leg.active[i] >= MIN_ACTIVE and leg.age[i] < MAX_AGE
    if math.isfinite(market_fair):
        reference = market_fair
        sd = float(leg.sd[i]) if leg.sd is not None and math.isfinite(leg.sd[i]) else 0.0
    else:  # this market never traded: fall back on the item's value, which says less about this market
        reference = desk.fair(item_id)
        sd = float(desk.fv.sd[-1, i]) if reference else 0.0
    band = max(sd, MIN_BAND)
    out = {"id": item_id, "name": desk.panel.names[i], "market": market, "side": side,
           "item_units": item_units, "currency_units": currency_units, "price": _num(price),
           "market_fair": _num(reference), "trusted": bool(trusted)}
    if not reference:
        out.update(verdict="no-go", reasons=[f"{out['name']} has no price yet in {desk.league}; nothing to compare with."])
        return out

    gap = math.log(price / reference)  # + means dearer than the market's value
    edge = -gap if side == "buy" else gap  # + means good for you
    out.update(gap=_num(math.exp(gap) - 1, 4), band=_num(math.exp(band) - 1, 4))
    pct = f"{abs(math.exp(gap) - 1):.1%}"
    where = "under" if gap < 0 else "over"
    reasons.append(f"{price:.4g} Chaos each is {pct} {where} the {market.title()} market's value of {reference:.4g} Chaos"
                   f" (it is uncertain by about {math.exp(band) - 1:.1%}).")
    if edge > band:
        verdict = "go"
    elif edge < -band:
        verdict = "no-go"
        reasons.append(f"As a {'buyer' if side == 'buy' else 'seller'} you want the price "
                       f"{'under' if side == 'buy' else 'over'} the value; this one is on the wrong side.")
    else:
        verdict = "caution"
        reasons.append("That is within the uncertainty: about a fair price, no clear edge.")

    fr = desk.fair_range(item_id)
    if fr:
        lo, fair, hi = fr
        out.update(fair=_num(fair), fair_low=_num(lo), fair_high=_num(hi), vs_fair=_num(price / fair - 1, 4))
        reasons.append(f"Against the item's overall value of {fair:.4g} Chaos (range {lo:.4g} to {hi:.4g}) it is "
                       f"{abs(price / fair - 1):.1%} {'under' if price < fair else 'over'}.")

    plans = desk.flips(min_margin=0.0) if plans is None else plans
    level = _plan_level(plans, item_id, side, market)
    if level and level["price"]:
        diff = price / level["price"] - 1
        better = -diff if side == "buy" else diff
        status = "matches" if abs(diff) <= PLAN_TOLERANCE else "beats" if better > 0 else "misses"
        out["plan"] = level | {"status": status}
        reasons.append(f"The {level['kind']} flip plan {side}s at {level['price']:.4g} Chaos; this offer {status} it.")

    flow = float(leg.flow[i]) if math.isfinite(leg.flow[i]) else 0.0
    out.update(volume_per_hour=_num(flow), active=_num(leg.active[i], 3), age=int(min(leg.age[i], 10**6)))
    if flow > 0:
        reasons.append(f"The {market.title()} market traded about {flow:,.0f} per hour over the last day.")
    if stock is not None and float(stock) > 0:
        hours = float(stock) / flow if flow > 0 else None
        out.update(stock=float(stock), stock_hours=_num(hours, 3))
        if hours is None:
            reasons.append(f"The {stock:g} on offer is more than this market has traded in a day.")
        else:
            reasons.append(f"The {stock:g} on offer at this ratio is about {hours:.1f} hours of that volume"
                           + (" - a large pile; the price may not hold for all of it." if hours > THIN_HOURS else "."))
    if not trusted:
        hours = "never" if leg.age[i] >= 10**6 else f"{int(leg.age[i])} hours ago"
        reasons.append(f"The {market.title()} market last traded {hours} and traded in {leg.active[i]:.0%} of the "
                       "last day's hours, so its value is a rough guess.")
        if verdict == "go":
            verdict = "caution"
    out.update(verdict=verdict, reasons=reasons)
    return out


def stash_value(desk: Desk, holdings: dict[str, float]) -> dict:
    """Value each holding at fair value with a range, per market, and against that market's volume."""
    ref, bridge_id = ANCHORS[desk.realm]
    rows, unpriced = [], 0
    total = {"value": 0.0, "low": 0.0, "high": 0.0, "best": 0.0}
    for item_id, units in holdings.items():
        units = float(units)
        row = {"id": item_id, "name": desk._name(item_id), "units": _num(units)}
        fr = desk.fair_range(item_id)
        if not fr:
            unpriced += 1
            rows.append(row | {"value": None, "note": f"No price in {desk.league}."})
            continue
        lo, fair, hi = fr
        row.update(fair=_num(fair), value=_num(units * fair), low=_num(units * lo), high=_num(units * hi))
        total["value"] += units * fair
        total["low"] += units * lo
        total["high"] += units * hi
        markets = {}
        if item_id not in (ref,):
            i = desk.panel.index(item_id)
            for name, key in MARKETS.items():
                leg = desk.legs[key]
                if item_id == bridge_id and name == "divine":
                    continue
                price = float(leg.fair[-1, i])
                if not (math.isfinite(price) and leg.active[i] >= MIN_ACTIVE and leg.age[i] < MAX_AGE):
                    continue
                flow = float(leg.flow[i])
                markets[name] = {"price": _num(price), "value": _num(units * price),
                                 "volume_per_hour": _num(flow), "hours": _num(units / flow, 3) if flow > 0 else None}
        row["chaos"], row["divine"] = markets.get("chaos"), markets.get("divine")
        if markets:
            best = max(markets, key=lambda k: markets[k]["price"])
            row["best"] = best
            other = next((k for k in markets if k != best), None)
            if other:
                row["best_by"] = _num(markets[best]["price"] / markets[other]["price"] - 1, 4)
            row["hours"] = markets[best]["hours"]
            row["thin"] = bool(row["hours"] is None or row["hours"] > THIN_HOURS)
            total["best"] += units * markets[best]["price"]
        else:
            total["best"] += units * fair
        rows.append(row)
    rows.sort(key=lambda r: -(r.get("value") or 0))
    return {
        "league": desk.league, "bridge": _num(desk.bridge), "rows": rows, "unpriced": unpriced,
        # Summing every line's low (and every high) treats all the errors as pointing the same way at once.
        # Real errors partly cancel across items, so the true range of the total is narrower: this one is
        # deliberately wide, the safe side for a player deciding what a stash can fund.
        "total": {k: _num(v) for k, v in total.items()},
    }
