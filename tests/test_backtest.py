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


def test_strategies_behave_the_same_whether_you_visit_hourly_or_rarely(con):
    for spec in ("hold", "momentum"):
        hourly = backtest.run(con, OLD, spec, capital=1000, slippage=0.0)
        rarely = backtest.run(con, OLD, spec, capital=1000, slippage=0.0, attention=5)
        assert rarely.fills <= hourly.fills * 2 + 5, f"{spec}: stale orders must not keep filling between visits"
        assert rarely.total_return == pytest.approx(hourly.total_return, abs=0.25)


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


# ------------------------------------------------------------- small purses, Gold and play time
def test_schedule_parser_reads_days_hours_and_windows():
    mask = backtest.parse_schedule("mon-fri 19-22")
    assert mask.sum() == 5 * 3, "the end hour is not included: 19, 20 and 21 on five days"
    assert mask[0, 19] and mask[4, 21] and not mask[0, 22] and not mask[5, 19]
    both = backtest.parse_schedule("sat,sun 10-14; wed 20-23")
    assert both.sum() == 2 * 4 + 3 and both[6, 13] and both[2, 22]
    late = backtest.parse_schedule("fri 22-2")
    assert sorted(map(tuple, np.argwhere(late))) == [(4, 22), (4, 23), (5, 0), (5, 1)], "runs past midnight"
    assert backtest.parse_schedule("18-20").sum() == 7 * 2, "no days means every day"
    assert backtest.parse_schedule("sat 0-24").sum() == 24
    assert backtest.parse_schedule("fri-mon 1-2").any(axis=1).tolist() == [1, 0, 0, 0, 1, 1, 1]
    for bad in ("", "mon", "someday 1-2", "mon 25-3", "mon 3-3", "mon 19:00-22:00"):
        with pytest.raises(ValueError):
            backtest.parse_schedule(bad)


def test_a_scheduled_run_visits_only_inside_its_windows():
    calls = []

    class Counter:
        def step(self, ctx):
            calls.append(ctx.t)

    # tiny_panel's row 0 is the hour starting Thursday 1970-01-01 00:00 UTC. It is published at 01:00,
    # so the visit acting on row t happens at clock hour t + 1. "thu 3-6" is clock 03, 04 and 05: rows 2-4.
    backtest.Simulation(tiny_panel([1] * 48), schedule="thu 3-6").run(Counter())
    assert calls == [2, 3, 4]
    calls.clear()
    # attention counts from the start of each session; Friday 03-06 is rows 26-28.
    backtest.Simulation(tiny_panel([1] * 48), schedule="thu,fri 3-6", attention=2).run(Counter())
    assert calls == [2, 4, 26, 28]


def test_orders_stand_while_you_are_away():
    # Posted at the only visit (row 2); the price dips at row 10, long after the session ended.
    prices = [10.0] * 10 + [9.0] * 4
    sim = backtest.Simulation(tiny_panel(prices), capital=1000, participation=1.0, schedule="thu 3-4")
    result = sim.run(Script({2: [("buy", 5, 9.5)]}))
    assert sim.position[0] == 5 and result.cash[10] == pytest.approx(1000 - 5 * 9.5)


def test_whole_lots_fill_whole_items_at_the_ratio_the_order_states():
    sim = backtest.Simulation(tiny_panel([4, 3, 3, 3]), capital=1000, participation=1.0, whole_lots=True)
    result = sim.run(Script({0: [("buy", 25, 3.31)]}))
    # 3.31 Chaos each becomes "33 Chaos for 10", a lot of ten: 25 wanted means two lots, 20 items at 3.30.
    assert sim.position[0] == 20 and result.fills == 1
    assert result.cash[1] == pytest.approx(1000 - 66)
    assert sim.books["r"].buy_units[0] == 0, "the five items short of a lot were never posted"


def test_whole_lot_orders_never_state_a_worse_price_than_their_limit():
    from vaalstreet import pricing

    for limit in (3.31, 0.0937, 7.77, 1.234, 0.31, 151.5, 2.0 / 3):
        c, i = pricing.fit_ratio(limit, 1000, side="buy")
        assert c / i <= limit + 1e-12, f"a buy at {limit} posts {c}/{i}"
        c, i = pricing.fit_ratio(limit, 1000, side="sell")
        assert c / i >= limit - 1e-12, f"a sell at {limit} posts {c}/{i}"
    # In the simulator: 3.31 is bought at "33 for 10" (3.30), and a sale at 3.31 is offered at or above it.
    sim = backtest.Simulation(tiny_panel([4, 3, 4, 4, 4]), capital=1000, participation=1.0, whole_lots=True)
    sim.run(Script({0: [("buy", 25, 3.31)], 2: [("sell", 20, 3.31)]}))
    # The sale rounds up to "53 for 16" (3.3125): one lot of 16 sells, four items stay.
    assert sim.position[0] == 4
    assert sim.realized[0] == pytest.approx(16 * (53 / 16 - 3.30))


