"""The live desk: one league's market table, flip plans and arbitrage, as plain dicts.

A `Desk` is built once per league and published hour, then answers every question
the UI or CLI asks from memory. All prices are in the reference currency (Chaos).

Reading a flip plan
-------------------
The exchange has no public order book; what is published is each hour's average
price (VWAP), volume, executed extremes and resting stock per market. The
extremes are polluted by single-unit rounding and joke trades, so plans are built
from where each market's hourly *average* has traded relative to fair value:
volume demonstrably went through those levels. A plan is evidence about where
patient orders fill - check the in-game book before committing size.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from vaalstreet import items as item_meta
from vaalstreet import pricing
from vaalstreet.ingest import ANCHORS
from vaalstreet.ledger import fixed_key, recipe_key
from vaalstreet.panel import CORE, HOUR, LEGS, Panel, ffill, weighted_quantile
from vaalstreet.paths import data_dir

DAY = 24
SWING_WINDOW = 48  # hours of prints used to learn where each market trades relative to fair value
PATIENCE = (0.15, 0.25, 0.35, 0.5)  # share of a market's volume that trades beyond an order level
# A market's hourly average sometimes prints far from fair value because the book was empty and somebody
# paid a whole Divine for three oils. That is not a level a second seller could count on, so order levels
# are never placed more than this far (in log terms, about 1.5x) from fair value.
MAX_SWING = math.log(1.5)  # keep in step with backtest.MAX_SWING
PLAN_LEGS = (("chaos", "r", "r"), ("divine", "b", "b"), ("cross", "r", "b"))  # kind, buy market, sell market
REPLAY_DAYS = 7
REPLAY_STAKE = 1000.0
MAX_SWAPS = 4  # longest round trip `loops` walks; the number of paths grows quickly past this


def _num(x: Any, digits: int = 6) -> float | None:
    """JSON-safe float: NaN/inf become None, values are rounded to significant digits."""
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


def _geo(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Typical level between an hour's lowest and highest stock (zero if either is)."""
    with np.errstate(invalid="ignore"):
        return np.sqrt(np.maximum(lo, 0) * np.maximum(hi, 0))


@dataclass
class Leg:
    """Model state for one side market (item vs Chaos, or item vs Divine)."""

    name: str
    fair: np.ndarray  # [T, N] this leg's own filtered VWAP
    dev: np.ndarray  # [W, N] trailing log(leg VWAP / combined fair value); NaN when the leg did not print
    weight: np.ndarray  # [W, N] units traded in those hours
    flow: np.ndarray  # [N] units traded per hour, trailing day
    active: np.ndarray  # [N] share of trailing-day hours with a print
    age: np.ndarray  # [N] hours since the leg last printed
    low: np.ndarray  # [N] typical executed low over the trailing day (median, reference currency)
    high: np.ndarray
    ask_units: np.ndarray  # [N] item units resting on offer (latest known)
    bid_value: np.ndarray  # [N] reference-currency value bidding for the item (latest known)
    sd: np.ndarray | None = None  # [N] latest sd of log(this leg's fair value): how sure its own price is


def kind_currency(plan: dict, side: str) -> bool:
    """Whether a plan's buy or sell order is denominated in the bridge currency (Divine)."""
    return plan[f"{side}_order"]["currency"] != "Chaos"


