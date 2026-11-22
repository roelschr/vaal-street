"""Flip levels follow each market's own price, in the desk, the replay and the trade check alike.

Known answer: in the `scrap_root` archive, Test Scrap trades at about 2 Chaos in its Chaos market and
2.8 times that, about 5.6 Chaos, in its Divine market. Its blended fair value lies between the two.
A Divine-market level must sit near 5.6 Chaos. Capped at 1.5 times the blended value, it could not.
"""

from __future__ import annotations

import numpy as np
import pytest

from conftest import NEW, SCRAP, SCRAP_PREMIUM
from vaalstreet import backtest, check, pricing
from vaalstreet.desk import LOT_WINDOW, REPLAY_STAKE, SWING_WINDOW, Desk
from vaalstreet.panel import weighted_quantile


@pytest.fixture(scope="module")
def desk(scrap_root):
    from vaalstreet.db import connect

    con = connect(scrap_root)
    yield Desk.build(con, NEW, root=scrap_root)
    con.close()


def test_divine_market_levels_sit_near_that_markets_own_price(desk):
    i = desk.panel.index(SCRAP)
    chaos_side, divine_side = desk.legs["r"].fair[-1, i], desk.legs["b"].fair[-1, i]
    assert divine_side / chaos_side == pytest.approx(SCRAP_PREMIUM, rel=0.1)
    blended = desk.fv.fair[-1, i]
    plans = {p["kind"]: p for p in desk.flips(min_margin=0.0) if p["id"] == SCRAP}
    for kind in ("divine", "cross"):
        assert plans[kind]["sell"] > 1.5 * blended, f"the {kind} flip's sell is capped at the blended value"
        assert plans[kind]["sell"] == pytest.approx(divine_side, rel=0.15)
    assert plans["divine"]["buy"] == pytest.approx(divine_side, rel=0.15)
    assert plans["cross"]["buy"] == pytest.approx(chaos_side, rel=0.15)


def test_replay_uses_the_same_levels_as_the_board(desk):
    """`Simulation.swing` (what `replay` and the Swing backtest quote) agrees with `Desk.flips` at the last hour."""
    i = desk.panel.index(SCRAP)
    sim = backtest.Simulation(desk.panel)
    for leg in ("r", "b"):
        leg_state = desk.legs[leg]
        for reach in (0.25, 0.75):
            board = desk.fv.fair[-1, i] * np.exp(
                weighted_quantile(leg_state.dev[:, i:i + 1].T, leg_state.weight[:, i:i + 1].T, reach)[0])
            replayed = sim.fair[-1, i] * np.exp(sim.swing(leg, reach, SWING_WINDOW)[-1, i])
            assert replayed == pytest.approx(board, rel=1e-6)


def test_a_replay_says_what_it_made_per_hour(desk):
    """A replay's return is on a 1,000 Chaos stake over `hours` hours: per hour it is return x 1,000 / hours,
    and against Divines it is that stake counted in Divines' worth at the start, less the stake."""
    replays = [p["replay"] for p in desk.flips(min_margin=0.0) if p["replay"]]
    assert replays
    for r in replays:
        assert r["stake"] == REPLAY_STAKE, "the page names the stake the rate is on"
        assert r["per_hour"] == pytest.approx(r["return"] * REPLAY_STAKE / r["hours"], rel=0.01, abs=1e-3)
        assert r["vs_divine_per_hour"] == pytest.approx(
            ((1 + r["return"]) / (1 + r["divines"]) - 1) * REPLAY_STAKE / r["hours"], rel=0.02, abs=0.02)
        # The synthetic Divine rises, so beating it is the higher bar.
        assert r["divines"] > 0 and r["vs_divine_per_hour"] < r["per_hour"]


def test_trade_check_and_flip_agree_on_the_divine_market(desk):
    """A Divine-market sell at that market's own value matches the flip's sell; it does not "beat" a capped one."""
    i = desk.panel.index(SCRAP)
    value = desk.legs["b"].fair[-1, i]
    per_divine = round(desk.bridge / value)
    r = check.check(desk, "Test Scrap", "divine", "sell", per_divine, 1)
    assert r["verdict"] == "caution"
    assert r["plan"]["status"] in ("matches", "misses")


def test_leg_deviation_is_causal():
    rng = np.random.default_rng(3)
    price = np.exp(rng.normal(0, 0.1, (300, 4))) * 2.0
    leg = price * 2.8
    fair, leg_fair = pricing.fair_value(price).fair, pricing.fair_value(leg).fair
    later = leg.copy()
    later[200:] *= 5
    later_fair = pricing.fair_value(later).fair
    a = pricing.leg_deviation(leg, fair, leg_fair)
    b = pricing.leg_deviation(later, fair, later_fair)
    assert np.array_equal(a[:200], b[:200], equal_nan=True), "the future must not move the past"
    # Clipped around the market's own value: a 2.8x market is not cut down to 1.5x the blended value.
    assert np.nanmedian(np.exp(a[50:])) == pytest.approx(2.8, rel=0.1)


def test_every_plan_says_how_long_one_lot_takes(desk):
    """Known answer: Test Scrap is worth about 2 Chaos and about 5.6 Chaos in its Divine market, so its
    Chaos-market lot is one scrap and its Divine-market lot is one Divine's worth, bridge / price scraps."""
    plans = desk.flips(min_margin=0.0)
    assert plans
    for plan in plans:
        assert plan["hours_per_lot"] == pytest.approx(plan["lot_units"] / plan["units_per_hour"], rel=0.01)  # both rounded
        assert plan["hours_per_lot"] <= LOT_WINDOW
    scrap = {p["kind"]: p for p in plans if p["id"] == SCRAP}
    assert scrap["chaos"]["lot_units"] == 1
    assert scrap["cross"]["lot_units"] == pytest.approx(desk.bridge / scrap["cross"]["sell"], rel=1e-3)
    # The Divine-only flip buys low and sells high in the same market: the cheaper buy sets the lot.
    assert scrap["divine"]["lot_units"] == pytest.approx(desk.bridge / scrap["divine"]["buy"], rel=1e-3)
    assert scrap["cross"]["slow"] is None, "a lot that fills within a sitting carries no warning"


