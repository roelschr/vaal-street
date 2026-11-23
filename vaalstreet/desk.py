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
PLAN_LEGS = (("chaos", "r", "r"), ("divine", "b", "b"), ("cross", "r", "b"))  # kind, buy market, sell market
PLAN_MARKETS = {"chaos": ("chaos", "chaos"), "divine": ("divine", "divine"), "cross": ("chaos", "divine")}
REPLAY_DAYS = 7
REPLAY_STAKE = 1000.0
SESSION_HOURS = 6  # a sitting; a lot bigger than a quarter of the purse must fill within one
# Orders fill in whole lots. A plan whose share of the trade needs longer than this to fill one lot is not
# offered: its levels were read off the last SWING_WINDOW hours, and over those hours its share of what
# traded there would not have made a single lot, so nothing says the order ever fills.
LOT_WINDOW = SWING_WINDOW
# A flip order states the smallest whole-number ratio within this of its level (`pricing.lot_ratio`).
# The levels come from hourly averages that move 5 to 12% in an hour or two, so 2% costs little, and
# it keeps lots small: on Covens it cut the median lot from 5 to 2 times the smallest the markets allow.
RATIO_TOLERANCE = 0.02
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
    value_24h: np.ndarray | None = None  # [N] reference-currency value traded here over the trailing day


# A swap rate this many times off what the two items' own prices imply is joke trades in a thin
# market, not an opportunity: on live data, 1 Vaal Orb "fetched" 5 Scours every hour. Ingest uses
# the same bound to drop a joke leg from an item's price.
OUT_OF_LINE = 3.0


def _out_of_line(ratio: float) -> bool:
    return not (1 / OUT_OF_LINE <= ratio <= OUT_OF_LINE)


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

    Gold is charged per unit of the item an order buys or sells, on each order. The ledger records
    no Gold for item-for-item swaps, so for round trips we *assume* each swap costs the fee of the
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
        # A reward with no exchange id (priced from stash listings) is sold outside the exchange.
        if row["reward_id"] not in (chaos_id, None):
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


def cycle_hours(plan: dict) -> float:
    """Hours one stake takes to buy and sell once under this plan (two if the plan does not say)."""
    return plan.get("cycle_hours") or 2.0


def replayed_rate(replay: dict | None, stake: float, against_divine: bool = True) -> float | None:
    """Chaos an hour a plan's rule made in its replay, scaled to `stake`. None if it was never replayed.

    A replay at the stake itself (`Desk.replay_at`) is taken as it is. One measured on another stake
    (`Desk.replay` uses `REPLAY_STAKE`) is scaled: profit grows with the stake only up to the most
    capital the replay ever tied up, and stays flat above it. That is rough at small stakes, which
    is why the ladder replays at the stake. Against Divines (the default), the whole stake is also measured against holding Divines
    over the same hours, so a rising Divine counts against Chaos that sat idle as well as against
    the flip.
    """
    if not replay or replay.get("return") is None:
        return None
    hours = replay.get("hours") or REPLAY_DAYS * DAY
    measured = replay.get("stake") or REPLAY_STAKE
    profit = replay["return"] * measured
    if not replay.get("at_stake"):
        used = replay.get("capital")
        profit *= min(1.0, stake / used) if used else min(stake, measured) / measured
    if not against_divine:
        return profit / hours
    divines = replay.get("divines") or 0.0
    return ((stake + profit) / (1 + divines) - stake) / hours


def _per_hour(returned: float, divines: float, stake: float, hours: int) -> dict:
    """A replay's result in Chaos per hour on its stake: plain, and against holding Divines.

    A return of +160% sounds like a fortune and is 10 Chaos an hour when the stake was 1,000 Chaos
    and the week had 167 hours. It is the rate on that stake only. A plan's promise
    (`profit_per_hour`) is at whatever capital its markets absorb, which for a deep market is
    hundreds of times the stake, so the two are not the same measure; `replayed_rate` scales a
    replay to a stake.
    """
    return {"per_hour": _num(returned * stake / hours, 4),
            "vs_divine_per_hour": _num(((1 + returned) / (1 + divines) - 1) * stake / hours, 4)}


def lot_hours(plan: dict) -> float:
    """Hours the plan's markets take to fill one whole lot at its share of the flow (inf if it never does)."""
    units = max(plan.get("buy_order", {}).get("item_units") or 1, plan.get("sell_order", {}).get("item_units") or 1)
    per_hour = plan.get("units_per_hour") or 0
    return units / per_hour if per_hour > 0 else math.inf


