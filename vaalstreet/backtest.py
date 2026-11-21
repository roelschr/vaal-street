"""Hourly backtests on the exchange archive.

The simulator replays one league hour by hour. A strategy sees everything
published up to hour t, posts orders, and those orders meet hour t+1's market.
There is no order book in the data, so execution is modelled from what *is*
published per market and hour - the average traded price (VWAP) and the volume:

* A resting order at limit L fills, at L, in an hour whose VWAP traded through L
  (VWAP <= L for a buy, >= L for a sell). Volume demonstrably changed hands on the
  far side of the limit, so the fill is plausible; hours that merely touched L at
  the extreme do not count. It never gets a better price than L.
* A market order (no limit) pays the hour's VWAP plus `slippage`, the taker's
  cost of crossing the spread.
* A fill takes at most `participation` of the hour's volume in that market.
* No leverage, no shorting: buys are scaled to cash, sells to inventory.

Orders live in one of two markets per item: "r" (against Chaos) or "b" (against
Divine). Divine proceeds are converted to Chaos at that hour's Divine price, so
the account is kept in Chaos throughout.

A strategy is any object with `step(ctx)`; see `Hold`, `Swing`, `Reversion`,
`Momentum` below, or pass your own class to `run`.
"""

from __future__ import annotations

import functools
import importlib.util
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import duckdb
import numpy as np

from vaalstreet import pricing
from vaalstreet.panel import CORE, HOUR, LEGS, Panel, ffill, rolling_weighted_quantile