def _key_part(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return repr(value)


def _memo(method):
    """Remember a list-of-rows answer on the Desk, keyed by every argument.

    A Desk is built once per published hour and then asked the same questions by several pages,
    so the walk is done once. Arguments such as your conversions and your Gold fees are part of
    the key, so changing them computes afresh. Each call gets its own copy of the rows, so a
    caller that adds fields to them does not change what the next caller sees.
    """
    signature = inspect.signature(method)

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        named = {k: v for k, v in bound.arguments.items() if k != "self"}
        key = (method.__name__, json.dumps(named, sort_keys=True, default=_key_part))
        if key not in self._cache:
            self._cache[key] = method(self, *args, **kwargs)
        return [dict(row) for row in self._cache[key]]

    return wrapper


@dataclass
class Costs:
    """What a plan costs besides Chaos: the Gold its orders take, and your own time.

    Every order on the exchange costs Gold, and GGG does not publish the schedule. `fees` is
    item id -> {"gold_per_unit", "trades"} as learned from your own records (`Ledger.gold_fees`);
    `assumed` is a Gold-per-unit you entered for items with no record yet (`Ledger.costs` builds both from your ledger). A learned fee always
    wins. With neither, the fee is unknown and every Gold figure built from it is None - the plan
    stays on the board, we just cannot price its Gold. We never guess a schedule.

    Gold is charged per unit of the item an order buys or sells, on each order. Item-for-item swaps
    cannot be recorded in the ledger, so for round trips we *assume* each swap costs the fee of the
    item given up, the same as selling it would.

    Time: every order is `minutes_per_order` of your time (an assumption you set; 1 by default).
    A plan is run as one batch of orders per hour of market time, sized to that hour's expected
    fills, so Chaos per hour of your own time = Chaos per market hour x 60 / minutes per batch.
    """

    fees: dict[str, dict] = field(default_factory=dict)
    assumed: float | None = None
    minutes_per_order: float = 1.0

    def fee(self, item_id: str) -> tuple[float | None, dict]:
        """(Gold per unit or None, where it came from)."""
        learned = self.fees.get(item_id)
        if learned and learned.get("gold_per_unit") is not None:
            return float(learned["gold_per_unit"]), {"source": "your trades", "trades": int(learned.get("trades", 0))}
        if self.assumed is not None:
            return float(self.assumed), {"source": "your assumption"}
        return None, {"source": "unknown"}

    def _fields(self, gold: float | None, profit: float | None, passes_per_hour: float | None,
                orders: int, profit_per_hour: float | None, sources: list[dict]) -> dict:
        """Trailing fields shared by every kind of plan. `gold` and `profit` are per one pass."""
        minutes = orders * self.minutes_per_order
        return {
            "gold": _num(gold, 4),
            "gold_per_hour": (_num(gold * passes_per_hour, 4)
                              if gold is not None and passes_per_hour is not None else None),
            # A zero fee would make this infinite: say nothing rather than something silly.
            "chaos_per_1000_gold": _num(1000 * profit / gold, 4) if gold and profit is not None else None,
            "gold_sources": sources,
            "orders": orders, "minutes": _num(minutes, 3),
            "profit_per_own_hour": (_num(profit_per_hour * 60 / minutes, 4)
                                    if minutes > 0 and profit_per_hour is not None else None),
        }

    def flip(self, plan: dict) -> dict:
        """Gold and time for a flip: one pass buys one unit and sells it; two orders a batch."""
        fee, source = self.fee(plan["id"])
        edge = (plan["sell"] or 0) - (plan["buy"] or 0)
        # Units flipped per hour, from the same (possibly bankroll-capped) profit the plan shows.
        units = (plan["profit_per_hour"] or 0) / edge if edge > 0 else plan.get("units_per_hour")
        return plan | self._fields(2 * fee if fee is not None else None, edge, units, 2,
                                   plan["profit_per_hour"], [{"id": plan["id"], **source}])

    def conversion(self, row: dict, chaos_id: str) -> dict:
        """Buy `need` inputs, sell `gives` results; Chaos needs no order on either side.

        A recipe you entered may take several inputs (`inputs`): each is bought with its own order.
        """
        inputs = [(x["id"], x["units"]) for x in row["inputs"]] if row.get("inputs") else [(row["id"], row["need"])]
        sources, orders, gold = [], 0, 0.0
        for item_id, units in inputs:
            if item_id == chaos_id:
                continue
            fee_in, src_in = self.fee(item_id)
            sources.append({"id": item_id, **src_in})
            orders += 1
            gold = None if gold is None or fee_in is None else gold + units * fee_in
        if row["reward_id"] != chaos_id:
            fee_out, src_out = self.fee(row["reward_id"])
            sources.append({"id": row["reward_id"], **src_out})
            orders += 1
            gold = None if gold is None or fee_out is None else gold + row["gives"] * fee_out
        profit = (row["proceeds"] or 0) - (row["cost"] or 0)
        return row | self._fields(gold, profit, row["sets_per_hour"], orders, row["profit_per_hour"], sources)

    def loop(self, row: dict, chaos_id: str | None = None) -> dict:
        """Per unit of the first item: buy it, give each item along the path, sell the last.

        A Divine stop is an order like any other. A swap with a vendor (a price you entered) is not an
        exchange order and costs no Gold, and Chaos handed to a vendor never had to be bought.
        """
        units, gold, sources, orders = 1.0, 0.0, [], 0
        via = row.get("via") or ["swap"] * len(row["rates"])
        for k, step in enumerate(row["path"]):
            last = k == len(row["path"]) - 1
            bought = k == 0 and step["id"] != chaos_id  # the first item is bought on the exchange
            given = last or via[k] != "vendor"  # given up in a swap order, or sold at the end
            charged = (1.0 if bought else 0.0) + (units if given else 0.0)
            orders += int(bought) + int(given)
            if charged:
                fee, source = self.fee(step["id"])
                sources.append({"id": step["id"], **source})
                if fee is None:
                    gold = None
                elif gold is not None:
                    gold += charged * fee
            if not last:
                units *= row["rates"][k] or 0
        profit = (row["buy"] or 0) * (row["edge"] or 0)  # per unit of the first item
        per_day = row.get("profit_per_day")
        passes = per_day / profit / DAY if profit > 0 and per_day is not None else None
        out = self._fields(gold, profit, passes, orders,
                           per_day / DAY if per_day is not None else None, sources)
        out["gold_per_day"] = _num(out["gold_per_hour"] * DAY, 4) if out["gold_per_hour"] is not None else None
        return row | out


def session_plan(plans: list[dict], bankroll: float, gold_budget: float | None = None,
                 max_picks: int = 12) -> dict:
    """The flips to run in one sitting with `bankroll` Chaos, one per item.

    A plan is eligible if one whole lot fits the bankroll and the same rule sold at least twice and
    made money over the last week. Each item gets a quarter of the bankroll, or one whole lot if a lot
    costs more. A stake of S earns margin x min(flow value, S / 2) an hour (the purse turns over at
    most once per buy-then-sell).

    Without a Gold budget, plans are taken by Chaos earned per Chaos staked. With `gold_budget`
    (Gold to spend this session), they are taken by Chaos per 1,000 Gold. Gold is charged when an
    order is posted, so a plan's Gold against the budget is posting its buy and sell orders once at
    the size of its stake: stake / buy price units, times the Gold per unit flipped (both sides).
    A stake is shrunk to what the remaining Gold pays for (skipped if not even one lot fits).
    Plans whose Gold is unknown cannot be ranked that way; they are listed in `gold_unknown`.
    """
    def earns(r: dict, stake: float) -> float:
        flow = r.get("flow_value")
        return r["margin"] * min(flow if flow is not None else math.inf, stake / 2)

    eligible = [r for r in plans if (r.get("lot_cost") or 0) > 0 and r["lot_cost"] <= bankroll
                and r.get("margin") is not None and r.get("replay")
                and (r["replay"].get("return") or 0) > 0 and (r["replay"].get("sales") or 0) >= 2]
    def per_gold(r: dict) -> float | None:
        """Chaos per 1,000 Gold; a plan whose orders cost no Gold at all ranks first."""
        if r.get("gold") == 0:
            return math.inf
        return r.get("chaos_per_1000_gold")

    sized = [(r, s, earns(r, s)) for r in eligible for s in [max(r["lot_cost"], bankroll / 4)]]
    unknown: list[dict] = []
    if gold_budget is not None:
        unknown = [r for r, _, _ in sized if per_gold(r) is None]
        sized = [x for x in sized if per_gold(x[0]) is not None]
        sized.sort(key=lambda x: (-per_gold(x[0]), -x[2]))
    else:
        sized.sort(key=lambda x: (-x[2] / x[1], -x[2]))  # most earned per Chaos tied up
    picks, taken = [], set()
    left, gold_left = bankroll, gold_budget
    for r, stake, _ in sized:
        if len(picks) >= max_picks:
            break
        if r["id"] in taken or r["lot_cost"] > left:
            continue
        stake = min(stake, left)
        posted = None
        if gold_left is not None:
            per_chaos = (r.get("gold") or 0) / r["buy"] if r.get("buy") else 0.0  # Gold per Chaos staked
            posted = stake * per_chaos
            if posted > gold_left + 1e-9:
                # Like Chaos running short: shrink the stake to what the Gold left pays to post.
                stake = gold_left / per_chaos
                if stake < r["lot_cost"]:
                    continue
                posted = gold_left
            gold_left -= posted
        est = earns(r, stake)
        ratio = per_gold(r)
        # Informative only: Gold an hour at this stake's expected fills.
        gold_hour = None if ratio is None else 0.0 if ratio == math.inf else est * 1000 / ratio
        taken.add(r["id"])
        left -= stake
        picks.append({"id": r["id"], "name": r["name"], "category": r.get("category"), "kind": r["kind"],
                      "buy_order": r["buy_order"], "sell_order": r["sell_order"], "margin": r["margin"],
                      "lot_cost": r["lot_cost"], "replay": r["replay"],
                      "chaos_per_1000_gold": r.get("chaos_per_1000_gold"), "gold_sources": r.get("gold_sources"),
                      "minutes": r.get("minutes"),
                      "stake": stake, "est": est, "gold_to_post": posted, "gold_per_hour": gold_hour})
    return {
        "bankroll": bankroll, "gold_budget": gold_budget, "flips": picks,
        "expected_per_hour": sum(p["est"] for p in picks), "deployed": bankroll - left,
        "gold_used": gold_budget - gold_left if gold_budget is not None else None,
        "minutes": sum(p["minutes"] or 0 for p in picks),
        "gold_unknown": [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in unknown],
    }


def _age(price: np.ndarray) -> np.ndarray:
    """Hours since each column last had a value (a large number if it never did)."""
    printed = ~np.isnan(price)
    return np.where(printed.any(axis=0), np.argmax(printed[::-1], axis=0), 10**6)


@dataclass
class Desk:
    league: str
    realm: str
    panel: Panel
    fv: pricing.FairValue
    legs: dict[str, Leg]
    premium: np.ndarray  # [N] latest trailing-median log(Divine leg / Chaos leg)
    meta: dict[str, dict] = field(default_factory=dict)
    cross: list[dict] = field(default_factory=list)  # item-vs-item markets, trailing day
    _replays: dict = field(default_factory=dict, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)  # see `_memo`

    # ------------------------------------------------------------------ build
    @classmethod
    def build(
        cls,
        con: duckdb.DuckDBPyConnection,
        league: str,
        *,
        realm: str = "poe1",
        days: int = 30,
        root: Path | None = None,
    ) -> "Desk":
        last = con.execute("SELECT epoch(max(hour))::BIGINT FROM prices WHERE league = ?", [league]).fetchall()[0][0]
        if last is None:
            raise LookupError(f"No prices for league {league!r}")
        panel = Panel.load(con, league, CORE + LEGS, since=last - days * DAY * HOUR)
        fv = pricing.fair_value(panel["price"], panel["value"])
        # A print is compared with the fair value known *before* it, so a leg cannot explain itself.
        prior = np.vstack([fv.fair[:1], fv.fair[:-1]])[-SWING_WINDOW:]
        legs = {}
        for key, label in (("r", "Chaos"), ("b", "Divine")):
            price, lo, hi, vol = panel[f"{key}_price"], panel[f"{key}_lo"], panel[f"{key}_hi"], panel[f"{key}_volume"]
            leg_fv = pricing.fair_value(price, vol * price)
            with np.errstate(divide="ignore", invalid="ignore"):
                dev = np.log(price[-SWING_WINDOW:] / prior)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                low, high = np.nanmedian(lo[-DAY:], axis=0), np.nanmedian(hi[-DAY:], axis=0)
            legs[key] = Leg(
                label, leg_fv.fair, np.where(np.isfinite(dev), np.clip(dev, -MAX_SWING, MAX_SWING), np.nan),
                np.nan_to_num(vol[-SWING_WINDOW:]),
                pricing.trailing_sum(vol, DAY)[-1] / DAY,
                pricing.activity(price, DAY)[-1], _age(price), low, high,
                ffill(_geo(panel[f"{key}_stock_lo"], panel[f"{key}_stock_hi"]), limit=6)[-1],
                ffill(_geo(panel[f"{key}_bid_lo"], panel[f"{key}_bid_hi"]), limit=6)[-1],
                sd=leg_fv.sd[-1],
            )
        premium = pricing.leg_premium(panel["r_price"][-DAY:], panel["b_price"][-DAY:])[-1]
        ref, bridge = ANCHORS[realm]
        cross = con.execute(
            """
            SELECT item, quote, sum(volume) AS volume, median(volume_quote / volume) AS rate, count(*) AS hours,
                   arg_max(stock_lo, hour) AS item_resting, arg_max(quote_stock_lo, hour) AS quote_resting
            FROM legs
            WHERE league = $league AND hour > to_timestamp($since) AND volume > 0 AND volume_quote > 0
              AND item NOT IN ($ref, $bridge) AND quote NOT IN ($ref, $bridge) AND item < quote
            GROUP BY item, quote
            HAVING count(*) >= 6
            """,
            {"league": league, "since": last - DAY * HOUR, "ref": ref, "bridge": bridge},
        ).fetchall()
        root = root or data_dir()
        return cls(
            league, realm, panel, fv, legs, premium, item_meta.load(root, realm),
            [dict(zip(("item", "quote", "volume", "rate", "hours", "item_resting", "quote_resting"), row)) for row in cross],
        )

    # ------------------------------------------------------------- shortcuts
    @property
    def as_of(self) -> int:
        return int(self.panel.hours[-1])

    @property
    def bridge(self) -> float:
        return float(self.panel.bridge[-1])

    def fair(self, item_id: str) -> float | None:
        """Latest fair value of an item id; the reference currency itself is worth 1."""
        if item_id == ANCHORS[self.realm][0]:
            return 1.0
        try:
            value = float(self.fv.fair[-1, self.panel.index(item_id)])
        except KeyError:
            return None
        return value if math.isfinite(value) else None

    def fair_range(self, item_id: str) -> tuple[float, float, float] | None:
        """Latest (low, fair, high) of an item id: fair value and one sd of its uncertainty either side."""
        if item_id == ANCHORS[self.realm][0]:
            return 1.0, 1.0, 1.0
        try:
            i = self.panel.index(item_id)
        except KeyError:
            return None
        out = tuple(float(a[-1, i]) for a in (self.fv.low, self.fv.fair, self.fv.high))
        return out if all(math.isfinite(x) for x in out) else None

    def _age(self) -> np.ndarray:
        """Hours since each item last printed."""
        return _age(self.panel["price"])

    # ---------------------------------------------------------------- market
    def summary(self) -> dict:
        value = np.nansum(self.panel["value"][-DAY:], axis=0)
        return {
            "league": self.league,
            "as_of": self.as_of,
            "first_hour": int(self.panel.hours[0]),
            "bridge": _num(self.bridge),
            "bridge_change_24h": _num(self.panel.bridge[-1] / self.panel.bridge[max(-DAY - 1, -len(self.panel.bridge))] - 1),
            "items": int((self._age() < DAY).sum()),
            "value_24h": _num(value.sum()),
        }

    def market(self) -> list[dict]:
        p, fv = self.panel, self.fv
        T = len(p.hours)
        fair = fv.fair
        fair_low, fair_high = fv.low[-1], fv.high[-1]
        age = self._age()
        value_24h = np.nansum(p["value"][-DAY:], axis=0)
        active = pricing.activity(p["price"], DAY)[-1]

        def change(back: int) -> np.ndarray:
            """Move over `back` hours between two 3-hour medians, so one odd print cannot fake a trend."""
            if T <= back + 3:
                return np.full(fair.shape[1], np.nan)
            with warnings.catch_warnings(), np.errstate(divide="ignore", invalid="ignore"):
                warnings.simplefilter("ignore", category=RuntimeWarning)
                return np.nanmedian(fair[-3:], axis=0) / np.nanmedian(fair[-3 - back : -back], axis=0) - 1

        chg_24, chg_7d = change(DAY), change(7 * DAY)
        spark_rows = np.arange(max(0, T - 72), T, 3)
        r, b = self.legs["r"], self.legs["b"]
        ask_value = np.nan_to_num(r.ask_units) * fair[-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            imbalance = (r.bid_value - ask_value) / (r.bid_value + ask_value)
        rows = []
        for i, item_id in enumerate(p.items):
            if not math.isfinite(fair[-1, i]):
                continue
            last_row = T - 1 - int(min(age[i], T - 1))
            rows.append({
                "id": item_id,
                "name": p.names[i],
                "category": p.categories[i],
                "price": _num(fair[-1, i]),
                # Fair value one sd either side: where the item's worth plausibly sits right now.
                "fair_low": _num(fair_low[i]), "fair_high": _num(fair_high[i]),
                "last": _num(p["price"][last_row, i]) if age[i] < T else None,
                "age": int(age[i]),
                "z": _num(fv.z[-1, i], 3),
                "change_24h": _num(chg_24[i], 4),
                "change_7d": _num(chg_7d[i], 4),
                "value_24h": _num(value_24h[i]),
                "active": _num(active[i], 3),
                # A market that prints a couple of times a day does not have a price worth quoting.
                "chaos": _num(r.fair[-1, i]) if r.active[i] >= 0.25 and r.age[i] < 6 else None,
                "divine": _num(b.fair[-1, i]) if b.active[i] >= 0.25 and b.age[i] < 6 else None,
                "premium": _num(self.premium[i], 4),
                "imbalance": _num(imbalance[i], 3),
                # The model's split of hourly returns: real movement vs print-to-print bounce (log sd).
                "move": _num(math.sqrt(fv.q[i]), 3), "noise": _num(math.sqrt(fv.r[i]), 3),
                "spark": [_num(v, 4) for v in fair[spark_rows, i]],
            })
        rows.sort(key=lambda row: -(row["value_24h"] or 0))
        return rows

    def series(self, item: str, hours: int | None = None) -> dict:
        """Chart data for one item from the desk window."""
        i = self.panel.index(item)
        p = self.panel
        rows = slice(-hours if hours else None, None)
        cols = {
            "t": p.hours[rows].tolist(),
            "price": p["price"][rows, i], "fair": self.fv.fair[rows, i],
            "fair_low": self.fv.low[rows, i], "fair_high": self.fv.high[rows, i], "value": p["value"][rows, i],
            "bridge": p.bridge[rows],
            "chaos": p["r_price"][rows, i], "chaos_lo": p["r_lo"][rows, i], "chaos_hi": p["r_hi"][rows, i],
            "divine": p["b_price"][rows, i], "divine_lo": p["b_lo"][rows, i], "divine_hi": p["b_hi"][rows, i],
            "chaos_fair": self.legs["r"].fair[rows, i], "divine_fair": self.legs["b"].fair[rows, i],
            "ask_units": _geo(p["r_stock_lo"][rows, i], p["r_stock_hi"][rows, i]),
            "bid_value": _geo(p["r_bid_lo"][rows, i], p["r_bid_hi"][rows, i]),
        }
        out = {k: (v if k == "t" else [_num(x) for x in v]) for k, v in cols.items()}
        out.update(id=p.items[i], name=p.names[i], category=p.categories[i])
        return out

    # ----------------------------------------------------------------- flips
    def _order(self, price: float, in_divine: bool) -> dict:
        """An integer exchange ratio close to `price` (reference currency per item)."""
        unit = self.bridge if in_divine else 1.0
        pay, get = pricing.nice_ratio(price / unit)
        return {"currency": "Divine" if in_divine else "Chaos", "currency_units": pay, "item_units": get,
                "price": _num(pay / get * unit)}

    def _fit(self, plan: dict, bankroll: float, min_margin: float) -> dict | None:
        """Re-cut a plan's orders to whole numbers a purse of `bankroll` can actually post.

        The exchange trades whole lots: "64 decks for 1 Divine" cannot be sold by someone holding
        ten decks. Returns the plan with affordable orders, its margin recomputed from the prices
        those orders really state, and the smallest stake that runs it - or None if nothing fits.
        """
        buy_unit = self.bridge if plan["kind"] == "divine" else 1.0
        sell_unit = self.bridge if plan["kind"] != "chaos" else 1.0
        buy = pricing.fit_ratio(plan["buy"] / buy_unit, bankroll / buy_unit)
        if not buy:
            return None
        buy_price = buy[0] / buy[1] * buy_unit
        sell = pricing.fit_ratio(plan["sell"] / sell_unit, 400, max_items=bankroll / buy_price)
        if not sell:
            return None
        sell_price = sell[0] / sell[1] * sell_unit
        margin = sell_price / buy_price - 1
        if margin < min_margin:
            return None
        flow_value = plan["units_per_hour"] * buy_price
        return plan | {
            "buy": _num(buy_price), "sell": _num(sell_price), "margin": _num(margin, 4),
            "buy_order": {"currency": "Divine" if buy_unit != 1 else "Chaos", "currency_units": buy[0],
                          "item_units": buy[1], "price": _num(buy_price)},
            "sell_order": {"currency": "Divine" if sell_unit != 1 else "Chaos", "currency_units": sell[0],
                           "item_units": sell[1], "price": _num(sell_price)},
            # Enough to post the buy order, and to hold a full sell lot.
            "lot_cost": _num(max(buy[0] * buy_unit, sell[1] * buy_price), 4),
            "flow_value": _num(flow_value, 4),
            "capital": _num(min(plan["capital"], bankroll), 4),
            # The purse turns over at most once per buy-then-sell (two hours at this data's resolution).
            "profit_per_hour": _num(margin * min(flow_value, bankroll / 2), 4),
        }

    @_memo
    def flips(self, share: float = 0.1, min_margin: float = 0.02, min_active: float = 0.5,
              bankroll: float | None = None, costs: Costs | None = None) -> list[dict]:
        """Ranked maker plans. `share` is the fraction of a market's flow you expect to capture.

        Each plan buys on one leg and sells on another (or the same) at levels the
        leg's *hourly average* price has actually reached: "`reach` of the units
        traded over the last two days changed hands in hours when the Chaos market
        averaged this low, and `reach` when the Divine market averaged that high".
        Volume went through those levels, which is what makes them better evidence
        than the published extremes. For every item and leg pair the patience level
        with the highest expected profit is kept.

        With `bankroll` set, every plan is re-cut to whole-number orders that purse can post
        (see `_fit`), plans that cannot be run with it are dropped, and profit is capped by how
        fast so little capital can turn over.
        """
        p = self.panel
        fair = self.fv.fair[-1]
        n_obs = {key: np.sum(~np.isnan(leg.dev), axis=0) for key, leg in self.legs.items()}
        best: dict[tuple[str, int], dict] = {}
        replay = self.replay(share)
        for kind, buy_key, sell_key in PLAN_LEGS:
            buy_leg, sell_leg = self.legs[buy_key], self.legs[sell_key]
            usable = (
                (n_obs[buy_key] >= 12) & (n_obs[sell_key] >= 12) & np.isfinite(fair)
                & (buy_leg.active >= min_active) & (sell_leg.active >= min_active)
                & (buy_leg.age < 6) & (sell_leg.age < 6)
            )
            if not usable.any():
                continue
            for reach in PATIENCE:
                # Volume-weighted: `reach` of the units traded at or beyond each level, so an hour in which
                # three units changed hands at a silly ratio cannot set it.
                buy = fair * np.exp(weighted_quantile(buy_leg.dev.T, buy_leg.weight.T, reach))
                sell = fair * np.exp(weighted_quantile(sell_leg.dev.T, sell_leg.weight.T, 1 - reach))
                with np.errstate(divide="ignore", invalid="ignore"):
                    margin = sell / buy - 1
                # Only the volume that trades beyond a level can fill an order resting there.
                units = share * reach * np.minimum(buy_leg.flow, sell_leg.flow)
                profit = margin * units * buy
                ok = usable & np.isfinite(margin) & (margin >= min_margin) & (units > 0)
                for i in np.flatnonzero(ok):
                    current = best.get((kind, i))
                    if current and current["profit_per_hour"] >= profit[i]:
                        continue
                    with np.errstate(divide="ignore", invalid="ignore"):
                        ask_queue = sell_leg.ask_units[i] / sell_leg.flow[i]
                        bid_queue = buy_leg.bid_value[i] / (buy_leg.flow[i] * buy_leg.fair[-1, i])
                    best[(kind, i)] = {
                        "id": p.items[i], "name": p.names[i], "category": p.categories[i], "kind": kind,
                        "buy": _num(buy[i]), "sell": _num(sell[i]), "margin": _num(margin[i], 4),
                        "reach": reach,
                        "buy_order": self._order(float(buy[i]), kind == "divine"),
                        "sell_order": self._order(float(sell[i]), kind != "chaos"),
                        "units_per_hour": _num(units[i], 4),
                        "profit_per_hour": float(profit[i]),
                        "capital": _num(units[i] * buy[i] * 6, 4),  # about six hours of buying held as inventory
                        "range_low": _num(buy_leg.low[i]), "range_high": _num(sell_leg.high[i]),
                        "bid_queue_hours": _num(bid_queue, 3), "ask_queue_hours": _num(ask_queue, 3),
                        "replay": replay.get((kind, p.items[i])),
                    }
        plans = list(best.values())
        for plan in plans:
            plan["profit_per_hour"] = _num(plan["profit_per_hour"], 4)
            plan["flow_value"] = _num((plan["units_per_hour"] or 0) * (plan["buy"] or 0), 4)
            plan["lot_cost"] = _num(max(
                plan["buy_order"]["currency_units"] * (self.bridge if kind_currency(plan, "buy") else 1.0),
                plan["sell_order"]["item_units"] * (plan["buy"] or 0)), 4)
        if bankroll:
            plans = [fitted for plan in plans if (fitted := self._fit(plan, bankroll, min_margin))]
        plans = [(costs or Costs()).flip(plan) for plan in plans]  # after _fit: Gold follows the capped profit
        plans.sort(key=lambda plan: -(plan["profit_per_hour"] or 0))
        return plans

    def replay(self, share: float = 0.1, days: int = REPLAY_DAYS) -> dict[tuple[str, str], dict]:
        """How each kind of plan would have done on each item over the last `days` days.

        The flip rule is replayed through the backtest simulator with the same stake on every
        item, visiting every four hours. Returns {(kind, item id): {"return": profit as a share
        of the stake, "sales": hours with a sale}}. A plan that looks good now but lost money
        all week is telling you something.
        """
        key = (round(share, 4), days)
        if key in self._replays:
            return self._replays[key]
        from vaalstreet import backtest  # backtest imports nothing from here; keep the dependency one-way

        window = self.panel.window(-(days * DAY + SWING_WINDOW))
        n = len(window.items)
        out: dict[tuple[str, str], dict] = {}
        for kind, buy_leg, sell_leg in PLAN_LEGS:
            sim = backtest.Simulation(window, capital=REPLAY_STAKE * n, participation=share, attention=4)
            sim.run(backtest.Swing(top=n, per_item=1 / n, buy_leg=buy_leg, sell_leg=sell_leg, min_margin=0.02))
            profit = sim.profit()
            for i, item_id in enumerate(window.items):
                if sim.sales[i] or sim.position[i] > 0:
                    out[(kind, item_id)] = {"return": _num(profit[i] / REPLAY_STAKE, 3), "sales": int(sim.sales[i])}
        self._replays[key] = out
        return out

    # ------------------------------------------------------------- arbitrage
    @staticmethod
    def _quotable(leg: Leg, i: int) -> bool:
        """Whether one of an item's markets trades often and recently enough to quote a price from."""
        return bool(leg.active[i] >= 0.25 and leg.age[i] < 6 and math.isfinite(leg.fair[-1, i]))

    def _recipe_chain(self, item_id: str, by_name: dict[str, int], max_steps: int = 3) -> list[tuple[int, int, int, int]]:
        """Follow an item's recipe, then the reward's own recipe, and so on.

        Returns [(need, gives, reward index or -1 for the reference currency, steps)], one entry per
        chain length: 3 essences make 1 of the next tier, so 9 make 1 of the tier after that.
        """
        ref_name = self.meta.get(ANCHORS[self.realm][0], {}).get("name", "Chaos Orb")
        chain, need, gives, current = [], 1, 1, item_id
        for step in range(1, max_steps + 1):
            recipe = self.meta.get(current, {}).get("set")
            if not recipe:
                break
            # Scale up until this step consumes a whole number of what the previous steps produced.
            k = math.lcm(gives, recipe["need"])
            need, gives = need * k // gives, k // recipe["need"] * recipe["gives"]
            if recipe["reward"] == ref_name:
                chain.append((need, gives, -1, step))
                break
            if recipe["reward"] not in by_name:
                break
            j = by_name[recipe["reward"]]
            chain.append((need, gives, j, step))
            current = self.panel.items[j]
        return chain

    @_memo
    def conversions(self, share: float = 0.1, fixed: list[dict] | None = None,
                    costs: Costs | None = None) -> list[dict]:
        """Fixed conversions against what they produce: divination card sets, shards and splinters
        that combine into their item, and three-to-one vendor upgrades of essences and oils -
        including chains of them (nine of one tier for one of the tier two above).

        `fixed` is the conversions you entered (`Ledger.fixed_conversions`); its recipes are priced
        the same way. Vendor prices among them are swaps for `loops`, not conversions."""
        p = self.panel
        by_name = {name: i for i, name in enumerate(p.names)}
        r, quick = self.legs["r"], self.legs["b"]
        fair = self.fv.fair[-1]
        value_24h = np.nansum(p["value"][-DAY:], axis=0)
        age = self._age()
        out = []
        for i, item_id in enumerate(p.items):
            if not math.isfinite(fair[i]) or age[i] >= DAY:
                continue
            # Buying through the Divine market is the quick entry; never assume better than fair value.
            buy_now = float(quick.fair[-1, i]) if self._quotable(quick, i) else float(fair[i])
            for need, gives, j, steps in self._recipe_chain(item_id, by_name):
                if j < 0:
                    reward_fair = reward_sell = 1.0
                elif math.isfinite(fair[j]) and age[j] < DAY:
                    reward_fair = float(fair[j])
                    # Selling into the Chaos market is the quick exit.
                    reward_sell = float(r.fair[-1, j]) if self._quotable(r, j) else reward_fair
                else:
                    continue
                cost, out_fair = need * float(fair[i]), gives * reward_fair
                sets = share * (r.flow[i] + quick.flow[i]) / need
                recipe = self.meta[item_id]["set"]
                out.append({
                    "id": item_id, "name": p.names[i], "need": need, "gives": gives, "steps": steps,
                    "how": recipe.get("how", "set"),
                    "reward": p.names[j] if j >= 0 else recipe["reward"],
                    "reward_id": p.items[j] if j >= 0 else ANCHORS[self.realm][0],
                    "cost": _num(cost), "proceeds": _num(out_fair),
                    "margin": _num(out_fair / cost - 1, 4),
                    # Pay the richer market for the inputs and dump the reward in the cheaper one.
                    "margin_now": _num(gives * min(reward_sell, reward_fair) / (need * max(buy_now, float(fair[i]))) - 1, 4),
                    "sets_per_hour": _num(sets, 4),
                    "profit_per_hour": _num(sets * (out_fair - cost), 4),
                    "value_24h": _num(value_24h[i]),
                    "reward_value_24h": _num(value_24h[j]) if j >= 0 else None,
                    "recipe_key": recipe_key("set", [item_id, p.items[j] if j >= 0 else ANCHORS[self.realm][0]]),
                })
        for recipe in fixed or []:
            if recipe["kind"] == "recipe":
                row = self._your_recipe(recipe, share, value_24h)
                if row:
                    out.append(row)
        out = [(costs or Costs()).conversion(row, ANCHORS[self.realm][0]) for row in out]
        out.sort(key=lambda row: -(row["profit_per_hour"] or 0))
        return out

    def _price(self, item_id: str) -> dict | None:
        """One unit's fair value, its quick buy and sell price, and its trade, in reference currency."""
        if item_id == ANCHORS[self.realm][0]:
            return {"fair": 1.0, "buy": 1.0, "sell": 1.0, "flow": math.inf}
        i = self.panel._by_id.get(item_id)
        if i is None:
            return None
        fair = float(self.fv.fair[-1, i])
        if not math.isfinite(fair) or self._age()[i] >= DAY:
            return None
        r, quick = self.legs["r"], self.legs["b"]
        # As for the built-in recipes: buy through the Divine market, sell into the Chaos market,
        # and never assume better than fair value.
        buy = float(quick.fair[-1, i]) if self._quotable(quick, i) else fair
        sell = float(r.fair[-1, i]) if self._quotable(r, i) else fair
        return {"fair": fair, "buy": max(buy, fair), "sell": min(sell, fair),
                "flow": float(r.flow[i] + quick.flow[i])}

    def _your_recipe(self, recipe: dict, share: float, value_24h: np.ndarray) -> dict | None:
        """A recipe you entered, priced like a built-in one. None while any of its items has no price."""
        inputs = [(item_id, float(units), self._price(item_id)) for item_id, units in recipe["inputs"]]
        reward = self._price(recipe["output"])
        if not reward or any(q is None for _, _, q in inputs):
            return None
        gives = float(recipe["gives"])
        cost = sum(units * q["fair"] for _, units, q in inputs)
        cost_now = sum(units * q["buy"] for _, units, q in inputs)
        out_fair = gives * reward["fair"]
        # The thinnest input sets the pace, as for the built-in recipes; a recipe of Chaos alone is
        # paced by its reward instead.
        sets = share * min(q["flow"] / units for _, units, q in inputs)
        if not math.isfinite(sets):
            sets = share * reward["flow"] / gives
        if not math.isfinite(sets):
            sets = 0.0
        first = inputs[0][0]
        i, j = self.panel._by_id.get(first), self.panel._by_id.get(recipe["output"])
        return {
            "id": first, "name": self._name(first), "need": _num(inputs[0][1]), "gives": _num(gives), "steps": 1,
            "how": "yours", "reward": self._name(recipe["output"]), "reward_id": recipe["output"],
            "cost": _num(cost), "proceeds": _num(out_fair), "margin": _num(out_fair / cost - 1, 4),
            "margin_now": _num(gives * reward["sell"] / cost_now - 1, 4),
            "sets_per_hour": _num(sets, 4), "profit_per_hour": _num(sets * (out_fair - cost), 4),
            "value_24h": _num(value_24h[i]) if i is not None else None,
            "reward_value_24h": _num(value_24h[j]) if j is not None else None,
            "recipe_key": fixed_key("recipe", recipe["inputs"], recipe["output"], gives),
            "inputs": [{"id": item_id, "name": self._name(item_id), "units": _num(units)} for item_id, units, _ in inputs],
            "fixed_id": recipe.get("id"), "note": recipe.get("note", ""),
        }

    @_memo
    def loops(self, min_edge: float = 0.03, min_value: float = 200.0, chaos_only: bool = False,
              share: float = 0.1, swaps: int = 3, fixed: list[dict] | None = None,
              costs: Costs | None = None) -> list[dict]:
        """Round trips through item-for-item markets: buy A, swap A for B (then B for C, ...), sell.

        Each swap uses that market's typical traded rate over the last day; entry is the cheaper of
        an item's own markets and exit the dearer (with `chaos_only`, both are the Chaos market,
        for purses too small to deal in Divines). `edge` is what comes back per unit put in.
        Side markets are thin, so `value_24h` - what actually went through the thinnest swap - matters
        as much as the edge, and `resting_value` says whether orders are waiting there to be taken now.

        Paths have up to `swaps` swaps (at most MAX_SWAPS). Two more kinds of swap join them:

        - Through Divine: sell A in its Divine market and buy C in C's. Divine is only ever a stop in
          the middle, and an item next to it is bought or sold in its Chaos market, because its Divine
          market is already in use. Choosing the dearer market at each end is what the cross flip
          does already, so a path through Divine is kept only when it beats stopping before Divine.
        - Vendor prices you entered (`fixed`, kind "vendor"): a vendor sells X for N of Y, one way and
          at any volume. They add no depth limit; a path of vendor swaps alone is as deep as the
          markets it buys and sells in.

        A path never visits an item twice and always ends somewhere other than where it started, so
        every row is a different trade (A to B and B to A are both listed: they are different trades).
        Paths through Divine are cut down to the best one per item you start with and per item you buy
        with the Divines, after sorting.
        """
        swaps = max(1, min(int(swaps), MAX_SWAPS))
        r, b = self.legs["r"], self.legs["b"]
        index = self.panel._by_id
        ref, divine = ANCHORS[self.realm]
        value_24h = np.nansum(self.panel["value"][-DAY:], axis=0)
        quotes: dict[tuple[str, bool], tuple[float, float] | None] = {}

        def quote(item_id: str, chaos_here: bool = False) -> tuple[float, float] | None:
            """(entry cost, exit proceeds) for one unit, in reference currency."""
            if item_id == ref:
                return (1.0, 1.0)
            key = (item_id, chaos_here)
            if key not in quotes:
                i = index.get(item_id)
                legs = (r,) if chaos_only or chaos_here else (r, b)
                options = [float(leg.fair[-1, i]) for leg in legs if i is not None and self._quotable(leg, i)]
                quotes[key] = (min(options), max(options)) if options else None
            return quotes[key]

        def depth(item_id: str) -> float:
            i = index.get(item_id)
            return math.inf if item_id == ref or i is None else float(value_24h[i])

        # An edge is (item received, units received per unit given, value traded in a day, hours active,
        # value of the received item resting on offer in the latest hour - what a swap could take at once,
        # what kind of swap it is, and the recipe key of a vendor price). A swap thinner than `min_value`
        # cannot carry a path, so it is left out before the walk.
        edges: dict[str, list[tuple]] = {}
        for m in self.cross:  # both directions of every side market
            pa, pb = self.fair(m["item"]), self.fair(m["quote"])
            if not pa or not pb:
                continue
            value = m["volume"] * pa
            if value < min_value:
                continue
            edges.setdefault(m["item"], []).append(
                (m["quote"], m["rate"], value, m["hours"], float(m.get("quote_resting") or 0) * pb, "swap", None))
            edges.setdefault(m["quote"], []).append(
                (m["item"], 1 / m["rate"], value, m["hours"], float(m.get("item_resting") or 0) * pa, "swap", None))
        if not chaos_only:
            for i, item_id in enumerate(self.panel.items):
                if item_id in (ref, divine) or not (self._quotable(b, i) and self._quotable(r, i)):
                    continue
                price = float(b.fair[-1, i])  # Chaos per unit through the Divine market
                value = float(b.flow[i]) * DAY * price
                if value < min_value:
                    continue
                hours = int(round(float(b.active[i]) * DAY))
                # Rates are in Divines, so a pair of Divine swaps comes to the ratio of the two Divine
                # markets and the Divine price itself cancels.
                edges.setdefault(item_id, []).append(
                    (divine, price / self.bridge, value, hours, float(np.nan_to_num(b.bid_value[i])), "divine", None))
                edges.setdefault(divine, []).append(
                    (item_id, self.bridge / price, value, hours, float(np.nan_to_num(b.ask_units[i])) * price, "divine", None))
        for v in fixed or []:
            if v["kind"] == "vendor" and len(v["inputs"]) == 1:
                pay, units = v["inputs"][0]
                edges.setdefault(pay, []).append(
                    (v["output"], float(v["gives"]) / float(units), math.inf, DAY, math.inf, "vendor",
                     fixed_key("vendor", v["inputs"], v["output"], v["gives"])))

        out = []

        def emit(path: list[str], rates: list[float], hows: list[str], keys: list[str],
                 value: float, hours: int, resting: float) -> None:
            start, end = quote(path[0], hows[0] == "divine"), quote(path[-1], hows[-1] == "divine")
            if not start or not end:
                return
            if not math.isfinite(value):  # vendor swaps alone: as deep as the markets at either end
                value, hours = min(depth(path[0]), depth(path[-1])), DAY
            if value < min_value:
                return
            edge = math.prod(rates) * end[1] / start[0] - 1
            if edge < min_edge:
                return
            if "divine" in hows:
                # Stop at the item before Divine and sell it in its dearer market instead: the Divine
                # stop has to earn more than that, or it is only the cross flip in disguise.
                k = path.index(divine)
                before, first = quote(path[k - 1]), quote(path[0])
                if not before or not first:
                    return
                cut = math.prod(rates[: k - 1]) * before[1] / first[0] - 1
                if edge <= cut + 1e-9:
                    return
            give, get = pricing.nice_ratio(1 / rates[0], max_units=200)  # first swap: `give` of A for `get` of B
            out.append({
                "path": [{"id": item_id, "name": self._name(item_id)} for item_id in path],
                "rates": [_num(rate) for rate in rates],
                "buy": _num(start[0]), "sell": _num(end[1]), "edge": _num(edge, 4),
                "first_swap": {"give": give, "get": get},
                "lot_cost": _num(give * start[0], 4),
                "value_24h": _num(value), "hours": int(hours),
                # Orders already waiting on the other side of the thinnest swap, at the latest hour's low point.
                "resting_value": _num(resting, 4),
                # If you took `share` of what went through the thinnest swap at these rates.
                "profit_per_day": _num(share * value * edge, 4),
                "swaps": len(rates), "via": list(hows),
                "recipe_key": recipe_key("loop", path), "vendor_keys": [k for k in keys if k],
            })

        def walk(path, rates, hows, keys, value, hours, resting) -> None:
            for to, rate, v, h, rest, how, key in edges.get(path[-1], ()):
                if to in path or (how == "divine" and path[0] == divine):
                    continue
                step = (path + [to], rates + [rate], hows + [how], keys + [key],
                        min(value, v), min(hours, h), min(resting, rest))
                if not (how == "divine" and to == divine):  # Divine is never where a path ends
                    emit(*step)
                if len(rates) + 1 < swaps:
                    walk(*step)

        for start in list(edges):
            walk([start], [], [], [], math.inf, DAY, math.inf)
        out.sort(key=lambda row: -(row["profit_per_day"] or 0))
        # Divine markets are deep, and one item that is cheap through Divines pairs with every item
        # that is dear through them, so Divine paths would crowd out everything else. Keep the best
        # one per item you start with and per item you buy with the Divines.
        seen: set[tuple[str, str]] = set()
        kept = []
        for row in out:
            if "divine" in row["via"]:
                ids = [x["id"] for x in row["path"]]
                start, bought = ("start", ids[0]), ("bought", ids[ids.index(divine) + 1])
                if start in seen or bought in seen:
                    continue
                seen.update((start, bought))
            kept.append(row)
        return [(costs or Costs()).loop(row, ref) for row in kept]

    def triangles(self, min_value: float = 500.0) -> list[dict]:
        """Item-vs-item markets whose traded rate disagrees with the two items' own prices."""
        out = []
        for m in self.cross:
            pa, pb = self.fair(m["item"]), self.fair(m["quote"])
            if not pa or not pb:
                continue
            rate = m["rate"]  # quote units received per item unit (median hourly VWAP)
            gap = rate * pb / pa - 1  # > 0: the swap market pays more for `item` than it is worth
            value = m["volume"] * pa
            if value < min_value:
                continue
            give, get = (m["item"], m["quote"]) if gap > 0 else (m["quote"], m["item"])
            names = {m["item"]: self._name(m["item"]), m["quote"]: self._name(m["quote"])}
            out.append({
                "give": give, "give_name": names[give], "get": get, "get_name": names[get],
                "rate": _num(rate if gap > 0 else 1 / rate),
                "implied_rate": _num(pa / pb if gap > 0 else pb / pa),
                "edge": _num(abs(gap) if gap > 0 else 1 / (1 + gap) - 1, 4),
                "value_24h": _num(value), "hours": int(m["hours"]),
            })
        out.sort(key=lambda row: -(row["edge"] or 0) * math.sqrt(row["value_24h"] or 0))
        return out

    def _name(self, item_id: str) -> str:
        try:
            return self.panel.names[self.panel.index(item_id)]
        except KeyError:
            return self.meta.get(item_id, {}).get("name") or item_meta.fallback_name(item_id)
