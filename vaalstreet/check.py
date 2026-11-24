"""Typed trade check and stash valuation: answers about one offer, or about what you hold.

Trade check
-----------
The player reads an offer off the in-game order window ("64 items for 1 Divine") and asks
whether taking it is a good idea. The offer is judged against *its own market's* fair value,
not the item's blended one: an item's Chaos market and its Divine market are different prices
(the Divine market is usually dearer: a median 16% on Covens, and up to several times as much for
cheap items), so a fair Chaos-market price looks like a bargain next to the blended value and a
fair Divine-market price looks dear. The blended fair
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

A pasted list
-------------
Typing a stash in line by line is slow, so the list can also be pasted or read from a file
(`read_holdings`): one item per line, the count and the name in either order, or rows copied
from a spreadsheet. Reading is kept apart from writing. The caller first shows what every line
was read as, what it would replace and which lines could not be read, and writes only when the
player says so: a wrong guess about a line must be seen before it changes the list.
"""

from __future__ import annotations

import csv
import difflib
import math
import re

from vaalstreet.desk import LOT_WINDOW, PATIENCE, Desk, _num
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
# The Flips page's three kinds of plan, as a player would say them.
PLAN_WORDS = {"chaos": "The Chaos-market flip", "divine": "The Divine-market flip",
              "cross": "The flip that buys for Chaos and sells for Divines"}
# The stash's sell plan starts between selling fast and holding out: of the flip plans' patience
# levels, the one nearest the middle of their range.
DEFAULT_REACH = 0.35


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
        reasons.append(f"{PLAN_WORDS[level['kind']]} {'buys' if side == 'buy' else 'sells'} at {level['price']:.4g} Chaos"
                       f" each; this offer {status} it.")

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
                           + (": a large pile; the price may not hold for all of it." if hours > THIN_HOURS else "."))
    if not trusted:
        when = "has never traded" if leg.age[i] >= 10**6 else f"last traded {int(leg.age[i])} hours ago"
        reasons.append(f"The {market.title()} market {when} and traded in {leg.active[i]:.0%} of the "
                       "last day's hours, so its value is a rough guess.")
        if verdict == "go":
            verdict = "caution"
    out.update(verdict=verdict, reasons=reasons)
    return out


# A count as a player or a spreadsheet writes it: 45, 2.5, or 2,448 with a mark between the thousands.
_COUNT = r"\d{1,3}(?:[,.'\u00a0\u202f]\d{3})+|\d+(?:\.\d+)?"
_NAME = r"[^,;\t]+?"  # a name never holds a comma, so a row of a CSV file is never taken for one name
_COUNT_FIRST = re.compile(rf"^({_COUNT})\s*(?:[x×*]\s*|\s)({_NAME})$", re.I)
_COUNT_LAST = re.compile(rf"^({_NAME})\s*(?:[:=]|\s[x×*]|\s)\s*({_COUNT})$", re.I)
_ONLY_COUNT = re.compile(rf"^(?:{_COUNT})$")


def _count(text: str) -> float:
    """"2,448" and "2.448" are 2448: a mark followed by exactly three digits splits thousands. "2.5" is 2.5."""
    if re.fullmatch(r"\d+(?:\.\d+)?", text) and not re.fullmatch(r"\d{1,3}\.\d{3}", text):
        return float(text)
    return float(re.sub(r"\D", "", text))


