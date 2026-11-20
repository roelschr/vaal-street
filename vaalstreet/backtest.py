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

import importlib.util
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import duckdb
import numpy as np

from vaalstreet import pricing
from vaalstreet.panel import CORE, HOUR, LEGS, Panel, ffill, rolling_quantile

LEG_KEYS = ("r", "b")


class Strategy(Protocol):
    def step(self, ctx: "Context") -> None: ...


@dataclass
class Book:
    """Standing orders in one market family: per item, at most one buy and one sell."""

    buy_limit: np.ndarray
    buy_units: np.ndarray
    sell_limit: np.ndarray
    sell_units: np.ndarray

    @classmethod
    def empty(cls, n: int) -> "Book":
        return cls(np.full(n, np.nan), np.zeros(n), np.full(n, np.nan), np.zeros(n))


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
        """Level (as log distance from fair) the leg's hourly VWAP stayed below in `reach` of recent hours."""
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
    ) -> None:
        self.panel = panel
        self.participation = participation
        self.slippage = slippage
        self.offset = offset
        self.attention = max(1, int(attention))
        self.compound = compound
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
        self.bought = 0.0
        self.sold = 0.0

    def swing(self, leg: str, reach: float, window: int = 48) -> np.ndarray:
        """Causal rolling quantile of log(leg VWAP / prior fair): [T, N]."""
        key = (leg, reach)
        if key not in self._swing:
            prior = np.vstack([self.fair[:1], self.fair[:-1]])
            with np.errstate(divide="ignore", invalid="ignore"):
                dev = np.log(self.panel[f"{leg}_price"] / prior)
            self._swing[key] = rolling_quantile(np.where(np.isfinite(dev), dev, np.nan), window, reach, min_count=12)
        return self._swing[key]

    def equity_now(self, t: int) -> float:
        return float(self.cash + np.nansum(self.position * np.nan_to_num(self._mark[t])))

    def _execute(self, t: int) -> None:
        """Match the standing orders against hour t's markets."""
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
                proceeds = units * price
                with np.errstate(divide="ignore", invalid="ignore"):
                    avg_cost = np.where(self.position > 0, self.cost / self.position, 0.0)
                self.realized += proceeds - units * avg_cost
                self.cost -= units * avg_cost
                self.position -= units
                self.cash += float(proceeds.sum())
                self.sold += float(proceeds.sum())
                self.fills += int(hit.sum())
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
                cost = cost * scale
                self.position += units
                self.cost += cost
                self.cash -= float(cost.sum())
                self.bought += float(cost.sum())
                self.fills += int(hit.sum())
                book.buy_units = book.buy_units - units

    def run(self, strategy: Strategy, name: str | None = None, params: dict | None = None) -> Result:
        T = len(self.panel.hours)
        ctx = Context(self)
        equity = np.empty(T)
        cash = np.empty(T)
        for t in range(T):
            if t > 0:
                self._execute(t)
            ctx.t = t
            if t % self.attention == 0:  # between visits the standing orders just sit there
                strategy.step(ctx)
            equity[t] = self.equity_now(t)
            cash[t] = self.cash
        bridge = self.panel.bridge
        # The first hours of a league print wild Divine prices; anchor the benchmark a day in.
        known = np.flatnonzero(np.isfinite(bridge))
        first = bridge[known[known >= min(24, T - 1)][0]] if len(known) and (known >= min(24, T - 1)).any() else np.nan
        return Result(
            self.panel.league, name or type(strategy).__name__, self.panel.hours, equity, cash,
            self.start_capital, self.fills, self.bought, self.sold, self.realized, self.panel.names, params or {},
            self._benchmark(bridge, first, known),
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
        if ctx.t != self.warmup:
            if ctx.t == self.warmup + 3:
                ctx.cancel_all()
            return
        mask = _liquid(ctx, self.top, window=self.warmup)
        budget = ctx.bankroll / max(mask.sum(), 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            units = np.where(mask, budget / ctx.fair, 0.0)
        ctx.quote("r", buy_limit=np.where(mask, np.inf, np.nan), buy_units=units)


@dataclass
class Swing:
    """Make markets the way the flip board suggests.

    Every hour, for the most liquid items: rest a buy in the Chaos market at the
    level its hourly average dipped to in `reach` of recent hours, and offer the
    inventory at the level the `sell_leg` market's average rose to in `reach` of
    hours - but never below cost plus `min_margin`. `sell_leg="b"` sells into the
    Divine market (the cross-market plan), `"r"` stays in the Chaos market.
    Inventory that has not sold after `stop_days` is dumped at market.
    """

    reach: float = 0.25
    top: int = 40
    sell_leg: str = "b"
    min_margin: float = 0.03
    per_item: float = 0.05  # max share of equity held in one item
    stop_days: float = 2.0  # dump inventory held this long without a sale, at market
    warmup: int = 48

    def step(self, ctx: Context) -> None:
        if ctx.t < self.warmup:
            return
        fair = ctx.fair
        buy = fair * np.exp(ctx.swing("r", self.reach))
        sell = fair * np.exp(ctx.swing(self.sell_leg, 1 - self.reach))
        with np.errstate(divide="ignore", invalid="ignore"):
            margin = sell / buy - 1
        want = _liquid(ctx, self.top) & np.isfinite(margin) & (margin >= self.min_margin)
        with np.errstate(divide="ignore", invalid="ignore"):
            room = np.maximum(self.per_item * ctx.bankroll - ctx.position * np.nan_to_num(fair), 0) / buy
        ctx.quote_buys("r", np.where(want, buy, np.nan), np.where(want, room, 0.0))

        held = ctx.position > 0
        age = ctx.state.setdefault("age", np.zeros(ctx.n_items))
        age[:] = np.where(held, age + 1, 0)
        stale = held & (age > self.stop_days * 24)
        # Like a human flipper: do not offer below what the inventory cost plus the margin worth having.
        # A position that still has not sold after `stop_days` is cut at market instead.
        floor = np.nan_to_num(ctx.average_cost) * (1 + self.min_margin)
        limit = np.where(held, np.maximum(np.where(np.isfinite(sell), sell, fair), floor), np.nan)
        if self.sell_leg == "r":
            ctx.quote_sells("r", np.where(stale, 0.0, limit), np.where(held, ctx.position, 0.0))
        else:
            ctx.quote_sells(self.sell_leg, limit, np.where(held & ~stale, ctx.position, 0.0))
            ctx.quote_sells("r", np.where(stale, 0.0, np.nan), np.where(stale, ctx.position, 0.0))


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
    """Once a day, hold the liquid items whose fair value rose most over `lookback` hours."""

    lookback: int = 72
    hold: int = 10
    top: int = 60
    rebalance: int = 24

    def step(self, ctx: Context) -> None:
        if ctx.t < self.lookback + 24 or ctx.t % self.rebalance:
            if ctx.t % self.rebalance == 3:
                ctx.cancel_all()
            return
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
) -> Result:
    """Backtest one strategy on one league.

    `start_day`/`days` select a window by league day. `attention` is how often (in hours) you
    come back to re-post orders. With `compound=False` orders are sized against the starting
    stake, so the result reads as income on a fixed bankroll.
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
    sim = Simulation(panel, capital=capital, participation=participation, slippage=slippage,
                     offset=(since - first) // HOUR, attention=attention, compound=compound)
    return sim.run(instance, name, params)
