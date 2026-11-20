"""Pricing models, the desk's plans, league analytics."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from conftest import CARD, ITEM, NEW, OLD, OTHER, PREMIUM, item_price
from vaalstreet import pricing
from vaalstreet.desk import Desk
from vaalstreet.leagues import LeagueBook
from vaalstreet.panel import CORE, LEGS, Panel, ffill, rolling, rolling_quantile


def noisy_walk(T=600, N=4, noise=0.05, seed=3):
    rng = np.random.default_rng(seed)
    true = 2.0 + np.cumsum(rng.normal(0, 0.01, (T, N)), axis=0)
    return np.exp(true), np.exp(true + rng.normal(0, noise, (T, N)))


def test_fair_value_is_closer_to_the_truth_than_the_prints():
    true, seen = noisy_walk()
    fv = pricing.fair_value(seen)
    err_fair = np.abs(np.log(fv.fair[200:] / true[200:])).mean()
    err_seen = np.abs(np.log(seen[200:] / true[200:])).mean()
    assert err_fair < 0.6 * err_seen
    assert np.sqrt(np.median(fv.r)) == pytest.approx(0.05, rel=0.4), "noise is recovered from the bounce"


def test_fair_value_is_causal():
    _, seen = noisy_walk()
    changed = seen.copy()
    changed[400:] *= 3.0
    a, b = pricing.fair_value(seen), pricing.fair_value(changed)
    assert np.array_equal(a.fair[:400], b.fair[:400]), "the future must not move the past"
    assert np.array_equal(a.z[:400], b.z[:400], equal_nan=True)


def test_fair_value_shrugs_off_a_fat_finger_and_survives_gaps():
    _, seen = noisy_walk(noise=0.02)
    spiked = seen.copy()
    spiked[300, 0] *= 50
    spiked[100:140, 1] = np.nan
    fv = pricing.fair_value(spiked)
    assert fv.fair[300, 0] == fv.fair[299, 0], "a lone far-out print is set aside"
    assert fv.z[300, 0] > 5
    assert np.isfinite(fv.fair[100:140, 1]).all() and np.isnan(fv.z[100:140, 1]).all()
    assert (fv.sd[139, 1] > fv.sd[99, 1]), "uncertainty grows while an item does not print"


def test_fair_value_accepts_a_repricing_once_a_second_print_confirms_it():
    _, seen = noisy_walk(noise=0.02)
    crashed = seen.copy()
    crashed[300:, 0] *= 0.03  # the item really did lose 97% of its value
    fv = pricing.fair_value(crashed)
    assert fv.fair[300, 0] == fv.fair[299, 0], "not believed on the first print"
    assert fv.fair[301, 0] == pytest.approx(crashed[301, 0]), "believed on the second"
    assert abs(math.log(fv.fair[330, 0] / crashed[330, 0])) < 0.1


def test_nice_ratio_gives_small_whole_number_orders():
    assert pricing.nice_ratio(220.97) == (221, 1)
    assert pricing.nice_ratio(3.3) == (33, 10)
    assert pricing.nice_ratio(1.5) == (3, 2)
    currency, units = pricing.nice_ratio(0.3157)
    assert abs(currency / units - 0.3157) / 0.3157 < 0.004 and currency < 20
    currency, units = pricing.nice_ratio(0.00106)
    assert currency == 1 and abs(1 / units - 0.00106) / 0.00106 < 0.004
    with pytest.raises(ValueError):
        pricing.nice_ratio(0)


def test_rolling_helpers_match_numpy():
    rng = np.random.default_rng(1)
    a = rng.normal(size=(120, 3))
    a[rng.random(a.shape) < 0.3] = np.nan
    fast = rolling_quantile(a, 24, 0.3, min_count=3)
    slow = rolling(a, 24, lambda v, axis: np.nanquantile(v, 0.3, axis=axis), min_count=3)
    assert np.allclose(fast, slow, equal_nan=True)
    filled = ffill(np.array([[np.nan], [1.0], [np.nan], [np.nan], [2.0]]), limit=1)
    assert np.array_equal(filled[:, 0], [np.nan, 1.0, 1.0, np.nan, 2.0], equal_nan=True)


def test_panel_is_a_dense_hourly_grid(con):
    panel = Panel.load(con, NEW, CORE + LEGS)
    assert panel.shape == (8 * 24, 4)
    assert np.all(np.diff(panel.hours) == 3600)
    i = panel.index("test orb")
    assert panel["r_price"][5, i] == pytest.approx(item_price(NEW, 5), rel=1e-3)
    assert np.isnan(panel["b_price"][:, panel.index("Divine Orb")]).all()
    with pytest.raises(KeyError):
        panel.index("Nonexistent")


@pytest.fixture()
def desk(con, archive_root):
    return Desk.build(con, NEW, root=archive_root)


def test_desk_market_rows_are_json_safe(desk):
    rows = desk.market()
    json.dumps(rows, allow_nan=False)
    orb = next(r for r in rows if r["id"] == ITEM)
    assert orb["premium"] == pytest.approx(math.log(PREMIUM), abs=0.01)
    assert orb["chaos"] < orb["price"] < orb["divine"]
    assert orb["age"] == 0 and orb["active"] == 1
    assert desk.summary()["bridge"] == pytest.approx(desk.bridge)
    series = desk.series("Test Orb", 48)
    json.dumps(series, allow_nan=False)
    assert len(series["t"]) == 48


def test_cross_market_flip_captures_the_divine_premium(desk):
    plans = {(p["id"], p["kind"]): p for p in desk.flips(share=0.1, min_margin=0.01)}
    cross = plans[(ITEM, "cross")]
    assert cross["margin"] >= PREMIUM - 1 - 0.01, "at least the structural premium"
    assert cross["buy"] < cross["sell"]
    assert cross["buy_order"]["currency"] == "Chaos" and cross["sell_order"]["currency"] == "Divine"
    # 10% of the smaller market's flow, in the share of hours the levels are reached.
    assert cross["units_per_hour"] == pytest.approx(0.1 * cross["reach"] * 1000, rel=1e-6)
    chaos = plans[(ITEM, "chaos")]
    assert 0.02 < chaos["margin"] < cross["margin"], "the same-market swing is smaller than the cross-market gap"
    assert all(p["margin"] >= 0.01 for p in plans.values())
    json.dumps(list(plans.values()), allow_nan=False)
    # Each plan carries its own week: the cross-market rule sold and made money, on a fixed stake.
    assert cross["replay"]["sales"] > 5 and cross["replay"]["return"] > 0.05


def test_set_conversion_compares_cost_with_reward(desk):
    rows = desk.conversions()
    card = next(r for r in rows if r["id"] == CARD)
    assert (card["need"], card["gives"], card["reward"]) == (5, 1, "Test Orb")
    assert card["cost"] == pytest.approx(7.5, rel=0.01)
    assert card["margin"] == pytest.approx(desk.fair(ITEM) / 7.5 - 1, rel=0.01)
    assert card["margin_now"] < card["margin"], "selling the reward into the cheaper market earns less"


def test_side_market_out_of_line_is_reported(desk):
    swap = desk.triangles(min_value=10)[0]
    # 1 orb fetches 3 shards worth 2 Chaos each, while an orb is worth ~10.6: giving shards for orbs is the edge.
    assert (swap["give"], swap["get"]) == (OTHER, ITEM)
    assert swap["edge"] == pytest.approx(desk.fair(ITEM) / 6 - 1, rel=0.02)


def test_league_book_lines_leagues_up_by_day(con):
    book = LeagueBook(con)
    leagues = {r["league"]: r for r in book.leagues()}
    assert leagues[OLD]["main"] and not leagues[OLD]["truncated"]
    assert book.precedents(NEW) == [OLD]
    assert book.day_of(NEW) == 7
    curve = book.curve("Test Orb")
    assert [p["day"] for p in curve[NEW]] == list(range(8))
    # In the old league the orb rose 10% of its day-0 price per day.
    assert curve[OLD][10]["price"] / curve[OLD][0]["price"] == pytest.approx(2.0, rel=0.03)


def test_playbook_reports_what_past_leagues_did_next(con):
    book = LeagueBook(con)
    pb = book.playbook(NEW, horizon=7, unit="chaos", min_value=0)
    orb = next(r for r in pb["items"] if r["id"] == ITEM)
    assert pb["day"] == 7 and pb["precedents"] == [OLD]
    # Entry averages days 6-7 (trend 1.65), exit days 13-15 (trend 2.4).
    assert orb["median"] == pytest.approx(2.4 / 1.65 - 1, abs=0.03)
    assert orb["n"] == 1 and orb["up_share"] == 1 and orb["traded_now"]
    in_divine = book.playbook(NEW, horizon=7, unit="divine", min_value=0)
    orb_d = next(r for r in in_divine["items"] if r["id"] == ITEM)
    assert orb_d["median"] < orb["median"], "Divine rose too, so the move is smaller in Divine"
    standing = book.standing(NEW, unit="chaos", min_value=0)
    orb_s = next(r for r in standing["items"] if r["id"] == ITEM)
    assert orb_s["vs_typical"] == pytest.approx(1 / 1.65 - 1, abs=0.03)