def parse_holdings(text: str) -> tuple[list[dict], list[dict]]:
    """A pasted list as (lines read, lines not read), without looking any name up.

    One item per line, the count and the name in either order: "45 Screaming Essence of Greed",
    "Divine Orb 12", "Divine Orb: 12", "Divine Orb x12", "2,448 Portal Scroll". A row copied from a
    spreadsheet or a CSV file works too: its cells are split at tabs, semicolons or commas, the
    first cell that is text is the name and the first that is a number is the count. Empty lines
    and lines that start with # are skipped. A read line is {"line", "text", "name", "units"}; a
    line not read is {"line", "text", "why"}.
    """
    read, unread = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = units = None
        cells = next((cells for sep in ("\t", ";", ",") if sep in line
                      for cells in [[c.strip() for c in next(csv.reader([line], delimiter=sep))]]
                      if sep != "," or not (_COUNT_FIRST.match(line) or _COUNT_LAST.match(line))), None)
        if cells is not None:
            names = [c for c in cells if c and not _ONLY_COUNT.match(c)]
            counts = [c for c in cells if _ONLY_COUNT.match(c)]
            if names and counts:
                name, units = names[0], _count(counts[0])
        elif match := _COUNT_FIRST.match(line):
            name, units = match[2].strip(), _count(match[1])
        elif match := _COUNT_LAST.match(line):
            name, units = match[1].strip(), _count(match[2])
        if name is None:
            unread.append({"line": number, "text": line[:120], "why": "No count and item name found on this line."})
        else:
            read.append({"line": number, "text": line[:120], "name": name, "units": units})
    return read, unread


def read_holdings(desk: Desk, text: str) -> tuple[dict[str, dict], list[dict]]:
    """A pasted list as holdings: item id -> {"units", "lines"}, and the lines that could not be read.

    Names are looked up as everywhere else on the desk (`resolve`), so a line whose item does not
    trade in the league is not read, and says which names are close. Two lines for one item are
    two stacks of it: their counts add, and `lines` names both.
    """
    read, unread = parse_holdings(text)
    holdings: dict[str, dict] = {}
    for row in read:
        try:
            item = resolve(desk, row["name"])
        except LookupError as err:
            unread.append({"line": row["line"], "text": row["text"], "why": str(err)})
            continue
        entry = holdings.setdefault(item, {"units": 0.0, "lines": []})
        entry["units"] += row["units"]
        entry["lines"].append(row["line"])
    return holdings, sorted(unread, key=lambda row: row["line"])


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


