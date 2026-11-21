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
from vaalstreet.desk import SWING_WINDOW, Desk
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