def test_a_purse_too_small_for_one_lot_posts_nothing():
    sim = backtest.Simulation(tiny_panel([10, 9, 9, 9]), capital=5, participation=1.0, whole_lots=True)
    result = sim.run(Script({0: [("buy", 5, 9.5)]}))
    assert result.fills == 0 and sim.position[0] == 0 and result.cash[-1] == 5
    # Without whole lots the same purse buys half an item.
    loose = backtest.Simulation(tiny_panel([10, 9, 9, 9]), capital=5, participation=1.0)
    loose.run(Script({0: [("buy", 5, 9.5)]}))
    assert 0 < loose.position[0] < 1


def test_a_divine_market_order_trades_whole_divines_at_the_fill_hours_divine_price():
    T = 3
    nan = np.full((T, 1), np.nan)
    data = {c: nan.copy() for c in CORE + LEGS}
    data.update(price=np.full((T, 1), 9.0), value=np.full((T, 1), 900.0), volume=np.full((T, 1), 100.0),
                b_price=np.full((T, 1), 9.0), b_volume=np.full((T, 1), 100.0))
    bridge = np.array([100.0, 110.0, 110.0])
    panel = Panel("T", np.arange(T) * 3600, ["x"], ["X"], ["Currency"], bridge, data)

    class BuyForDivines:
        def step(self, ctx):
            if ctx.t == 0:
                ctx.buy(0, 80, 9.6, leg="b")

    sim = backtest.Simulation(panel, capital=1000, participation=1.0, whole_lots=True)
    result = sim.run(BuyForDivines())
    # 9.6 Chaos at 100 a Divine is 0.096 Divine each; rounded down within 10 Divines: "7 Divines for 73".
    # Row 1 the Divine is 110, so the order stands at 10.55 Chaos each and the 9.0 average trades through it.
    assert sim.position[0] == 73
    assert result.cash[1] == pytest.approx(1000 - 7 * 110), "seven whole Divines, at the hour they are paid"


class Requote:
    """Buys at 9.5 at row 0, re-quotes the same order at row 1 and moves the limit at row 2."""

    def step(self, ctx):
        if ctx.t in (0, 1):
            ctx.buy(0, 10, 9.5)
        elif ctx.t == 2:
            ctx.buy(0, 10, 9.4)


def test_gold_is_charged_per_unit_posted_and_again_on_a_repost():
    sim = backtest.Simulation(tiny_panel([10] * 5), capital=1000, gold_per_unit=3)
    result = sim.run(Requote())
    # Row 0 posts 10 units (30 Gold); row 1 leaves the order as it was (free); row 2 moves it (30 more).
    assert result.gold.tolist() == [30, 30, 60, 60, 60]
    assert result.summary()["gold_spent"] == 60
    assert backtest.Simulation(tiny_panel([10] * 5)).run(Requote()).summary()["chaos_per_1000_gold"] is None


def test_a_whole_lot_requote_that_rounds_to_the_standing_order_is_not_a_new_post():
    class Same:
        def step(self, ctx):
            if ctx.t in (0, 1):
                ctx.buy(0, 25, 3.31)

    sim = backtest.Simulation(tiny_panel([4] * 4), capital=1000, whole_lots=True, gold_per_unit=3)
    result = sim.run(Same())
    # Both quotes become "33 Chaos for 10", two lots: 20 units posted once, 60 Gold.
    assert result.gold.tolist() == [60, 60, 60, 60]


def test_a_spent_gold_budget_stops_new_orders_but_standing_ones_still_fill():
    sim = backtest.Simulation(tiny_panel([10, 10, 10, 9, 9]), capital=1000, participation=1.0,
                              gold_per_unit=3, gold_budget=50)
    result = sim.run(Requote())
    # The move at row 2 would cost 30 more than the 20 left, so the 9.5 order stays and fills at row 3.
    assert result.gold[-1] == 30 and sim.books["r"].buy_limit[0] == 9.5
    assert sim.position[0] == 10 and result.cash[3] == pytest.approx(1000 - 95)
    summary = result.summary()
    assert summary["chaos_per_1000_gold"] == pytest.approx((result.equity[-1] - 1000) / 30 * 1000, abs=0.01)