LEG_KEYS = ("r", "b")
MAX_SWING = math.log(1.5)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_schedule(spec: str) -> np.ndarray:
    """A play schedule as a [7, 24] mask of the hours you are at the desk (Monday first, UTC).

    `spec` is one or more windows separated by ";" or ",", each "[days] start-end":
    "mon-fri 19-22", "sat,sun 10-14; wed 20-23", or "18-20" for every day. Days take
    ranges that wrap ("fri-mon"). Hours are whole UTC hours and the end is exclusive, so
    "19-22" is three hours of play; "22-2" runs past midnight into the next day.
    """
    mask = np.zeros((7, 24), dtype=bool)
    windows = [w.strip().lower() for w in spec.replace(";", "\n").splitlines() if w.strip()]
    if not windows:
        raise ValueError("Empty schedule; write it like 'mon-fri 19-22'.")
    days: list[int] = []
    for window in windows:
        # Commas separate both day lists ("sat,sun 10-14") and windows ("mon 19-22, sun 10-14").
        for part in [p.strip() for p in window.split(",") if p.strip()]:
            words = part.split()
            if len(words) > 2:
                raise ValueError(f"Cannot read schedule window {part!r}; write it like 'mon-fri 19-22'.")
            if len(words) == 2 or not any(c.isdigit() for c in words[0]):
                days += _days(words[0])
            if any(c.isdigit() for c in words[-1]):
                start, end = _hours(words[-1])
                for day in days or range(7):
                    for h in range(start, end if end > start else end + 24):  # "22-2" wraps
                        mask[(day + h // 24) % 7, h % 24] = True
                days = []
    if days:
        raise ValueError(f"Schedule {spec!r} names days without hours; write it like 'mon-fri 19-22'.")
    return mask


def _days(text: str) -> list[int]:
    first, _, last = text.partition("-")
    try:
        a = DAYS.index(first[:3])
        b = DAYS.index(last[:3]) if last else a
    except ValueError:
        raise ValueError(f"Unknown day {text!r}; use mon, tue, wed, thu, fri, sat, sun.") from None
    return [(a + i) % 7 for i in range((b - a) % 7 + 1)]


def _hours(text: str) -> tuple[int, int]:
    try:
        start, end = (int(x) for x in text.split("-"))
    except ValueError:
        raise ValueError(f"Cannot read hours {text!r}; write them like 19-22 (UTC, end not included).") from None
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        raise ValueError(f"Hours {text!r} must lie between 0 and 24 and not be empty.")
    return start, end


def in_schedule(mask: np.ndarray, hours: np.ndarray) -> np.ndarray:
    """Whether each panel hour's visit falls inside the schedule.

    Hour stamps are the start of the hour in unix seconds (UTC). An hour's prices are published
    when it ends, so the visit that acts on row t happens at hours[t] + 1h. 1970-01-01 was a Thursday.
    """
    clock = (np.asarray(hours, dtype=np.int64) + HOUR) // HOUR
    return mask[(clock // 24 + 3) % 7, clock % 24]


class Strategy(Protocol):
    def step(self, ctx: "Context") -> None: ...


@dataclass
class Book:
    """Standing orders in one market family: per item, at most one buy and one sell."""

    buy_limit: np.ndarray
    buy_units: np.ndarray
    sell_limit: np.ndarray
    sell_units: np.ndarray
    # With whole lots: the price each order states, in its own market's currency (Chaos or
    # Divine), and its lot size in items. NaN / 1 for market orders and outside whole-lot runs.
    buy_ratio: np.ndarray | None = None
    buy_lot: np.ndarray | None = None
    sell_ratio: np.ndarray | None = None
    sell_lot: np.ndarray | None = None

    def __post_init__(self) -> None:
        n = len(self.buy_limit)
        for side in ("buy", "sell"):
            if getattr(self, f"{side}_ratio") is None:
                setattr(self, f"{side}_ratio", np.full(n, np.nan))
            if getattr(self, f"{side}_lot") is None:
                setattr(self, f"{side}_lot", np.ones(n))

    @classmethod
    def empty(cls, n: int) -> "Book":
        return cls(np.full(n, np.nan), np.zeros(n), np.full(n, np.nan), np.zeros(n))

    def copy(self) -> "Book":
        return Book(*(np.array(getattr(self, f), dtype=float) for f in (
            "buy_limit", "buy_units", "sell_limit", "sell_units", "buy_ratio", "buy_lot", "sell_ratio", "sell_lot")))


class Context:
    """What a strategy can see and do at hour `t`. Everything exposed is known at t."""

    def __init__(self, sim: "Simulation") -> None:
        self._sim = sim
        self.t = 0

    # ----- the past
    @property
    def n_items(self) -> int:
        return len(self._sim.panel.items)

    @property
    def names(self) -> list[str]:
        return self._sim.panel.names

    @property
    def hour(self) -> int:
        return int(self._sim.panel.hours[self.t])

    @property
    def league_hour(self) -> int:
        return self.t + self._sim.offset

    def history(self, column: str, hours: int | None = None) -> np.ndarray:
        """Rows up to and including the current hour of a panel column ([t+1, N] or the last `hours`)."""
        data = self._sim.panel[column]
        start = 0 if hours is None else max(0, self.t + 1 - hours)
        return data[start : self.t + 1]

    @property
    def fair(self) -> np.ndarray:
        """Causal fair value per item at the current hour."""
        return self._sim.fair[self.t]

    def fair_history(self, hours: int | None = None) -> np.ndarray:
        start = 0 if hours is None else max(0, self.t + 1 - hours)
        return self._sim.fair[start : self.t + 1]

    @property
    def z(self) -> np.ndarray:
        """Surprise of this hour's print against the model's forecast (NaN if no print)."""
        return self._sim.z[self.t]

    def swing(self, leg: str, reach: float) -> np.ndarray:
        """Log distance from fair value below which `reach` of the leg's recent volume traded."""
        return self._sim.swing(leg, reach)[self.t]

    @property
    def bridge(self) -> float:
        return float(self._sim.panel.bridge[self.t])

    # ----- the account
    @property
    def cash(self) -> float:
        return self._sim.cash

    @property
    def position(self) -> np.ndarray:
        return self._sim.position

    @property
    def equity(self) -> float:
        return self._sim.equity_now(self.t)

    @property
    def bankroll(self) -> float:
        """Capital to size orders against: the starting stake, or current equity when compounding."""
        return self.equity if self._sim.compound else min(self.equity, self._sim.start_capital)

    @property
    def average_cost(self) -> np.ndarray:
        """What each unit currently held was bought for, per item (NaN where nothing is held)."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self._sim.position > 0, self._sim.cost / self._sim.position, np.nan)

    @property
    def gold_spent(self) -> float:
        return self._sim.gold_spent

    @property
    def gold_left(self) -> float:
        """Gold still in the budget (infinite without one)."""
        return self._sim.gold_budget - self._sim.gold_spent

    @property
    def state(self) -> dict:
        """Scratch space that persists between steps."""
        return self._sim.state

    # ----- orders (replace whatever was standing in that market)
    def quote(
        self,
        leg: str = "r",
        *,
        buy_limit: np.ndarray | None = None,
        buy_units: np.ndarray | None = None,
        sell_limit: np.ndarray | None = None,
        sell_units: np.ndarray | None = None,
    ) -> None:
        """Set standing orders for all items in one market family. NaN limit or zero units = no order."""
        book = self._sim.books[leg]
        n = self.n_items
        book.buy_limit = np.full(n, np.nan) if buy_limit is None else np.asarray(buy_limit, dtype=float)
        book.buy_units = np.zeros(n) if buy_units is None else np.nan_to_num(np.asarray(buy_units, dtype=float))
        book.sell_limit = np.full(n, np.nan) if sell_limit is None else np.asarray(sell_limit, dtype=float)
        book.sell_units = np.zeros(n) if sell_units is None else np.nan_to_num(np.asarray(sell_units, dtype=float))

    def quote_sells(self, leg: str, limit: np.ndarray, units: np.ndarray) -> None:
        """Replace only the sell side of one market family."""
        book = self._sim.books[leg]
        book.sell_limit = np.asarray(limit, dtype=float)
        book.sell_units = np.nan_to_num(np.asarray(units, dtype=float))

    def quote_buys(self, leg: str, limit: np.ndarray, units: np.ndarray) -> None:
        """Replace only the buy side of one market family."""
        book = self._sim.books[leg]
        book.buy_limit = np.asarray(limit, dtype=float)
        book.buy_units = np.nan_to_num(np.asarray(units, dtype=float))

    def buy(self, item: int, units: float, limit: float = math.inf, leg: str = "r") -> None:
        book = self._sim.books[leg]
        book.buy_limit[item], book.buy_units[item] = limit, units

    def sell(self, item: int, units: float, limit: float = 0.0, leg: str = "r") -> None:
        book = self._sim.books[leg]
        book.sell_limit[item], book.sell_units[item] = limit, units

    def cancel_all(self) -> None:
        for key in LEG_KEYS:
            self._sim.books[key] = Book.empty(self.n_items)


@dataclass
class Result:
    league: str
    strategy: str
    hours: np.ndarray
    equity: np.ndarray
    cash: np.ndarray
    start_capital: float
    fills: int
    bought: float
    sold: float
    realized: np.ndarray  # [N] realised profit per item
    names: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    divine: np.ndarray | None = None  # [T] Divine price relative to hour 24: the "just hold Divines" line
    gold: np.ndarray | None = None  # [T] Gold spent on posting orders so far

    @property
    def gold_spent(self) -> float:
        return float(self.gold[-1]) if self.gold is not None and len(self.gold) else 0.0

    @property
    def total_return(self) -> float:
        return float(self.equity[-1] / self.start_capital - 1)

    @property
    def divine_return(self) -> float:
        """What the same Chaos would have done sitting in Divine Orbs."""
        return float(self.divine[-1] - 1) if self.divine is not None else float("nan")

    @property
    def max_drawdown(self) -> float:
        peak = np.maximum.accumulate(self.equity)
        return float(np.max(1 - self.equity / peak))

    @property
    def daily_sharpe(self) -> float:
        """Mean over standard deviation of daily returns (not annualised)."""
        daily = self.equity[::24]
        if len(daily) < 4:
            return float("nan")
        rets = np.diff(daily) / daily[:-1]
        sd = rets.std(ddof=1)
        return float(rets.mean() / sd) if sd > 0 else float("nan")

    def summary(self) -> dict:
        days = max(len(self.hours) / 24, 1e-9)
        order = np.argsort(-np.abs(self.realized))[:8]
        return {
            "league": self.league,
            "strategy": self.strategy,
            "days": round(days, 1),
            "start": self.start_capital,
            "end": round(float(self.equity[-1]), 2),
            "return": round(self.total_return, 4),
            "divine_return": None if math.isnan(self.divine_return) else round(self.divine_return, 4),
            "per_day": round(float((self.equity[-1] - self.start_capital) / days), 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "daily_sharpe": None if math.isnan(self.daily_sharpe) else round(self.daily_sharpe, 3),
            "fills": self.fills,
            "turnover": round(float((self.bought + self.sold) / 2 / self.start_capital), 2),
            "gold_spent": round(self.gold_spent),
            "chaos_per_1000_gold": round(float((self.equity[-1] - self.start_capital) / self.gold_spent * 1000), 2)
            if self.gold_spent > 0 else None,
            "top_items": [
                {"name": self.names[i], "profit": round(float(self.realized[i]), 2)} for i in order if self.realized[i]
            ],
        }


class Simulation:
    def __init__(
        self,
        panel: Panel,
        *,
        capital: float = 10_000.0,
        participation: float = 0.1,
        slippage: float = 0.03,
        offset: int = 0,
        attention: int = 1,
        compound: bool = False,
        whole_lots: bool = False,
        gold_per_unit: float | np.ndarray = 0.0,
        gold_budget: float | None = None,
        schedule: str | np.ndarray | None = None,
    ) -> None:
        self.panel = panel
        self.participation = participation
        self.slippage = slippage
        self.offset = offset
        self.attention = max(1, int(attention))
        self.compound = compound
        self.whole_lots = whole_lots
        n_items = len(panel.items)
        fee = np.nan_to_num(np.broadcast_to(np.asarray(gold_per_unit, dtype=float), (n_items,)).copy())
        if (fee < 0).any():
            raise ValueError("Gold per unit cannot be negative.")
        self.gold_fee = fee
        self.gold_budget = math.inf if gold_budget is None else float(gold_budget)
        self.gold_spent = 0.0
        if isinstance(schedule, str):
            schedule = parse_schedule(schedule)
        self.playing = None if schedule is None else in_schedule(schedule, panel.hours)
        self.start_capital = capital
        self.cash = capital
        n = len(panel.items)
        self.position = np.zeros(n)
        self.cost = np.zeros(n)  # total cost of the current position, for average-cost P&L
        self.realized = np.zeros(n)
        self.books = {key: Book.empty(n) for key in LEG_KEYS}
        self.state: dict = {}
        fv = pricing.fair_value(panel["price"], panel["value"])
        self.fair, self.z = fv.fair, fv.z
        self._mark = ffill(self.fair)
        self._swing: dict[tuple[str, float], np.ndarray] = {}
        self.fills = 0
        self.sales = np.zeros(n, dtype=np.int64)  # hours in which each item sold
        self.bought = 0.0
        self.sold = 0.0

    def profit(self, t: int = -1) -> np.ndarray:
        """Profit per item so far: realised, plus what is still held marked at fair value."""
        return self.realized + self.position * np.nan_to_num(self._mark[t]) - self.cost

    def swing(self, leg: str, reach: float, window: int = 48) -> np.ndarray:
        """Causal, volume-weighted rolling quantile of log(leg VWAP / prior fair): [T, N].

        The level below which `reach` of the leg's units traded over the trailing window."""
        key = (leg, reach)
        if key not in self._swing:
            prior = np.vstack([self.fair[:1], self.fair[:-1]])
            with np.errstate(divide="ignore", invalid="ignore"):
                dev = np.log(self.panel[f"{leg}_price"] / prior)
            # Never place a level more than ~1.5x from fair value: beyond that the print was an empty book.
            dev = np.where(np.isfinite(dev), np.clip(dev, -MAX_SWING, MAX_SWING), np.nan)
            self._swing[key] = rolling_weighted_quantile(dev, self.panel[f"{leg}_volume"], window, reach, min_count=12)
        return self._swing[key]

    def equity_now(self, t: int) -> float:
        return float(self.cash + np.nansum(self.position * np.nan_to_num(self._mark[t])))

    def _execute(self, t: int) -> None:
        """Match the standing orders against hour t's markets."""
        if self.whole_lots:
            return self._execute_whole(t)
        for key in LEG_KEYS:
            book = self.books[key]
            vwap = self.panel[f"{key}_price"][t]
            cap = self.participation * np.nan_to_num(self.panel[f"{key}_volume"][t])
            traded = np.isfinite(vwap) & (cap > 0)

            # Sells first: they free up cash for this hour's buys.
            want = np.minimum(np.minimum(book.sell_units, self.position), cap)
            with np.errstate(invalid="ignore"):
                hit = traded & (want > 0) & ~np.isnan(book.sell_limit) & (vwap >= book.sell_limit)
            if hit.any():
                market = book.sell_limit <= 0
                price = np.where(hit, np.where(market, vwap * (1 - self.slippage), book.sell_limit), 0.0)
                units = np.where(hit, want, 0.0)
                self._sold(units, price, hit)
                book.sell_units = book.sell_units - units

            want = np.minimum(book.buy_units, cap)
            with np.errstate(invalid="ignore"):
                hit = traded & (want > 0) & ~np.isnan(book.buy_limit) & (vwap <= book.buy_limit)
            if hit.any():
                market = np.isinf(book.buy_limit)
                price = np.where(hit, np.where(market, vwap * (1 + self.slippage), book.buy_limit), 0.0)
                cost = np.where(hit, want, 0.0) * price
                total = float(cost.sum())
                scale = min(1.0, self.cash / total) if total > 0 else 0.0
                units = np.where(hit, want, 0.0) * scale
                self._bought(units, cost * scale, hit)
                book.buy_units = book.buy_units - units

    def _sold(self, units: np.ndarray, price: np.ndarray, hit: np.ndarray) -> None:
        proceeds = units * price
        with np.errstate(divide="ignore", invalid="ignore"):
            avg_cost = np.where(self.position > 0, self.cost / self.position, 0.0)
        self.realized += proceeds - units * avg_cost
        self.cost -= units * avg_cost
        self.position -= units
        self.cash += float(proceeds.sum())
        self.sold += float(proceeds.sum())
        self.fills += int(hit.sum())
        self.sales += hit

    def _bought(self, units: np.ndarray, cost: np.ndarray, hit: np.ndarray) -> None:
        self.position += units
        self.cost += cost
        self.cash -= float(cost.sum())
        self.bought += float(cost.sum())
        self.fills += int(hit.sum())

    def _execute_whole(self, t: int) -> None:
        """Hour t's fills when the exchange's whole-lot rules apply.

        A limit order fills in whole lots at the ratio it states. A Divine-market order states
        Divines, so it is tested and paid at this hour's Divine price, the way the order really
        stands while the Divine moves. Market orders fill whole units. Buys take cash order by
        order rather than pro rata, because scaling every order down would leave each one short
        of a whole lot.
        """
        bridge = float(self.panel.bridge[t])
        for key in LEG_KEYS:
            book = self.books[key]
            unit = bridge if key == "b" else 1.0
            vwap = self.panel[f"{key}_price"][t]
            cap = self.participation * np.nan_to_num(self.panel[f"{key}_volume"][t])
            traded = np.isfinite(vwap) & (cap > 0)

            stated = np.isfinite(book.sell_ratio)
            limit = np.where(stated, book.sell_ratio * unit, book.sell_limit)
            want = _whole(np.minimum(np.minimum(book.sell_units, self.position), cap), book.sell_lot)
            with np.errstate(invalid="ignore"):
                hit = traded & (want > 0) & ~np.isnan(limit) & (vwap >= limit)
            if hit.any():
                market = ~stated & (book.sell_limit <= 0)
                price = np.where(hit, np.where(market, vwap * (1 - self.slippage), limit), 0.0)
                units = np.where(hit, want, 0.0)
                self._sold(units, price, hit)
                book.sell_units = book.sell_units - units

            stated = np.isfinite(book.buy_ratio)
            limit = np.where(stated, book.buy_ratio * unit, book.buy_limit)
            want = _whole(np.minimum(book.buy_units, cap), book.buy_lot)
            with np.errstate(invalid="ignore"):
                hit = traded & (want > 0) & ~np.isnan(limit) & (vwap <= limit)
            if hit.any():
                market = ~stated & np.isinf(book.buy_limit)
                price = np.where(market, vwap * (1 + self.slippage), limit)
                units = np.zeros_like(want)
                for i in np.flatnonzero(hit):
                    lot = book.buy_lot[i]
                    lots = min(want[i] / lot, math.floor(self.cash / (price[i] * lot) + 1e-9))
                    units[i] = max(lots, 0) * lot
                    self.cash -= units[i] * price[i]  # reserved here, booked by _bought below
                hit &= units > 0
                cost = np.where(hit, units * price, 0.0)
                self.cash += float(cost.sum())
                self._bought(units, cost, hit)
                book.buy_units = book.buy_units - units

    def _post(self, t: int, before: dict[str, Book]) -> None:
        """Turn what the strategy just quoted into orders the exchange would take, and charge Gold.

        An order counts as posted when its limit changed or its size grew; an order left as it
        stood, or one that only shrank, costs nothing. With whole lots, a posted order is re-cut
        to a whole-number ratio the purse can pay for (the same fit the Flips page makes for a
        bankroll), and dropped if not even one lot fits. Gold is charged per unit posted; an order
        the remaining budget cannot pay for is not posted, and whatever stood before stays.
        """
        cash = self.cash
        unit_b = float(self.panel.bridge[t])
        for key in LEG_KEYS:
            book, old = self.books[key], before[key]
            unit = unit_b if key == "b" else 1.0
            for side in ("sell", "buy"):
                limit, units = getattr(book, f"{side}_limit"), getattr(book, f"{side}_units")
                ratio, lot = getattr(book, f"{side}_ratio"), getattr(book, f"{side}_lot")
                old_limit, old_units = getattr(old, f"{side}_limit"), getattr(old, f"{side}_units")
                # quote() may hand over fresh arrays; take writable copies before changing them.
                limit, units, ratio, lot = (np.array(a, dtype=float) for a in (limit, units, ratio, lot))
                old_ratio, old_lot = getattr(old, f"{side}_ratio"), getattr(old, f"{side}_lot")
                live = ~np.isnan(limit) & (units > 0)
                same_limit = (limit == old_limit) | (np.isnan(limit) & np.isnan(old_limit))
                untouched = live & same_limit & (units == old_units)
                ratio[untouched], lot[untouched] = old_ratio[untouched], old_lot[untouched]
                ratio[~live], lot[~live] = np.nan, 1.0
                for i in np.flatnonzero(live & ~untouched):
                    if self.whole_lots:
                        fitted = self._fit(side, float(limit[i]), float(units[i]), unit, cash, i)
                        if fitted is None:
                            limit[i], units[i], ratio[i], lot[i] = np.nan, 0.0, np.nan, 1.0
                            continue
                        limit[i], units[i], ratio[i], lot[i] = fitted
                    # Judge "the same order" on what would really stand: a re-quote that rounds to the
                    # standing ratio is no new post, and only units beyond the standing ones pay again.
                    if np.isfinite(ratio[i]) or np.isfinite(old_ratio[i]):
                        same = ratio[i] == old_ratio[i] and lot[i] == old_lot[i]
                    else:
                        same = bool(same_limit[i])
                    if same:
                        limit[i] = old_limit[i]  # keep the standing order's price as it was posted
                    fee = self.gold_fee[i] * (max(units[i] - old_units[i], 0.0) if same else units[i])
                    if fee > 0 and self.gold_spent + fee > self.gold_budget + 1e-9:
                        limit[i], units[i], ratio[i], lot[i] = old_limit[i], old_units[i], old_ratio[i], old_lot[i]
                        continue
                    self.gold_spent += fee
                    if side == "buy" and self.whole_lots and np.isfinite(ratio[i]):
                        cash -= units[i] * ratio[i] * unit  # the next buy is cut to what is left
                setattr(book, f"{side}_limit", limit)
                setattr(book, f"{side}_units", units)
                setattr(book, f"{side}_ratio", ratio)
                setattr(book, f"{side}_lot", lot)

    def _fit(
        self, side: str, limit: float, units: float, unit: float, cash: float, item: int
    ) -> tuple[float, float, float, float] | None:
        """(limit, units, stated ratio, lot) for one whole-lot order, or None if it cannot be posted."""
        if side == "buy" and math.isinf(limit) or side == "sell" and limit <= 0:  # market order
            whole = math.floor(min(units, self.position[item] if side == "sell" else units) + 1e-9)
            return (limit, float(whole), math.nan, 1.0) if whole >= 1 else None
        if not math.isfinite(unit) or unit <= 0:
            return None
        if side == "buy":
            pair = _fit_ratio(limit / unit, cash / unit)
        else:
            pair = _fit_ratio(limit / unit, 400.0, math.floor(self.position[item] + 1e-9))
        if pair is None:
            return None
        currency, items = pair
        held = units if side == "buy" else min(units, self.position[item])
        lots = math.floor(held / items + 1e-9)
        if side == "buy":
            lots = min(lots, math.floor(cash / (currency * unit) + 1e-9))
        if lots < 1:
            return None
        ratio = currency / items
        return ratio * unit, float(lots * items), ratio, float(items)

    def run(self, strategy: Strategy, name: str | None = None, params: dict | None = None) -> Result:
        T = len(self.panel.hours)
        ctx = Context(self)
        equity = np.empty(T)
        cash = np.empty(T)
        gold = np.empty(T)
        posting = self.whole_lots or bool(self.gold_fee.any())
        session = 0  # first hour of the current play session
        for t in range(T):
            if t > 0:
                self._execute(t)
            ctx.t = t
            if self.playing is None:
                visit = t % self.attention == 0  # between visits the standing orders just sit there
            elif self.playing[t]:
                if t == 0 or not self.playing[t - 1]:
                    session = t
                visit = (t - session) % self.attention == 0
            else:
                visit = False  # away from the game: orders stand until the next session
            if visit:
                before = {k: b.copy() for k, b in self.books.items()} if posting else None
                strategy.step(ctx)
                if posting:
                    self._post(t, before)
            equity[t] = self.equity_now(t)
            cash[t] = self.cash
            gold[t] = self.gold_spent
        bridge = self.panel.bridge
        # The first hours of a league print wild Divine prices; anchor the benchmark a day in.
        known = np.flatnonzero(np.isfinite(bridge))
        first = bridge[known[known >= min(24, T - 1)][0]] if len(known) and (known >= min(24, T - 1)).any() else np.nan
        return Result(
            self.panel.league, name or type(strategy).__name__, self.panel.hours, equity, cash,
            self.start_capital, self.fills, self.bought, self.sold, self.realized, self.panel.names, params or {},
            self._benchmark(bridge, first, known), gold,
        )

    @staticmethod
    def _benchmark(bridge: np.ndarray, first: float, known: np.ndarray) -> np.ndarray:
        """Relative value of Chaos parked in Divines from the anchor hour on (flat before it)."""
        if not math.isfinite(first):
            return np.ones(len(bridge))
        anchor = int(known[known >= min(24, len(bridge) - 1)][0])
        line = np.where(np.isfinite(bridge), bridge, first) / first
        line[:anchor] = 1.0
        return line


def _whole(units: np.ndarray, lot: np.ndarray) -> np.ndarray:
    """Round down to whole lots (the epsilon keeps 2.9999999 lots from becoming 2)."""
    return np.floor(np.maximum(units, 0) / lot + 1e-9) * lot


@functools.lru_cache(maxsize=65536)
def _fit_cached(price: float, max_currency: int, max_items: int) -> tuple[int, int] | None:
    return pricing.fit_ratio(price, max_currency, max_items)


def _fit_ratio(price: float, max_currency: float, max_items: float = 5000) -> tuple[int, int] | None:
    """pricing.fit_ratio, cached: strategies re-quote the same levels hour after hour.

    The caps only matter as whole numbers, and prices are keyed to 7 significant digits."""
    if not math.isfinite(price) or price <= 0:
        return None
    return _fit_cached(float(f"{price:.7g}"), int(min(max_currency, 1e12)), int(min(max_items, 1e12)))


# ------------------------------------------------------------------ strategies
def _liquid(ctx: Context, top: int, min_active: float = 0.5, window: int = 48) -> np.ndarray:
    """Boolean mask of the `top` items by traded value over the trailing window that print regularly."""
    value = np.nansum(ctx.history("value", window), axis=0)
    active = np.mean(~np.isnan(ctx.history("price", window)), axis=0)
    eligible = (active >= min_active) & (value > 0)
    order = np.argsort(-np.where(eligible, value, -1))[:top]
    mask = np.zeros(ctx.n_items, dtype=bool)
    mask[order] = True
    return mask & eligible


@dataclass
class Hold:
    """Buy an equal-weight basket of the most traded items once, then hold. The benchmark."""

    top: int = 20
    warmup: int = 24

    def step(self, ctx: Context) -> None:
        bought = ctx.state.get("bought")
        if bought is not None:
            if ctx.t - bought >= 3:  # whatever has not filled by now is left unbought
                ctx.cancel_all()
            return
        if ctx.t < self.warmup:
            return
        mask = _liquid(ctx, self.top, window=self.warmup)
        budget = ctx.bankroll / max(mask.sum(), 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            units = np.where(mask, budget / ctx.fair, 0.0)
        ctx.quote("r", buy_limit=np.where(mask, np.inf, np.nan), buy_units=units)
        ctx.state["bought"] = ctx.t


@dataclass
class Swing:
    """Make markets the way the flip board suggests.

    For the most liquid items: rest a buy in the `buy_leg` market at the level
    its hourly average dipped to in `reach` of recent hours, and offer the
    inventory in the `sell_leg` market at the level that market's average rose to
    in `reach` of hours - but never below cost plus `min_margin`. Legs are "r"
    (against Chaos) and "b" (against Divine); the default buys for Chaos and
    sells for Divine. Inventory still unsold after `stop_days` is dumped at market.
    """

    reach: float = 0.25
    top: int = 40
    sell_leg: str = "b"
    buy_leg: str = "r"
    min_margin: float = 0.03
    per_item: float = 0.05  # max share of the bankroll held in one item
    stop_days: float = 2.0
    warmup: int = 48

    def step(self, ctx: Context) -> None:
        if ctx.t < self.warmup:
            return
        fair = ctx.fair
        buy = fair * np.exp(ctx.swing(self.buy_leg, self.reach))
        sell = fair * np.exp(ctx.swing(self.sell_leg, 1 - self.reach))
        with np.errstate(divide="ignore", invalid="ignore"):
            margin = sell / buy - 1
        want = _liquid(ctx, self.top) & np.isfinite(margin) & (margin >= self.min_margin)
        with np.errstate(divide="ignore", invalid="ignore"):
            room = np.maximum(self.per_item * ctx.bankroll - ctx.position * np.nan_to_num(fair), 0) / buy
        ctx.quote_buys(self.buy_leg, np.where(want, buy, np.nan), np.where(want, room, 0.0))

        held = ctx.position > 0
        opened = ctx.state.setdefault("opened", np.full(ctx.n_items, -1))
        opened[:] = np.where(held, np.where(opened < 0, ctx.t, opened), -1)
        stale = held & (ctx.t - opened > self.stop_days * 24)
        # Like a human flipper: do not offer below what the inventory cost plus the margin worth having.
        # A position that still has not sold after `stop_days` is cut at market instead.
        floor = np.nan_to_num(ctx.average_cost) * (1 + self.min_margin)
        limit = np.where(held, np.maximum(np.where(np.isfinite(sell), sell, fair), floor), np.nan)
        ctx.quote_sells(self.sell_leg, np.where(stale, 0.0, limit), np.where(held, ctx.position, 0.0))


@dataclass
class Reversion:
    """Take liquidity when a print is far below the model's forecast; exit when it normalises."""

    entry_z: float = -2.0
    exit_z: float = 0.0
    top: int = 40
    per_item: float = 0.05
    warmup: int = 48

    def step(self, ctx: Context) -> None:
        if ctx.t < self.warmup:
            return
        z = ctx.z
        fair = ctx.fair
        liquid = _liquid(ctx, self.top)
        enter = liquid & np.isfinite(z) & (z <= self.entry_z) & (ctx.position == 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            units = np.where(enter, self.per_item * ctx.bankroll / fair, 0.0)
        leave = (ctx.position > 0) & np.isfinite(z) & (z >= self.exit_z)
        ctx.quote(
            "r",
            buy_limit=np.where(enter, np.inf, np.nan), buy_units=units,
            sell_limit=np.where(leave, 0.0, np.nan), sell_units=np.where(leave, ctx.position, 0.0),
        )


@dataclass
class Momentum:
    """About once a day, hold the liquid items whose fair value rose most over `lookback` hours."""

    lookback: int = 72
    hold: int = 10
    top: int = 60
    rebalance: int = 24

    def step(self, ctx: Context) -> None:
        last = ctx.state.get("rebalanced")
        if ctx.t < self.lookback + 24 or (last is not None and ctx.t - last < self.rebalance):
            if last is not None and ctx.t - last >= 3:
                ctx.cancel_all()  # orders that did not fill within a few hours are dropped until the next rebalance
            return
        ctx.state["rebalanced"] = ctx.t
        hist = ctx.fair_history(self.lookback + 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = np.log(hist[-1] / hist[0])
        liquid = _liquid(ctx, self.top)
        score = np.where(liquid & np.isfinite(ret), ret, -np.inf)
        winners = np.zeros(ctx.n_items, dtype=bool)
        winners[np.argsort(-score)[: self.hold]] = True
        winners &= np.isfinite(score) & (score > 0)
        fair = ctx.fair
        target_value = ctx.bankroll / max(self.hold, 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            target = np.where(winners, target_value / fair, 0.0)
        delta = target - ctx.position
        ctx.quote(
            "r",
            buy_limit=np.where(delta > 0, np.inf, np.nan), buy_units=np.maximum(delta, 0),
            sell_limit=np.where(delta < 0, 0.0, np.nan), sell_units=np.maximum(-delta, 0),
        )


STRATEGIES: dict[str, type] = {"hold": Hold, "swing": Swing, "reversion": Reversion, "momentum": Momentum}


def load_strategy(spec: str, params: dict | None = None) -> tuple[Strategy, str]:
    """`swing`, or `path/to/file.py:ClassName` for your own."""
    params = params or {}
    if spec in STRATEGIES:
        return STRATEGIES[spec](**params), spec
    path, _, cls_name = spec.partition(":")
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"Unknown strategy {spec!r}. Built in: {', '.join(STRATEGIES)}; or path/to/file.py:ClassName")
    module_spec = importlib.util.spec_from_file_location(file.stem, file)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)  # trusted local code, chosen on the command line
    cls = getattr(module, cls_name or "Strategy")
    return cls(**params), cls.__name__


def run(
    con: duckdb.DuckDBPyConnection,
    league: str,
    strategy: str | Strategy = "swing",
    *,
    params: dict | None = None,
    capital: float = 10_000.0,
    participation: float = 0.1,
    slippage: float = 0.03,
    start_day: float = 0.0,
    days: float | None = None,
    universe: int = 150,
    attention: int = 1,
    compound: bool = False,
    whole_lots: bool = False,
    gold_per_unit: float | dict[str, float] = 0.0,
    gold_budget: float | None = None,
    schedule: str | None = None,
) -> Result:
    """Backtest one strategy on one league.

    `start_day`/`days` select a window by league day. `attention` is how often (in hours) you
    come back to re-post orders. With `compound=False` orders are sized against the starting
    stake, so the result reads as income on a fixed bankroll.

    `whole_lots` makes orders whole-number ratios the purse can post, filled in whole items.
    `gold_per_unit` is the Gold paid per unit posted: one number for every item, or a mapping
    from item id to fee (items it leaves out cost nothing). `gold_budget` caps the Gold spent
    on new orders. `schedule` (e.g. "mon-fri 19-22", UTC) limits visits to your play hours.
    """
    first, last = con.execute(
        "SELECT epoch(min(hour))::BIGINT, epoch(max(hour))::BIGINT FROM prices WHERE league = ?", [league]
    ).fetchall()[0]
    if first is None:
        raise LookupError(f"No prices for league {league!r}")
    since = first + int(start_day * 24) * HOUR
    until = last if days is None else min(last, since + int(days * 24) * HOUR)
    panel = Panel.load(con, league, CORE + LEGS, since=since, until=until)
    # Keep the items that carry the league's trade; thousands of dead columns only cost time.
    totals = np.nansum(panel["value"], axis=0)
    keep = np.argsort(-totals)[:universe]
    keep.sort()
    panel = Panel(
        panel.league, panel.hours, [panel.items[i] for i in keep], [panel.names[i] for i in keep],
        [panel.categories[i] for i in keep], panel.bridge, {k: v[:, keep] for k, v in panel.data.items()},
    )
    if isinstance(strategy, str):
        instance, name = load_strategy(strategy, params)
    else:
        instance, name = strategy, type(strategy).__name__
    if isinstance(gold_per_unit, dict):
        gold_per_unit = np.array([float(gold_per_unit.get(item, 0.0)) for item in panel.items])
    sim = Simulation(panel, capital=capital, participation=participation, slippage=slippage,
                     offset=(since - first) // HOUR, attention=attention, compound=compound,
                     whole_lots=whole_lots, gold_per_unit=gold_per_unit, gold_budget=gold_budget,
                     schedule=schedule)
    return sim.run(instance, name, params)