def test_a_level_too_slow_to_fill_one_lot_is_not_a_plan(desk):
    """Thin the scrap's Divine market until only the most patient level fills one lot within two days.

    Known answer: hours per lot = lot / (share x reach x units per hour). With the market trading
    lot / 2 units an hour at a 10% share, the 50% level takes 2 / (0.1 x 0.5) = 40 hours and the 35% level
    2 / (0.1 x 0.35) = 57 hours, more than the 48 hours the levels were read from. (The lot differs a
    little between levels, because each level sells at its own price; hence the tolerance.)
    """
    i = desk.panel.index(SCRAP)
    leg = desk.legs["b"]
    lot = next(p for p in desk.flips(min_margin=0.0) if p["id"] == SCRAP and p["kind"] == "cross")["lot_units"]
    flow, value = leg.flow[i], leg.value_24h[i]
    try:
        leg.flow[i], leg.value_24h[i] = lot / 2, 30 * desk.bridge
        desk._cache.clear()
        cross = next(p for p in desk.flips(min_margin=0.0) if p["id"] == SCRAP and p["kind"] == "cross")
        assert cross["reach"] == 0.5
        assert cross["hours_per_lot"] == pytest.approx(40, rel=0.1)
        assert cross["slow_market"] == "Divine"
        assert cross["slow"] == (f"Slow: at your share of the trade, one lot fills about every {cross['hours_per_lot']:.0f} hours. "
                                 "Its Divine market traded about 30 Divines in the last day.")
        # Thinner still, and no level fills a lot in two days: the Divine-market plans are gone, the Chaos one stays.
        leg.flow[i] = lot / 4
        desk._cache.clear()
        assert {p["kind"] for p in desk.flips(min_margin=0.0) if p["id"] == SCRAP} == {"chaos"}
    finally:
        leg.flow[i], leg.value_24h[i] = flow, value
        desk._cache.clear()


def test_a_slow_plan_names_its_market_in_plain_words(desk):
    slow = {"hours_per_lot": 12.4, "slow_market": "Chaos", "slow_market_value_24h": 1234.0}
    assert desk._slow(slow) == ("Slow: at your share of the trade, one lot fills about every 12 hours. "
                                "Its Chaos market traded about 1,234 Chaos in the last day.")
    assert desk._slow(slow | {"slow_market": "Divine", "slow_market_value_24h": desk.bridge}) == (
        "Slow: at your share of the trade, one lot fills about every 12 hours. "
        "Its Divine market traded about 1 Divine in the last day.")
    # Under ten, one decimal place and never an exponent: 9.7 Divines, not "1e+01".
    assert "traded about 9.7 Divines in" in desk._slow(slow | {"slow_market": "Divine", "slow_market_value_24h": 9.7 * desk.bridge})
    assert "traded about 0.5 Divines in" in desk._slow(slow | {"slow_market": "Divine", "slow_market_value_24h": 0.5 * desk.bridge})
    assert desk._slow(slow | {"hours_per_lot": 6.0}) is None, "one lot within a sitting is not slow"
    # Just past a sitting says so, not "every 6 hours".
    assert "one lot fills about every 6.4 hours" in desk._slow(slow | {"hours_per_lot": 6.4})


def test_a_plan_cut_to_a_bankroll_times_its_own_lot(desk):
    """With a bankroll the orders are re-cut to whole numbers at slightly different prices; the lot and
    its time follow those prices, not the uncut plan's."""
    plans = desk.flips(min_margin=0.0, bankroll=500.0)
    assert plans
    for plan in plans:
        buy_unit = desk.bridge if plan["kind"] == "divine" else 1.0
        sell_unit = desk.bridge if plan["kind"] != "chaos" else 1.0
        lot = max(1.0, buy_unit / plan["buy"], sell_unit / plan["sell"])
        assert plan["lot_units"] == pytest.approx(lot, rel=1e-3)
        assert plan["hours_per_lot"] == pytest.approx(lot / plan["units_per_hour"], rel=1e-2)
        assert plan["hours_per_lot"] <= LOT_WINDOW
        assert (plan["slow"] is None) == (plan["hours_per_lot"] <= 6)


def test_a_divine_market_far_above_the_chaos_market_is_flagged_not_dropped(desk):
    """Test Scrap's Divine market is 2.8x its Chaos market: flagged only past 3x. Push it to 6x."""
    from vaalstreet.desk import session_plan

    cross = next(p for p in desk.flips(min_margin=0.0) if p["id"] == SCRAP and p["kind"] == "cross")
    assert cross["divine_gap"] == pytest.approx(SCRAP_PREMIUM, rel=0.1) and cross["warning"] is None
    i = desk.panel.index(SCRAP)
    desk.legs["b"].fair[-1, i] = 6 * desk.legs["r"].fair[-1, i]
    desk._cache.clear()
    flagged = next(p for p in desk.flips(min_margin=0.0) if p["id"] == SCRAP and p["kind"] == "cross")
    assert flagged["warning"] == "The Divine market pays 6x the Chaos market here; it fills rarely."
    # Kept on the board; a session plan takes it only if its replay at the stake beat holding Divines.
    losing = flagged | {"lot_cost": 10.0, "replay": {"return": -0.1, "sales": 5}}
    assert session_plan([losing], 1000)["flips"] == []