def test_new_options_stay_causal_when_the_league_is_cut_short(con):
    """Results up to hour T must not change when the hours after T are removed."""
    full = Panel.load(con, NEW, CORE + LEGS)
    # Monday-to-Friday evenings plus a weekend window, so the run crosses gaps between sessions.
    options = dict(capital=600, participation=0.1, whole_lots=True, gold_per_unit=0.5, gold_budget=4000,
                   schedule="mon-fri 17-23; sat,sun 8-20", attention=2)
    whole = backtest.Simulation(full, **options).run(backtest.Swing(per_item=0.5, warmup=24))
    assert whole.fills > 0 and whole.gold[-1] > 0 and np.all(np.diff(whole.gold) >= 0)
    assert whole.gold[59] > 0 and whole.cash[59] != whole.cash[0], "the shortest cut already traded and paid Gold"
    queued = backtest.Simulation(full, fill_model="queue", **options).run(backtest.Swing(per_item=0.5, warmup=24))
    assert queued.fills > 0 and queued.risk.max() > 0
    for cut in (60, 101, 150):
        for model, ref in (("share", whole), ("queue", queued)):
            part = backtest.Simulation(full.window(0, cut), fill_model=model, **options).run(
                backtest.Swing(per_item=0.5, warmup=24))
            for series in ("equity", "cash", "gold", "holdings", "risk"):
                np.testing.assert_allclose(getattr(part, series), getattr(ref, series)[:cut], err_msg=f"{model} {series}")


def test_whole_lots_and_gold_on_the_synthetic_league(con):
    loose = backtest.run(con, NEW, "swing", params={"per_item": 0.5}, capital=2000)
    whole = backtest.run(con, NEW, "swing", params={"per_item": 0.5}, capital=2000, whole_lots=True,
                         gold_per_unit={"Metadata/Items/Currency/CurrencyTestOrb": 1.0})
    assert whole.fills > 0 and 0 < whole.total_return <= loose.total_return + 0.05
    assert whole.gold_spent > 0 and whole.summary()["chaos_per_1000_gold"] > 0
    assert np.all(np.diff(whole.gold) >= 0), "Gold is never given back"


def queue_panel(prices, volume=100.0, bid=2375.0, ask=250.0):
    """tiny_panel with resting stock: `ask` items on offer and `bid` Chaos bid for the item, every hour."""
    panel = tiny_panel(prices, volume)
    panel.data["r_stock_lo"] = np.full((len(prices), 1), ask)
    panel.data["r_bid_lo"] = np.full((len(prices), 1), bid)
    return panel


def test_a_queued_buy_waits_for_the_stock_ahead_of_it():
    # 2375 Chaos bid at a 9.5 limit is 250 items ahead. Only hours that trade through 9.5 eat into
    # the queue: hour 1 (100 left 150), hour 3 (50 left), hour 4 fills 50 of 80, hour 5 the last 30.
    sim = backtest.Simulation(queue_panel([10, 9, 10, 9, 9, 9]), capital=1000, fill_model="queue")
    result = sim.run(Script({0: [("buy", 80, 9.5)]}))
    assert list(result.cash) == [1000, 1000, 1000, 1000, 1000 - 50 * 9.5, 1000 - 80 * 9.5]
    share = backtest.Simulation(queue_panel([10, 9, 10, 9, 9, 9]), capital=1000).run(Script({0: [("buy", 80, 9.5)]}))
    assert share.cash[1] == 1000 - 10 * 9.5, "the default model fills 10% of the hour at once"


def test_a_new_price_goes_to_the_back_of_the_queue_and_market_orders_do_not_queue():
    sim = backtest.Simulation(queue_panel([10, 9, 10, 9, 9, 9]), capital=1000, fill_model="queue")
    sim.run(Script({0: [("buy", 80, 9.5)], 2: [("buy", 80, 9.6)]}))
    # Re-posted at 9.6 after hour 2: 2375 / 9.6 = 247.4 ahead again, 47.4 left after hours 3 and 4.
    assert sim.position[0] == pytest.approx(100 - (2375 / 9.6 - 200))
    taker = backtest.Simulation(queue_panel([10, 10, 10]), capital=1000, fill_model="queue", slippage=0.0)
    taker.run(Script({0: [("buy", 1000, float("inf"))]}))
    assert taker.position[0] == pytest.approx(20), "market orders keep the 10% share of each hour"