def session_plan(plans: list[dict], bankroll: float, gold_budget: float | None = None,
                 max_picks: int = 12) -> dict:
    """The flips to run in one sitting with `bankroll` Chaos, one per item.

    Each item gets at most a quarter of the bankroll. A plan whose one whole lot costs more than that
    is eligible only if its markets fill a lot within `SESSION_HOURS` at the plan's share of the
    flow: a lot that takes a day to fill would tie up most of a small purse in one item for the whole
    sitting. Such a pick's stake is that one lot, and it carries `lot_share`, the share of the
    bankroll the lot takes ("one lot is 40% of your purse").

    A plan is eligible if one whole lot fits the bankroll and the same rule, replayed over the last
    week, sold at least twice and, at the stake it would get here, beat holding Divines over the same
    hours (`replayed_rate`; at a league start the Divine rises fast, and a gain in Chaos can still be
    a loss next to it). Plans that made Chaos but trailed Divines are counted in `trailing_divines`,
    and `divines` says what Divines made, so an empty plan can say why. A stake of S earns
    margin x min(flow value, S / cycle hours) an hour: the purse turns over at most once per
    buy-then-sell, and that takes as long as the two markets take to reach the levels.

    Without a Gold budget, plans are taken by Chaos earned per Chaos staked. With `gold_budget`
    (Gold to spend this session), they are taken by Chaos per 1,000 Gold. Gold is charged when an
    order is posted, so a plan's Gold against the budget is posting its buy and sell orders once at
    the size of its stake: stake / buy price units, times the Gold per unit flipped (both sides).
    A stake is shrunk to what the remaining Gold pays for (skipped if not even one lot fits).
    Plans whose Gold is unknown cannot be ranked that way; they are listed in `gold_unknown`.
    """
    def earns(r: dict, stake: float) -> float:
        flow = r.get("flow_value")
        return r["margin"] * min(flow if flow is not None else math.inf, stake / cycle_hours(r))

    quarter = bankroll / 4
    eligible = [r for r in plans if (r.get("lot_cost") or 0) > 0 and r["lot_cost"] <= bankroll
                and (r["lot_cost"] <= quarter or lot_hours(r) <= SESSION_HOURS)
                and r.get("margin") is not None and r.get("replay")
                and (r["replay"].get("sales") or 0) >= 2]
    def per_gold(r: dict) -> float | None:
        """Chaos per 1,000 Gold; a plan whose orders cost no Gold at all ranks first."""
        if r.get("gold") == 0:
            return math.inf
        return r.get("chaos_per_1000_gold")

    sized = [(r, s, earns(r, s)) for r in eligible for s in [max(r["lot_cost"], quarter)]]
    # Judged at the stake it would get: a thin market's replay used only part of its 1,000 Chaos,
    # and the Divine's rise is charged on the whole stake, idle or not.
    trailing = [r for r, s, _ in sized if (r["replay"].get("return") or 0) > 0 and (replayed_rate(r["replay"], s) or 0) <= 0]
    sized = [x for x in sized if (replayed_rate(x[0]["replay"], x[1]) or 0) > 0]
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
                      "lot_cost": r["lot_cost"], "replay": r["replay"], "reach": r.get("reach"),
                      "chaos_per_1000_gold": r.get("chaos_per_1000_gold"), "gold_sources": r.get("gold_sources"),
                      "minutes": r.get("minutes"),
                      "stake": stake, "est": est, "gold_to_post": posted, "gold_per_hour": gold_hour,
                      "lot_share": _num(r["lot_cost"] / bankroll, 3) if r["lot_cost"] > quarter else None,
                      # Last week's replay of this rule, scaled to the stake: in Chaos, and against Divines.
                      "replayed_per_hour": _num(replayed_rate(r["replay"], stake, against_divine=False), 4),
                      "replayed_vs_divine_per_hour": _num(replayed_rate(r["replay"], stake), 4),
                      "share_source": r.get("share_source")})
    return {
        "bankroll": bankroll, "gold_budget": gold_budget, "flips": picks,
        "expected_per_hour": sum(p["est"] for p in picks), "deployed": bankroll - left,
        "replayed_per_hour": sum(p["replayed_per_hour"] or 0 for p in picks),
        "replayed_vs_divine_per_hour": sum(p["replayed_vs_divine_per_hour"] or 0 for p in picks),
        "gold_used": gold_budget - gold_left if gold_budget is not None else None,
        "minutes": sum(p["minutes"] or 0 for p in picks),
        "gold_unknown": [{"id": r["id"], "name": r["name"], "kind": r["kind"]} for r in unknown],
        "trailing_divines": len(trailing),
        "divines": next((r["replay"].get("divines") for r in eligible if r["replay"].get("divines") is not None), None),
    }