def _sell_in(desk: Desk, i: int, market: str, units: float, reach: float, level: float,
             share: float, source: str) -> dict:
    """One line's sell order in one market at one level, or why there is none."""
    leg = desk.legs[MARKETS[market]]
    word = market.title()
    out: dict = {"market": market}
    if not math.isfinite(level):
        return out | {"why": f"The {word} market has not traded enough in the last two days to set a price."}
    out["level"] = _num(level)
    # As a flip's order: the smallest whole-number ratio near the level, here capped at the units held,
    # since a sell order can never ask for more items than you have.
    order = desk._order(level, market == "divine", max_items=math.floor(units + 1e-9))
    if order is None:
        need = desk._order(level, market == "divine")
        return out | {"why": f"You hold {units:g}; the {word} market's order at this price sells "
                             f"{need['item_units']} at a time." if need else f"Too few to post in the {word} market."}
    exact = order.pop("exact")
    lots = int((units + 1e-9) // order["item_units"])
    posted = lots * order["item_units"]
    # Only the volume that trades at or beyond the level can fill an order resting there, as in a flip plan.
    per_hour = share * reach * float(leg.flow[i])
    hours = posted / per_hour if per_hour > 0 else math.inf
    return out | {
        "order": order, "lots": lots, "units": posted, "left": _num(units - posted),
        # What the order states, not the level: whole-number ratios move it a little either way.
        "value": _num(posted * exact), "currency_total": lots * order["currency_units"],
        "off_level": _num(exact / level - 1, 4),
        "units_per_hour": _num(per_hour, 4), "hours": _num(hours, 3),
        "hours_per_lot": _num(order["item_units"] / per_hour, 3) if per_hour > 0 else None,
        "thin": bool(hours > LOT_WINDOW), "share": _num(share, 4), "share_source": source,
    }


def stash_plan(desk: Desk, holdings: dict[str, float], reach: float = DEFAULT_REACH, min_value: float = 0.0,
               share: float = 0.1, share_by_item: dict[str, dict[str, list]] | None = None) -> dict:
    """A sell order for every line of a stash: the market, the whole-number order, and about how long.

    The level is a flip plan's sell level at the same patience (`Desk._level`): the price at or above
    which `reach` of the market's units traded over the last two days. A higher `reach` sells faster
    and for less. Each line is planned in its Chaos market and its Divine market apart, since those are
    different prices, and the market whose order brings in more for the whole line is recommended. A
    market that does not trade steadily (the bar `stash_value` uses) gets no order.

    The order is cut to the line: units posted are whole lots of its ratio, and the rest is left over.
    Time is the units posted over the units per hour your share of the flow gets at the level, with
    the share your own orders measured for that item and market where there is one (`share_by_item`,
    as `calibrate.shares` builds it). Lines worth less than `min_value` Chaos at fair value are left
    out, and counted in `skipped` so the page can say so.
    """
    if reach not in PATIENCE:
        raise ValueError(f"reach must be one of {', '.join(f'{r:g}' for r in PATIENCE)}.")
    if not (min_value >= 0 and math.isfinite(min_value)):
        raise ValueError("The minimum must be zero or more.")
    ref, bridge_id = ANCHORS[desk.realm]
    levels = {m: desk._level(desk.legs[k], "sell", reach)[1] for m, k in MARKETS.items()}
    shares = {m: desk._shares(share, share_by_item, (m,)) for m in MARKETS}
    rows, skipped = [], {"lines": 0, "value": 0.0}
    total = {"plan": 0.0, "worth": 0.0, "planned_worth": 0.0}
    for item_id, units in holdings.items():
        units = float(units)
        row: dict = {"id": item_id, "name": desk._name(item_id), "units": _num(units)}
        fair = desk.fair(item_id)
        worth = units * fair if fair is not None else None
        row["worth"] = _num(worth)
        if worth is not None:
            total["worth"] += worth
            if worth < min_value:
                skipped["lines"] += 1
                skipped["value"] += worth
                continue
        if item_id == ref:
            rows.append(row | {"why": "Chaos is what the desk counts in; there is nothing to sell it for."})
            continue
        if worth is None:
            rows.append(row | {"why": f"No price in {desk.league}, so no level to sell at."})
            continue
        i = desk.panel.index(item_id)
        markets = {}
        for market, key in MARKETS.items():
            leg = desk.legs[key]
            if item_id == bridge_id and market == "divine":
                continue
            if not (math.isfinite(float(leg.fair[-1, i])) and leg.active[i] >= MIN_ACTIVE and leg.age[i] < MAX_AGE):
                markets[market] = {"market": market, "why": f"The {market.title()} market does not trade steadily."}
                continue
            vec, sources = shares[market]
            markets[market] = _sell_in(desk, i, market, units, reach, float(levels[market][i]),
                                       float(vec[i]), sources.get(i, f"default {share:.0%}"))
        row["chaos"], row["divine"] = markets.get("chaos"), markets.get("divine")
        able = [m for m in markets.values() if "order" in m]
        if not able:
            reasons = [m["why"] for m in markets.values()]
            rows.append(row | {"why": " ".join(reasons) or "No market to sell it in."})
            continue
        best = max(able, key=lambda m: m["value"])
        row["best"] = best["market"]
        other = next((m for m in able if m is not best), None)
        if other:
            row["other"] = other["market"]
            row["best_by"] = _num(best["value"] / other["value"] - 1, 4) if other["value"] else None
        total["plan"] += best["value"]
        total["planned_worth"] += worth
        rows.append(row)
    # The planned lines first, the largest sale first; then the lines that could not be planned.
    rows.sort(key=lambda r: (0, -r[r["best"]]["value"]) if r.get("best") else (1, -(r.get("worth") or 0)))
    return {
        "league": desk.league, "bridge": _num(desk.bridge), "reach": reach, "min_value": min_value,
        "rows": rows, "skipped": {"lines": skipped["lines"], "value": _num(skipped["value"])},
        "total": {k: _num(v) for k, v in total.items()},
    }