def test_inventory_risk_is_the_loss_on_a_one_range_move():
    from vaalstreet import pricing

    prices = [10, 10.5, 9.8, 10.2, 10, 10.1, 9.9, 10]
    sim = backtest.Simulation(tiny_panel(prices), capital=1000, participation=1.0, slippage=0.0)
    result = sim.run(Script({2: [("buy", 20, float("inf"))]}))
    fv = pricing.fair_value(np.array(prices)[:, None], np.array(prices)[:, None] * 100)
    held = np.array([0, 0, 0] + [20] * 5)
    np.testing.assert_allclose(result.holdings, held * fv.fair[:, 0])
    np.testing.assert_allclose(result.risk, held * (fv.fair - fv.low)[:, 0])
    summary = result.summary()
    assert summary["peak_risk"] == pytest.approx(result.risk.max(), abs=0.01) and summary["peak_risk"] > 0
    assert summary["average_risk"] == pytest.approx(result.risk.mean(), abs=0.01)
    assert summary["peak_holdings"] == pytest.approx(result.holdings.max(), abs=0.01)


def test_the_queue_reads_the_published_stock_on_your_side(con):
    sim = backtest.Simulation(Panel.load(con, NEW, CORE + LEGS), fill_model="queue")
    orb = sim.panel.index("Test Orb")
    # The synthetic digests rest 500 test orbs (lowest stock) and 500 Chaos bid for them, every hour.
    assert sim._queue_at(50, "r", "sell", orb, 11.0) == 500
    assert sim._queue_at(50, "r", "buy", orb, 10.0) == pytest.approx(50)
    assert sim._queue_at(50, "r", "buy", orb, float("inf")) == 0
    swing = backtest.run(con, NEW, "swing", params={"per_item": 0.5}, capital=2000, fill_model="queue")
    assert swing.fills > 0


def test_a_portfolio_settles_in_the_order_given_on_shared_cash_and_volume():
    first, second = Script({0: [("buy", 1000, float("inf"))]}), Script({0: [("buy", 1000, float("inf"))]})
    folio = backtest.Portfolio(tiny_panel([10] * 4, volume=100), capital=250, participation=0.1, slippage=0.0)
    result = folio.run([(first, "first", {}), (second, "second", {})])
    a, b = folio.sims
    # Each hour 10% of 100 units may be taken, and the first strategy takes all ten; then cash runs out.
    assert a.position[0] == 25 and b.position[0] == 0
    assert result.cash[-1] == pytest.approx(0) and [m["strategy"] for m in result.members] == ["first", "second"]
    assert result.members[0]["peak_capital"] == 250 and result.members[1]["peak_capital"] == 0
    assert result.members[1]["fills"] == 0, "a buy that finds no cash left is not a fill"


def test_portfolio_shares_add_up_and_one_strategy_alone_matches_a_plain_run(con):
    # Two flippers at different patience share 2000 Chaos; hold would spend it all before swing's warmup ends.
    specs = ['swing:{"per_item": 0.3}', 'swing:{"per_item": 0.3, "reach": 0.4}']
    folio = backtest.run_portfolio(con, NEW, specs, capital=2000, gold_per_unit=0.2)
    profits = [m["profit"] for m in folio.summary()["strategies"]]
    assert all(m["fills"] > 0 and m["peak_capital"] > 0 for m in folio.members), "both strategies trade"
    assert sum(profits) == pytest.approx(folio.equity[-1] - 2000, abs=0.05)
    assert folio.gold_spent == pytest.approx(sum(m["gold_spent"] for m in folio.members), abs=1)
    alone = backtest.run_portfolio(con, NEW, specs[:1], capital=2000, gold_per_unit=0.2)
    plain = backtest.run(con, NEW, "swing", params={"per_item": 0.3}, capital=2000, gold_per_unit=0.2)
    np.testing.assert_array_equal(alone.equity, plain.equity)
    assert backtest.parse_spec('x.py:Mine:{"a": 1}') == ("x.py:Mine", {"a": 1})
    with pytest.raises(ValueError):
        backtest.parse_spec("swing:{oops}")


def test_portfolio_runs_stay_causal_when_the_league_is_cut_short(con):
    full = Panel.load(con, NEW, CORE + LEGS)
    options = dict(capital=900, whole_lots=True, gold_per_unit=0.5, gold_budget=3000, fill_model="queue")

    def folio(panel):
        return backtest.Portfolio(panel, **options).run([
            (backtest.Swing(per_item=0.5, warmup=24), "swing", {}),
            (backtest.Hold(top=3, warmup=30), "hold", {}),
        ])

    whole = folio(full)
    assert all(m["fills"] > 0 for m in whole.members), "both strategies trade before the shortest cut"
    for cut in (60, 101, 150):
        part = folio(full.window(0, cut))
        for series in ("equity", "cash", "gold", "holdings", "risk"):
            np.testing.assert_allclose(getattr(part, series), getattr(whole, series)[:cut], err_msg=series)