def _age(price: np.ndarray) -> np.ndarray:
    """Hours since each column last had a value (a large number if it never did)."""
    printed = ~np.isnan(price)
    return np.where(printed.any(axis=0), np.argmax(printed[::-1], axis=0), 10**6)


# A 24h or 7d change compares with an earlier window; launch prices are wild for about a day, so a
# window that starts less than this many hours after the item's first trade in the league gives no change.
SETTLE_HOURS = 24


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
        as_of: int | None = None,
    ) -> "Desk":
        """Build the desk from the league's last `days` days. With `as_of` (unix seconds), the desk is the
        one that stood at that hour: nothing published after it is read."""
        if as_of is None:
            last = con.execute("SELECT epoch(max(hour))::BIGINT FROM prices WHERE league = ?", [league]).fetchall()[0][0]
        else:
            last = con.execute("SELECT epoch(max(hour))::BIGINT FROM prices WHERE league = ? AND hour <= to_timestamp(?)",
                               [league, int(as_of)]).fetchall()[0][0]
        if last is None:
            raise LookupError(f"No prices for league {league!r}" + ("" if as_of is None else " by that hour"))
        panel = Panel.load(con, league, CORE + LEGS, since=last - days * DAY * HOUR,
                           until=None if as_of is None else last)
        fv = pricing.fair_value(panel["price"], panel["value"])
        # A print is compared with the fair value known *before* it, so a leg cannot explain itself.
        legs = {}
        for key, label in (("r", "Chaos"), ("b", "Divine")):
            price, lo, hi, vol = panel[f"{key}_price"], panel[f"{key}_lo"], panel[f"{key}_hi"], panel[f"{key}_volume"]
            leg_fv = pricing.fair_value(price, vol * price)
            dev = pricing.leg_deviation(price, fv.fair, leg_fv.fair)[-SWING_WINDOW:]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                low, high = np.nanmedian(lo[-DAY:], axis=0), np.nanmedian(hi[-DAY:], axis=0)
            legs[key] = Leg(
                label, leg_fv.fair, dev,
                np.nan_to_num(vol[-SWING_WINDOW:]),
                pricing.trailing_sum(vol, DAY)[-1] / DAY,
                pricing.activity(price, DAY)[-1], _age(price), low, high,
                ffill(_geo(panel[f"{key}_stock_lo"], panel[f"{key}_stock_hi"]), limit=6)[-1],
                ffill(_geo(panel[f"{key}_bid_lo"], panel[f"{key}_bid_hi"]), limit=6)[-1],
                sd=leg_fv.sd[-1], value_24h=np.nansum((vol * price)[-DAY:], axis=0),
            )
        premium = pricing.leg_premium(panel["r_price"][-DAY:], panel["b_price"][-DAY:])[-1]
        ref, bridge = ANCHORS[realm]
        cross = con.execute(
            """
            SELECT item, quote, sum(volume) AS volume, median(volume_quote / volume) AS rate, count(*) AS hours,
                   arg_max(stock_lo, hour) AS item_resting, arg_max(quote_stock_lo, hour) AS quote_resting
            FROM legs
            WHERE league = $league AND hour > to_timestamp($since) AND hour <= to_timestamp($last) AND volume > 0 AND volume_quote > 0
              AND item NOT IN ($ref, $bridge) AND quote NOT IN ($ref, $bridge) AND item < quote
            GROUP BY item, quote
            HAVING count(*) >= 6
            """,
            {"league": league, "since": last - DAY * HOUR, "last": last, "ref": ref, "bridge": bridge},
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

        traded = np.isfinite(p["price"])
        first_print = np.where(traded.any(axis=0), traded.argmax(axis=0), T)

        def change(back: int) -> np.ndarray:
            """Move over `back` hours between two 3-hour medians, so one odd print cannot fake a trend.

            No change when the earlier window starts within SETTLE_HOURS of the item's first trade in
            the league (or before it): launch prices are not a level to compare with, and on Covens
            day 7 they made a 7-day change read +66,700%.
            """
            if T < back + 3:
                return np.full(fair.shape[1], np.nan)
            with warnings.catch_warnings(), np.errstate(divide="ignore", invalid="ignore"):
                warnings.simplefilter("ignore", category=RuntimeWarning)
                out = np.nanmedian(fair[-3:], axis=0) / np.nanmedian(fair[T - 3 - back : T - back], axis=0) - 1
            out[first_print + SETTLE_HOURS > T - 3 - back] = np.nan
            return out

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
    def _order(self, price: float, in_divine: bool, max_currency: float = math.inf,
               max_items: float = math.inf) -> dict | None:
        """The order to post for a level of `price` Chaos per item: the smallest whole-number ratio
        within `RATIO_TOLERANCE` of it, in Chaos or in Divines. The caps are in that currency's units
        and in items; None when nothing fits them."""
        unit = self.bridge if in_divine else 1.0
        pair = pricing.lot_ratio(price / unit, RATIO_TOLERANCE, max_currency, max_items)
        if pair is None:
            return None
        pay, get = pair
        return {"currency": "Divine" if in_divine else "Chaos", "currency_units": pay, "item_units": get,
                "price": _num(pay / get * unit), "exact": pay / get * unit}

    def _fit(self, plan: dict, bankroll: float, min_margin: float) -> dict | None:
        """Re-cut a plan's orders to whole numbers a purse of `bankroll` can actually post.

        The exchange trades whole lots: "64 decks for 1 Divine" cannot be sold by someone holding
        ten decks. Returns the plan with affordable orders, its margin recomputed from the prices
        those orders really state, and the smallest stake that runs it - or None if nothing fits.
        The orders are cut again from the plan's levels, not from its uncut orders, so rounding
        never piles on rounding.
        """
        buy_unit = self.bridge if plan["kind"] == "divine" else 1.0
        buy = self._order(plan["buy_level"], plan["kind"] == "divine", max_currency=bankroll / buy_unit)
        if not buy:
            return None
        buy_price = buy.pop("exact")
        sell = self._order(plan["sell_level"], plan["kind"] != "chaos", max_items=bankroll / buy_price)
        if not sell:
            return None
        sell_price = sell.pop("exact")
        margin = sell_price / buy_price - 1
        if margin < min_margin:
            return None
        # The re-cut orders can have other lots than the uncut plan's, so their time is worked out again.
        lot = max(buy["item_units"], sell["item_units"])
        hours_per_lot = lot / plan["units_per_hour"] if plan["units_per_hour"] else math.inf
        if hours_per_lot > LOT_WINDOW:
            return None
        flow_value = plan["units_per_hour"] * buy_price
        fitted = {"lot_units": lot, "hours_per_lot": _num(hours_per_lot, 3)}
        return plan | fitted | {"slow": self._slow(plan | fitted)} | {
            "buy": _num(buy_price), "sell": _num(sell_price), "margin": _num(margin, 4),
            "buy_order": buy, "sell_order": sell,
            # Enough to post the buy order, and to hold a full sell lot.
            "lot_cost": _num(max(buy["currency_units"] * buy_unit, sell["item_units"] * buy_price), 4),
            "flow_value": _num(flow_value, 4),
            "capital": _num(min(plan["capital"], bankroll), 4),
            # The purse turns over at most once per buy-then-sell, which takes as long as it takes
            # the two markets' hourly averages to reach the levels.
            "profit_per_hour": _num(margin * min(flow_value, bankroll / cycle_hours(plan)), 4),
        }

    @_memo
    def flips(self, share: float = 0.1, min_margin: float = 0.02, min_active: float = 0.5,
              bankroll: float | None = None, costs: Costs | None = None,
              share_by_item: dict[str, dict[str, list]] | None = None) -> list[dict]:
        """Ranked maker plans. `share` is the fraction of a market's flow you expect to capture.

        `share_by_item` overrides it where your own orders measured it: item -> market ("chaos" or
        "divine") -> [share, orders], as `calibrate.shares` builds it. A plan uses the smaller of
        its buy market's and sell market's shares, since the slower side sets the pace, and says
        where its share came from in `share_source`.

        Each plan buys on one leg and sells on another (or the same) at levels the
        leg's *hourly average* price has actually reached: "`reach` of the units
        traded over the last two days changed hands in hours when the Chaos market
        averaged this low, and `reach` when the Divine market averaged that high".
        Volume went through those levels, which is what makes them better evidence
        than the published extremes. For every item and leg pair the patience level
        with the highest expected profit is kept.

        Each level is posted as the smallest whole-number order within `RATIO_TOLERANCE` of it
        (`buy_order`, `sell_order`), and the plan's `buy`, `sell`, margin and profit are the prices
        those orders state; the unrounded levels stay in `buy_level` and `sell_level`. How fast the
        plan fills is still read at the level: rounding either way moves an order a little more or
        a little less patient, and over the plans that evens out.

        Orders fill in multiples of their ratio, so each plan says how long one lot takes. A
        plan's lot is the larger of its two orders' item counts (`lot_units`: "7 Chaos for 3" is
        three items, "1 Divine for 40" is forty), and `hours_per_lot` is that lot divided by the
        units the plan takes per hour, as `lot_hours` and the session plan count it. A level
        slower than `LOT_WINDOW` is not a plan, and that is decided per patience level, so a more
        patient level cannot win on a margin it has no trade to back. A plan slower than one sitting (`SESSION_HOURS`) carries `slow`, a sentence
        naming its thinner market and what that market traded in the last day
        (`slow_market`, `slow_market_value_24h`). The queue of offers ahead of you is shown
        (`ask_queue_hours`) but not counted here: the exchange does not publish at what prices
        that stock rests.

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
            share_vec, sources = self._shares(share, share_by_item, PLAN_MARKETS[kind])
            thin_is_buy = buy_leg.flow < sell_leg.flow
            for reach in PATIENCE:
                # Volume-weighted: `reach` of the units traded at or beyond each level, so an hour in which
                # three units changed hands at a silly ratio cannot set it.
                low = weighted_quantile(buy_leg.dev.T, buy_leg.weight.T, reach)
                high = weighted_quantile(sell_leg.dev.T, sell_leg.weight.T, 1 - reach)
                buy, sell = fair * np.exp(low), fair * np.exp(high)
                # Only the volume that trades beyond a level can fill an order resting there.
                units = share_vec * reach * np.minimum(buy_leg.flow, sell_leg.flow)
                with np.errstate(divide="ignore", invalid="ignore"):
                    margin = sell / buy - 1
                # How long one stake takes to go round: wait for an hour whose average reaches the buy,
                # then for one that reaches the sell. Hours, not volume: a purse smaller than the flow
                # turns over only as often as the levels are reached.
                # (Against the quantile itself: hours clipped onto a level sit exactly on it.)
                with np.errstate(invalid="ignore"):
                    hit_buy = np.mean(buy_leg.dev <= low + 1e-12, axis=0)
                    hit_sell = np.mean(sell_leg.dev >= high - 1e-12, axis=0)
                floor = 1 / len(buy_leg.dev)
                cycle = 1 / np.maximum(hit_buy, floor) + 1 / np.maximum(hit_sell, floor)
                # The patience level is chosen on what the volume shows at the level itself. Choosing on the
                # posted orders would favour whichever level happened to round in your favour, and credit
                # it with the fills of the level it no longer sits at.
                level_profit = margin * units * buy
                ok = usable & np.isfinite(margin) & (margin >= min_margin) & (units > 0)
                for i in np.flatnonzero(ok):
                    current = best.get((kind, i))
                    if current and current["level_profit"] >= level_profit[i]:
                        continue
                    # The orders it would post must clear the margin too, and fill one lot in time. An order
                    # fills in multiples of its ratio, so the larger of the two orders' item counts is one
                    # lot, and a level that cannot fill one within `LOT_WINDOW` is not a plan.
                    buy_order = self._order(float(buy[i]), kind == "divine")
                    sell_order = self._order(float(sell[i]), kind != "chaos")
                    buy_price, sell_price = buy_order.pop("exact"), sell_order.pop("exact")
                    posted_margin = sell_price / buy_price - 1
                    lot = max(buy_order["item_units"], sell_order["item_units"])
                    hours_per_lot = lot / units[i]
                    if posted_margin < min_margin or hours_per_lot > LOT_WINDOW:
                        continue
                    profit = posted_margin * units[i] * buy_price
                    with np.errstate(divide="ignore", invalid="ignore"):
                        ask_queue = sell_leg.ask_units[i] / sell_leg.flow[i]
                        bid_queue = buy_leg.bid_value[i] / (buy_leg.flow[i] * buy_leg.fair[-1, i])
                    best[(kind, i)] = {
                        "id": p.items[i], "name": p.names[i], "category": p.categories[i], "kind": kind,
                        "buy": _num(buy_price), "sell": _num(sell_price), "margin": _num(posted_margin, 4),
                        "buy_level": _num(buy[i]), "sell_level": _num(sell[i]),
                        "reach": reach,
                        "buy_order": buy_order, "sell_order": sell_order,
                        "units_per_hour": _num(units[i], 4),
                        "lot_units": lot, "hours_per_lot": _num(hours_per_lot, 3),
                        "slow_market": (buy_leg if thin_is_buy[i] else sell_leg).name,
                        "slow_market_value_24h": _num((buy_leg if thin_is_buy[i] else sell_leg).value_24h[i], 4),
                        "cycle_hours": _num(cycle[i], 3),
                        "profit_per_hour": float(profit), "level_profit": float(level_profit[i]),
                        "share": _num(share_vec[i], 4), "share_source": sources.get(i, f"default {share:.0%}"),
                        "capital": _num(units[i] * buy_price * 6, 4),  # about six hours of buying held as inventory
                        "range_low": _num(buy_leg.low[i]), "range_high": _num(sell_leg.high[i]),
                        "bid_queue_hours": _num(bid_queue, 3), "ask_queue_hours": _num(ask_queue, 3),
                        "replay": replay.get((kind, p.items[i], reach)),
                    }
        plans = list(best.values())
        r_fair, b_fair = self.legs["r"].fair[-1], self.legs["b"].fair[-1]
        for plan in plans:
            if plan["kind"] == "cross":
                # The two markets are real, but a Divine market far above the Chaos market is a thin
                # crowd paying a lot per lot: the plan fills rarely, and its promise is the least reliable.
                i = p._by_id[plan["id"]]
                with np.errstate(divide="ignore", invalid="ignore"):
                    gap = float(b_fair[i] / r_fair[i])
                plan["divine_gap"] = _num(gap, 3)
                plan["warning"] = (f"The Divine market pays {gap:.0f}x the Chaos market here; it fills rarely."
                                   if math.isfinite(gap) and gap > OUT_OF_LINE else None)
            plan["slow"] = self._slow(plan)
            del plan["level_profit"]  # chose the patience level; the posted orders' profit is the one shown
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

    def _slow(self, plan: dict) -> str | None:
        """A sentence for a plan whose one lot takes longer than a sitting to fill, or None."""
        hours = plan.get("hours_per_lot")
        if hours is None or hours <= SESSION_HOURS:
            return None
        market, value = plan["slow_market"], plan.get("slow_market_value_24h") or 0.0
        amount = value / self.bridge if market == "Divine" else value
        traded = f"{amount:,.0f}" if amount >= 9.95 else f"{amount:.2g}"
        unit = "Chaos" if market == "Chaos" else "Divine" if traded == "1" else "Divines"
        # Just past a sitting must not read "every 6 hours"; one decimal place under ten hours.
        every = f"{hours:.0f}" if hours >= 9.95 else f"{hours:.1f}"
        return (f"Slow: at your share of the trade, one lot fills about every {every} hours. "
                f"Its {market} market traded about {traded} {unit} in the last day.")

    def _shares(self, share: float, share_by_item: dict | None,
                markets: tuple[str, str]) -> tuple[np.ndarray, dict[int, str]]:
        """Per-item share of the flow for a plan buying in one market and selling in another."""
        out = np.full(len(self.panel.items), float(share))
        sources: dict[int, str] = {}
        for item, by_market in (share_by_item or {}).items():
            i = self.panel._by_id.get(item)
            if i is None:
                continue
            sides = {m: (float(by_market[m][0]), int(by_market[m][1])) if m in by_market else (float(share), 0)
                     for m in dict.fromkeys(markets)}
            if not any(n for _, n in sides.values()):
                continue
            # The slower side sets the pace, so the label names the side whose share is used.
            value = min(v for v, _ in sides.values())
            used = [m for m, (v, n) in sides.items() if v == value and n]
            out[i] = value
            if used:
                n = sum(sides[m][1] for m in used)
                sources[i] = f"your fills, {n} order{'s' if n != 1 else ''}"
            else:
                slow = next(m for m, (v, _) in sides.items() if v == value)
                fast = next(m for m, (_, n) in sides.items() if n)
                sources[i] = (f"default {share:.0%} on the {slow.title()} side; "
                              f"your fills on the {fast.title()} side are faster")
        return out, sources

    def replay(self, share: float = 0.1, days: int = REPLAY_DAYS) -> dict[tuple[str, str, float], dict]:
        """How each kind of plan, at each patience level, would have done on each item over the last `days` days.

        The flip rule is replayed through the backtest simulator with the same stake on every item,
        visiting every four hours, with orders waiting behind the stock already on offer (the
        simulator's default "queue" fill model), at the same patience (`reach`) a plan on the board uses. Returns
        {(kind, item id, reach): entry}. An entry holds:

        - `return`: profit as a share of the stake, unsold inventory marked at fair value;
        - `per_hour`: the same profit in Chaos per hour on the stake (not on the capital the
          plan's promise assumes);
        - `realized`: the part of it from sales;
        - `sales`: hours with a sale;
        - `capital`: the most Chaos the item ever had tied up, which says how much of the stake the
          rule could use (a thin market cannot absorb 1,000 Chaos, so a smaller stake earns about
          as much);
        - `hours`: the hours in which orders could fill (the first two days of the window only
          teach the levels, and a young league has fewer than `days` days);
        - `divines`: what holding Divines made over those hours, and `vs_divine`: the return
          against that. At a league start the Divine rises fast, so a flip that holds Chaos or
          items can show a large return in Chaos and still trail plain Divines. `vs_divine` is the
          figure a plan is judged by, and `vs_divine_per_hour` is it in Chaos per hour on the stake.

        A plan that looks good now but lost money all week is telling you something.
        """
        key = (round(share, 4), days)
        if key in self._replays:
            return self._replays[key]
        from vaalstreet import backtest  # backtest imports nothing from here; keep the dependency one-way

        window = self.panel.window(-(days * DAY + SWING_WINDOW))
        n, T = len(window.items), len(window.hours)
        start = min(SWING_WINDOW, T - 1)  # Swing's warm-up: it posts its first orders at this hour
        hours = max(T - 1 - start, 1)
        b0, b1 = float(window.bridge[start]), float(window.bridge[-1])
        divines = b1 / b0 - 1 if math.isfinite(b0) and math.isfinite(b1) and b0 > 0 else 0.0
        out: dict[tuple[str, str, float], dict] = {}
        base = None
        for kind, buy_leg, sell_leg in PLAN_LEGS:
            for reach in PATIENCE:
                sim = backtest.Simulation(window, capital=REPLAY_STAKE * n, participation=share, attention=4,
                                          model_from=base)
                base = base or sim  # one model fit for every run
                sim.run(backtest.Swing(top=n, per_item=1 / n, buy_leg=buy_leg, sell_leg=sell_leg, min_margin=0.02,
                                       reach=reach, warmup=start))
                profit = sim.profit()
                for i, item_id in enumerate(window.items):
                    if sim.sales[i] or sim.position[i] > 0:
                        r = profit[i] / REPLAY_STAKE
                        out[(kind, item_id, reach)] = {
                            "return": _num(r, 3), "sales": int(sim.sales[i]),
                            "realized": _num(sim.realized[i] / REPLAY_STAKE, 3),
                            "capital": _num(sim.peak_cost[i], 4), "hours": hours, "stake": REPLAY_STAKE,
                            "divines": _num(divines, 3), "vs_divine": _num((1 + r) / (1 + divines) - 1, 3),
                            **_per_hour(r, divines, REPLAY_STAKE, hours),
                        }
        self._replays[key] = out
        self._replays[("model", days)] = base
        return out

    def replay_at(self, picks: list[dict], share: float = 0.1, days: int = REPLAY_DAYS) -> list[dict | None]:
        """Each pick's rule replayed over the last `days` days at the pick's own stake, in whole lots.

        `replay` measures every plan on 1,000 Chaos. A small purse differs in two ways that matter:
        it cannot buy a lot it cannot afford, and it can turn over much faster than 1,000 Chaos of
        inventory that the market takes only slowly. Picks with the same kind and patience share one
        run, each item's buying held to its own stake (valued at fair value, so stock bought cheap and
        marked up can tie up more than the stake at cost). Entries look like `replay`'s, with `return` measured on
        the pick's `stake`. A pick that never filled gets a zero entry rather than None.
        """
        from vaalstreet import backtest

        self.replay(share, days)  # fits the model once; every run here reuses it
        base = self._replays[("model", days)]
        window = base.panel
        n, T = len(window.items), len(window.hours)
        start = min(SWING_WINDOW, T - 1)
        hours = max(T - 1 - start, 1)
        b0, b1 = float(window.bridge[start]), float(window.bridge[-1])
        divines = b1 / b0 - 1 if math.isfinite(b0) and math.isfinite(b1) and b0 > 0 else 0.0
        groups: dict[tuple[str, float], list[tuple[int, int, float]]] = {}
        for k, pick in enumerate(picks):
            i = window._by_id.get(pick["id"])
            if i is not None and pick.get("stake", 0) > 0 and pick.get("reach") in PATIENCE:
                groups.setdefault((pick["kind"], pick["reach"]), []).append((k, i, float(pick["stake"])))
        out: list[dict | None] = [None] * len(picks)
        legs = {kind: (buy, sell) for kind, buy, sell in PLAN_LEGS}
        for (kind, reach), members in groups.items():
            memo = ("at", round(share, 4), days, kind, reach, tuple((i, round(s, 2)) for _, i, s in members))
            if memo not in self._replays:
                total = sum(s for _, _, s in members)
                per_item = np.zeros(n)
                for _, i, s in members:
                    per_item[i] = s / total
                sim = backtest.Simulation(window, capital=total, participation=share, attention=4, whole_lots=True,
                                          model_from=base)
                sim.run(backtest.Swing(top=n, per_item=per_item, buy_leg=legs[kind][0], sell_leg=legs[kind][1],
                                       min_margin=0.02, reach=reach, warmup=start))
                profit = sim.profit()
                self._replays[memo] = {i: {
                    "return": _num(profit[i] / s, 3), "sales": int(sim.sales[i]),
                    "realized": _num(sim.realized[i] / s, 3), "capital": _num(sim.peak_cost[i], 4),
                    "stake": _num(s, 4), "hours": hours, "divines": _num(divines, 3),
                    "vs_divine": _num((1 + profit[i] / s) / (1 + divines) - 1, 3), "whole_lots": True,
                    **_per_hour(profit[i] / s, divines, s, hours),
                    "at_stake": True,  # measured at this pick's stake: never rescaled
                } for _, i, s in members}
            for k, i, _ in members:
                out[k] = self._replays[memo][i]
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

    def outside_conversions(self, prices: dict[str, dict], share: float = 0.1,
                            costs: Costs | None = None) -> list[dict]:
        """Card sets whose reward does not trade on the exchange - a unique, a gem or a base type -
        against that reward's price from stash listings (`ninja.reward_prices`: card id -> price).

        The cards are priced from the exchange as usual; only the reward comes from outside. Those
        listing prices never enter fair value, and these rows stay out of round trips and alerts.
        There is no quick exit on the exchange for the reward, so `margin_now` is left empty.
        """
        from vaalstreet.ninja import CREDIT, NOTE

        p = self.panel
        r, quick = self.legs["r"], self.legs["b"]
        fair = self.fv.fair[-1]
        value_24h = np.nansum(p["value"][-DAY:], axis=0)
        age = self._age()
        on_exchange = set(p.names)  # the exchange stays the source wherever it has a price
        out = []
        for item_id, price in prices.items():
            i = p._by_id.get(item_id)
            recipe = self.meta.get(item_id, {}).get("set")
            if (i is None or not recipe or recipe["reward"] in on_exchange
                    or not math.isfinite(fair[i]) or age[i] >= DAY):
                continue
            need, gives = recipe["need"], recipe["gives"]
            cost, proceeds = need * float(fair[i]), gives * price["chaos"]
            sets = share * (r.flow[i] + quick.flow[i]) / need
            out.append({
                "id": item_id, "name": p.names[i], "need": need, "gives": gives, "steps": 1, "how": "set",
                "reward": recipe["reward"], "reward_id": None, "reward_kind": recipe["kind"],
                "cost": _num(cost), "proceeds": _num(proceeds), "margin": _num(proceeds / cost - 1, 4),
                "margin_now": None,
                "sets_per_hour": _num(sets, 4), "profit_per_hour": _num(sets * (proceeds - cost), 4),
                "value_24h": _num(value_24h[i]), "reward_value_24h": None,
                "recipe_key": recipe_key("set", [item_id, "poe.ninja:" + recipe["reward"]]),
                "reward_source": "poe.ninja", "reward_note": NOTE, "credit": CREDIT,
                "reward_price": _num(price["chaos"]), "reward_listings": price.get("listings"),
                "reward_fetched": price.get("fetched"), "reward_stale": bool(price.get("stale")),
                "reward_variant": price.get("variant"),
            })
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
        quotes: dict[tuple[str, bool], tuple[float, float, float, float] | None] = {}

        def quote(item_id: str, chaos_here: bool = False) -> tuple[float, float, float, float] | None:
            """(entry cost, exit proceeds) for one unit, in reference currency, and the value the market
            you would buy in and the one you would sell in traded over the last day."""
            if item_id == ref:
                return (1.0, 1.0, math.inf, math.inf)
            key = (item_id, chaos_here)
            if key not in quotes:
                i = index.get(item_id)
                legs = (r,) if chaos_only or chaos_here else (r, b)
                options = [(float(leg.fair[-1, i]), float(leg.flow[i]) * DAY * float(leg.fair[-1, i]))
                           for leg in legs if i is not None and self._quotable(leg, i)]
                if options:
                    (buy, buy_value), (sell, sell_value) = min(options), max(options)
                    quotes[key] = (buy, sell, buy_value, sell_value)
                else:
                    quotes[key] = None
            return quotes[key]

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
            if value < min_value or _out_of_line(m["rate"] * pb / pa):
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
                if value < min_value or _out_of_line(price / float(r.fair[-1, i])):
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
            if not math.isfinite(value):  # vendor swaps alone
                hours = DAY
            # A trip is as deep as its thinnest market, and that includes the market you buy the first
            # item in and the one you sell the last item in: a swap that trades millions a day does not
            # help when the item you start with trades one unit every few hours.
            value = min(value, start[2], end[3])
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
                "profit_per_hour": _num(share * value * edge / DAY, 4),  # the unit flips and conversions use
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
