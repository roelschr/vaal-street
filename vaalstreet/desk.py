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

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from vaalstreet import items as item_meta
from vaalstreet import pricing
from vaalstreet.ingest import ANCHORS
from vaalstreet.panel import CORE, HOUR, LEGS, Panel, ffill
from vaalstreet.paths import data_dir

DAY = 24
SWING_WINDOW = 48  # hours of prints used to learn where each market trades relative to fair value
PATIENCE = (0.15, 0.25, 0.35, 0.5)  # share of hours an order level is expected to be reached


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
    flow: np.ndarray  # [N] units traded per hour, trailing day
    active: np.ndarray  # [N] share of trailing-day hours with a print
    age: np.ndarray  # [N] hours since the leg last printed
    low: np.ndarray  # [N] typical executed low over the trailing day (median, reference currency)
    high: np.ndarray
    ask_units: np.ndarray  # [N] item units resting on offer (latest known)
    bid_value: np.ndarray  # [N] reference-currency value bidding for the item (latest known)


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
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                low, high = np.nanmedian(lo[-DAY:], axis=0), np.nanmedian(hi[-DAY:], axis=0)
            legs[key] = Leg(
                label, leg_fv.fair, np.where(np.isfinite(dev), dev, np.nan),
                pricing.trailing_sum(vol, DAY)[-1] / DAY,
                pricing.activity(price, DAY)[-1], _age(price), low, high,
                ffill(_geo(panel[f"{key}_stock_lo"], panel[f"{key}_stock_hi"]), limit=6)[-1],
                ffill(_geo(panel[f"{key}_bid_lo"], panel[f"{key}_bid_hi"]), limit=6)[-1],
            )
        premium = pricing.leg_premium(panel["r_price"][-DAY:], panel["b_price"][-DAY:])[-1]
        ref, bridge = ANCHORS[realm]
        cross = con.execute(
            """
            SELECT item, quote, sum(volume) AS volume, median(volume_quote / volume) AS rate, count(*) AS hours
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
            [dict(zip(("item", "quote", "volume", "rate", "hours"), row)) for row in cross],
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
        age = self._age()
        value_24h = np.nansum(p["value"][-DAY:], axis=0)
        active = pricing.activity(p["price"], DAY)[-1]

        def change(back: int) -> np.ndarray:
            """Move over `back` hours between two 3-hour medians, so one odd print cannot fake a trend."""
            if T <= back + 3:
                return np.full(fair.shape[1], np.nan)
            import warnings

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
            "price": p["price"][rows, i], "fair": self.fv.fair[rows, i], "value": p["value"][rows, i],
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

    def flips(self, share: float = 0.1, min_margin: float = 0.02, min_active: float = 0.5) -> list[dict]:
        """Ranked maker plans. `share` is the fraction of a market's flow you expect to capture.

        Each plan buys on one leg and sells on another (or the same) at levels the
        leg's *hourly average* price has actually reached: "in `reach` of recent
        hours the Chaos market averaged this low, and the Divine market averaged
        that high". Volume traded through those levels, which is what makes them
        better evidence than the published extremes. For every item and leg pair
        the patience level with the highest expected profit is kept.
        """
        import warnings

        p = self.panel
        fair = self.fv.fair[-1]
        plans = []
        r, b = self.legs["r"], self.legs["b"]
        n_obs = {key: np.sum(~np.isnan(leg.dev), axis=0) for key, leg in self.legs.items()}
        best: dict[tuple[str, int], dict] = {}
        for kind, buy_key, sell_key in (("chaos", "r", "r"), ("divine", "b", "b"), ("cross", "r", "b")):
            buy_leg, sell_leg = self.legs[buy_key], self.legs[sell_key]
            usable = (
                (n_obs[buy_key] >= 12) & (n_obs[sell_key] >= 12) & np.isfinite(fair)
                & (buy_leg.active >= min_active) & (sell_leg.active >= min_active)
                & (buy_leg.age < 6) & (sell_leg.age < 6)
            )
            if not usable.any():
                continue
            for reach in PATIENCE:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)
                    buy = fair * np.exp(np.nanquantile(buy_leg.dev, reach, axis=0))
                    sell = fair * np.exp(np.nanquantile(sell_leg.dev, 1 - reach, axis=0))
                with np.errstate(divide="ignore", invalid="ignore"):
                    margin = sell / buy - 1
                # Orders only work in the hours that reach them, and a maker sees one side of the flow.
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
                    }
        plans = list(best.values())
        for plan in plans:
            plan["profit_per_hour"] = _num(plan["profit_per_hour"], 4)
        plans.sort(key=lambda plan: -(plan["profit_per_hour"] or 0))
        return plans

    # ------------------------------------------------------------- arbitrage
    def conversions(self, share: float = 0.1) -> list[dict]:
        """Full sets (divination cards, shards) against what they turn into."""
        p = self.panel
        by_name = {name: i for i, name in enumerate(p.names)}
        ref_name = self.meta.get(ANCHORS[self.realm][0], {}).get("name", "Chaos Orb")
        r = self.legs["r"]
        fair = self.fv.fair[-1]
        value_24h = np.nansum(p["value"][-DAY:], axis=0)
        age = self._age()
        out = []
        for i, item_id in enumerate(p.items):
            recipe = self.meta.get(item_id, {}).get("set")
            if not recipe or not math.isfinite(fair[i]) or age[i] >= DAY:
                continue
            if recipe["reward"] == ref_name:
                reward_fair, reward_sell, j = 1.0, 1.0, None
            elif recipe["reward"] in by_name:
                j = by_name[recipe["reward"]]
                if not math.isfinite(fair[j]) or age[j] >= DAY:
                    continue
                reward_fair = float(fair[j])
                # Selling into the Chaos market is the quick exit.
                reward_sell = float(r.fair[-1, j]) if r.age[j] < 6 and math.isfinite(r.fair[-1, j]) else reward_fair
            else:
                continue
            cost = recipe["need"] * float(fair[i])
            # Buying through the Divine market is the quick entry.
            quick = self.legs["b"]
            buy_now = float(quick.fair[-1, i]) if quick.age[i] < 6 and math.isfinite(quick.fair[-1, i]) else float(fair[i])
            cost_now = recipe["need"] * max(buy_now, float(fair[i]))
            out_fair = recipe["gives"] * reward_fair
            flow = (r.flow[i] + self.legs["b"].flow[i])
            sets = share * flow / recipe["need"]
            out.append({
                "id": item_id, "name": p.names[i], "need": recipe["need"], "gives": recipe["gives"],
                "reward": recipe["reward"], "reward_id": p.items[j] if j is not None else ANCHORS[self.realm][0],
                "cost": _num(cost), "proceeds": _num(out_fair),
                "margin": _num(out_fair / cost - 1, 4),
                # Pay the richer market for the cards and dump the reward in the cheaper one.
                "margin_now": _num(recipe["gives"] * min(reward_sell, reward_fair) / cost_now - 1, 4),
                "sets_per_hour": _num(sets, 4),
                "profit_per_hour": _num(sets * (out_fair - cost), 4),
                "value_24h": _num(value_24h[i]),
            })
        out.sort(key=lambda row: -(row["profit_per_hour"] or 0))
        return out

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
