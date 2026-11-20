"""Execution rules of the simulator, and the built-in strategies on the synthetic league."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import NEW, OLD
from vaalstreet import backtest
from vaalstreet.panel import CORE, LEGS, Panel


def tiny_panel(prices, volume=100.0):
    """One item, Chaos market only, with the given hourly VWAPs."""
    T = len(prices)
    col = np.array(prices, dtype=float)[:, None]
    nan = np.full((T, 1), np.nan)
    data = {c: nan.copy() for c in CORE + LEGS}
    data.update(price=col, value=col * volume, volume=np.full((T, 1), volume), r_price=col.copy(),
                r_volume=np.full((T, 1), volume))
    return Panel("T", np.arange(T) * 3600, ["x"], ["X"], ["Currency"], np.full(T, 100.0), data)


class Script:
    """Posts the given orders at the given hours and records what it could see."""

    def __init__(self, orders):
        self.orders, self.seen = orders, []

    def step(self, ctx):
        self.seen.append(ctx.history("price").shape[0])
        for kind, units, limit in self.orders.get(ctx.t, []):
            (ctx.buy if kind == "buy" else ctx.sell)(0, units, limit)


def test_resting_buy_fills_at_its_limit_only_when_the_average_trades_through():
    sim = backtest.Simulation(tiny_panel([10, 10, 9.6, 9.0, 9.0]), capital=1000, participation=1.0)
    result = sim.run(Script({0: [("buy", 5, 9.5)]}))
    # Hour 1 (10) and hour 2 (9.6) stay above the limit; hour 3 averages 9.0 and fills at 9.5, not 9.0.
    assert list(result.cash) == [1000, 1000, 1000, 1000 - 5 * 9.5, 1000 - 5 * 9.5]
    assert sim.position[0] == 5 and result.fills == 1


def test_orders_meet_the_next_hour_and_strategies_see_only_the_past():
    script = Script({0: [("buy", 1, float("inf"))]})
    sim = backtest.Simulation(tiny_panel([10, 20, 30]), capital=1000, participation=1.0, slippage=0.05)
    result = sim.run(script)
    assert script.seen == [1, 2, 3]
    assert result.cash[0] == 1000, "nothing fills in the hour the order is posted"
    assert result.cash[1] == pytest.approx(1000 - 20 * 1.05), "a market order pays next hour's average plus slippage"


def test_fills_respect_volume_cash_and_inventory():
    sim = backtest.Simulation(tiny_panel([10, 10, 10, 10], volume=100), capital=300, participation=0.1, slippage=0.0)
    result = sim.run(Script({0: [("buy", 1000, float("inf"))], 2: [("sell", 1000, 0.0)]}))
    # 10% of 100 units per hour: 10 units in hour 1, 10 more in hour 2, then cash (300) is spent.
    assert result.cash[1] == pytest.approx(200) and result.cash[2] == pytest.approx(100)
    # The standing buy keeps working in hour 3 alongside the sell: sells go first, capped by volume.
    assert sim.position[0] >= 0 and result.cash.min() >= -1e-9
    assert result.equity[-1] == pytest.approx(300), "no slippage, flat price: nothing gained or lost"


def test_sale_realises_profit_against_average_cost():
    sim = backtest.Simulation(tiny_panel([10, 10, 12, 12, 12]), capital=1000, participation=1.0, slippage=0.0)
    result = sim.run(Script({0: [("buy", 10, float("inf"))], 2: [("sell", 4, 11.0)]}))
    assert sim.realized[0] == pytest.approx(4 * (11 - 10)), "a resting sale gets its limit, never better"
    assert sim.position[0] == 6
    assert result.summary()["top_items"] == [{"name": "X", "profit": 4.0}]


def test_attention_leaves_orders_standing_between_visits():
    calls = []

    class Counter:
        def step(self, ctx):
            calls.append(ctx.t)

    backtest.Simulation(tiny_panel([1] * 10), attention=4).run(Counter())
    assert calls == [0, 4, 8]


def test_swing_earns_the_premium_and_takers_pay_for_it(con):
    # Only a few items exist here, so let one item take half the bankroll.
    swing = backtest.run(con, NEW, "swing", params={"per_item": 0.5}, capital=2000, participation=0.1)
    assert swing.total_return > 0.2, "buying in the Chaos market and selling in the Divine market pays"
    assert swing.fills > 20 and swing.max_drawdown < 0.1
    same = backtest.run(con, NEW, "swing", params={"sell_leg": "r", "per_item": 0.5}, capital=2000)
    assert 0 < same.total_return < swing.total_return
    dip = backtest.run(con, NEW, "reversion", params={"entry_z": -1.0}, capital=2000)
    assert dip.total_return <= 0.02, "paying the spread on a mean-reverting wiggle does not"
    summary = swing.summary()
    assert summary["league"] == NEW and summary["divine_return"] > 0 and summary["days"] == 8.0


def test_hold_tracks_the_market_and_windows_select_league_days(con):
    held = backtest.run(con, OLD, "hold", capital=1000, slippage=0.0)
    assert held.total_return > 0.3, "the old league's orb trends up"
    window = backtest.run(con, OLD, "hold", capital=1000, start_day=5, days=3)
    assert len(window.hours) == 3 * 24 + 1


def test_custom_strategy_is_loaded_from_a_file(tmp_path, con):
    module = tmp_path / "mine.py"
    module.write_text(
        "class Idle:\n"
        "    def __init__(self, note='x'):\n        self.note = note\n"
        "    def step(self, ctx):\n        pass\n"
    )
    result = backtest.run(con, NEW, f"{module}:Idle", params={"note": "y"})
    assert result.strategy == "Idle" and result.total_return == 0
    with pytest.raises(ValueError):
        backtest.load_strategy("no-such-strategy")