def test_the_desk_api_takes_the_fill_model_and_a_portfolio(server):
    from conftest import call

    status, queued = call(f"{server}/api/backtest", {"strategy": "swing", "capital": 2000, "fill_model": "queue"})
    assert status == 200 and "peak_risk" in queued["summary"]
    status, folio = call(f"{server}/api/backtest", {"capital": 2000, "strategies": [
        {"strategy": "swing", "params": {"per_item": 0.5}}, {"strategy": "reversion"}]})
    assert status == 200 and [m["strategy"] for m in folio["summary"]["strategies"]] == ["swing", "reversion"]
    assert call(f"{server}/api/backtest", {"strategies": [{"strategy": "/tmp/x.py:X"}]})[0] == 400
    assert call(f"{server}/api/backtest", {"strategy": "swing", "fill_model": "magic"})[0] == 400


EXAMPLE = '''import numpy as np


class Patient:
    def __init__(self, cut=0.95):
        self.cut = cut

    def step(self, ctx):
        if ctx.t >= 24:
            ctx.quote_buys("r", ctx.fair * self.cut, np.where(np.isfinite(ctx.fair), 5.0, 0.0))
'''


def test_a_strategy_exports_with_its_results_and_imports_only_with_consent(archive_root, tmp_path, monkeypatch, capsys):
    import json

    from vaalstreet import cli

    mine = tmp_path / "mine" / "my strategy.py"
    mine.parent.mkdir()
    mine.write_text(EXAMPLE)
    shared = tmp_path / "shared.json"
    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    cli.main(["backtest", f"{mine}:Patient", "--league", NEW, "--league", OLD, "--params", '{"cut": 0.97}',
              "--capital", "2000", "--fill-model", "queue", "--export", str(shared), "--json"])
    doc = json.loads(shared.read_text())
    assert doc["params"] == {"cut": 0.97} and doc["options"]["fill_model"] == "queue"
    assert [r["league"] for r in doc["results"]] == [NEW, OLD] and doc["source"] == EXAMPLE
    assert doc["class"] == "Patient" and doc["strategy"] == "my strategy.py:Patient", "no local folders in the file"
    assert doc["code_version"] != "unknown" and str(tmp_path) not in shared.read_text()

    home = tmp_path / "home"
    monkeypatch.setenv("VAAL_DATA_DIR", str(home))
    capsys.readouterr()
    cli.main(["strategy", "import", str(shared)])
    out = capsys.readouterr().out
    assert "WARNING" in out and "class Patient" in out and "Nothing was saved" in out
    assert not (home / "strategies").exists(), "without --yes nothing is written"

    cli.main(["strategy", "import", str(shared), "--yes"])
    saved = home / "strategies" / "my_strategy.py"
    assert saved.read_text() == EXAMPLE and f"{saved}:Patient" in capsys.readouterr().out
    cli.main(["strategy", "import", str(shared), "--yes"])
    assert (home / "strategies" / "my_strategy_2.py").exists(), "an import never overwrites"

    doc.update(file="../../evil.py", **{"class": "not a name"})
    shared.write_text(json.dumps(doc))
    with pytest.raises(SystemExit):
        cli.main(["strategy", "import", str(shared), "--yes"])
    assert not (tmp_path / "evil.py").exists()
    doc.update(file="../../evil.py", **{"class": "Patient"})
    shared.write_text(json.dumps(doc))
    cli.main(["strategy", "import", str(shared), "--yes"])
    assert (home / "strategies" / "evil.py").exists() and not (tmp_path / "evil.py").exists(), "stays in the folder"
    del doc["source"]
    shared.write_text(json.dumps(doc))
    with pytest.raises(SystemExit):
        cli.main(["strategy", "import", str(shared)])


def test_a_portfolio_from_the_command_line(archive_root, monkeypatch, capsys):
    from vaalstreet import cli

    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    cli.main(["backtest", 'swing:{"per_item": 0.5}', "reversion", "--portfolio", "--league", NEW, "--capital", "2000"])
    out = capsys.readouterr().out
    assert "Worst drop" in out and "Share" in out and "swing + reversion" in out
    with pytest.raises(SystemExit):
        cli.main(["backtest", "swing", "reversion", "--league", NEW])
