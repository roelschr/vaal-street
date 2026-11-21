"""Pricing models, the desk's plans, league analytics."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from conftest import CARD, EMBER, EMBER_PREMIUM, ITEM, NEW, OLD, OTHER, PREMIUM, item_price
from vaalstreet import pricing
from vaalstreet.desk import Desk
from vaalstreet.leagues import LeagueBook
from vaalstreet.panel import CORE, LEGS, Panel, ffill, rolling, rolling_quantile, rolling_weighted_quantile, weighted_quantile


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
    # With traded value, as the desk runs it: typical units and the reprice check stay causal too.
    units = np.full(seen.shape, 100.0)
    later = units.copy()
    later[400:] *= 9
    a, b = pricing.fair_value(seen, seen * units), pricing.fair_value(changed, changed * later)
    assert np.array_equal(a.fair[:400], b.fair[:400]), "future volume must not move the past"


def test_fair_value_is_quiet_about_items_that_never_print():
    import warnings

    _, seen = noisy_walk()
    seen = seen.copy()
    seen[:, 0] = np.nan  # an item with no prints at all, as many have on a young league
    with warnings.catch_warnings():
        warnings.simplefilter("error", category=RuntimeWarning)
        fv = pricing.fair_value(seen, seen * 100.0)
    assert np.isnan(fv.fair[:, 0]).all() and np.isfinite(fv.fair[-1, 1:]).all()


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


def test_two_thin_joke_prints_in_a_row_do_not_reprice_an_item():
    # As seen live on Tainted Armourer's Scrap's Divine market: ~100 units an hour at about 6.5,
    # then 2 and 12 units at half a Divine (~107), then 260 units back at 8. Known answer: the
    # value never leaves the old level, while a real crash (full volume) is still believed.
    _, seen = noisy_walk(noise=0.02, N=2)
    units = np.full(seen.shape, 100.0)
    seen[300:302] *= 16
    units[300:302] = [[2, 2], [12, 12]]
    seen[302:, 1] *= 16  # column 1: the move is real and keeps trading at full volume
    fv = pricing.fair_value(seen, seen * units)
    assert abs(math.log(fv.fair[301, 0] / fv.fair[299, 0])) < 0.1, "thin prints confirm nothing"
    assert abs(math.log(fv.fair[310, 0] / seen[310, 0])) < 0.1
    assert fv.fair[302, 1] == pytest.approx(seen[302, 1]), "a full-volume print confirms the move"


def test_scorecard_measures_fair_value_against_the_last_print():
    true, seen = noisy_walk(T=800, noise=0.05)
    value = np.ones_like(seen)
    target = pricing.forward_vwap(seen, value, 3)
    assert target[10, 0] == pytest.approx(np.log(seen[11:14, 0]).mean()), "the next three hours, not this one"
    assert np.isnan(target[-1]).all()
    rows = pricing.scorecard(seen, value, horizon=6)
    assert len(rows) == 1 and rows[0]["count"] == 4
    assert rows[0]["fair_mean"] < rows[0]["last_mean"] and rows[0]["improvement"] > 0.2


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


def test_weighted_quantile_ignores_volume_free_outliers():
    values = np.array([1.0, 2.0, 3.0, 100.0, np.nan])
    weights = np.array([10.0, 10.0, 10.0, 0.01, 5.0])
    assert weighted_quantile(values, weights, 0.5) == 2.0
    assert weighted_quantile(values, weights, 0.99) == 3.0, "a joke trade carries no weight"
    assert weighted_quantile(values, np.ones(5), 0.99) == 100.0
    assert np.isnan(weighted_quantile(values, weights, 0.5, min_count=5))
    rng = np.random.default_rng(2)
    a = rng.normal(size=(60, 2))
    rolled = rolling_weighted_quantile(a, np.ones_like(a), 12, 0.5, min_count=12)
    assert np.isnan(rolled[:11]).all() and rolled[30, 0] == np.sort(a[19:31, 0])[5]


def test_panel_is_a_dense_hourly_grid(con):
    panel = Panel.load(con, NEW, CORE + LEGS)
    assert panel.shape == (8 * 24, 7)  # five items plus the Test Lure and the Test Blade card
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
    # 10% of the smaller market's flow, times the share of volume that trades beyond the levels.
    assert cross["units_per_hour"] == pytest.approx(0.1 * cross["reach"] * 1000, rel=1e-6)
    chaos = plans[(ITEM, "chaos")]
    assert 0.02 < chaos["margin"] < cross["margin"], "the same-market swing is smaller than the cross-market gap"
    assert all(p["margin"] >= 0.01 for p in plans.values())
    json.dumps(list(plans.values()), allow_nan=False)
    # Each plan carries its own week: the cross-market rule sold and made money, on a fixed stake.
    assert cross["replay"]["sales"] > 5 and cross["replay"]["return"] > 0.05


def test_small_bankroll_gets_whole_lot_orders_it_can_post(desk):
    assert pricing.fit_ratio(3.07, 40) == (40, 13)
    assert pricing.fit_ratio(3.07, 5) == (3, 1), "the closest order that still fits five Chaos"
    assert pricing.fit_ratio(250, 40) is None, "one unit costs more than the purse"
    assert pricing.fit_ratio(0.024, 40) == (3, 125)
    assert pricing.fit_ratio(0.024, 1) == (1, 42)
    assert pricing.fit_ratio(10.4, 400, max_items=2) == (21, 2), "cannot sell more units than it can hold"

    everything = desk.flips(share=0.1, min_margin=0.01)
    assert {p["kind"] for p in everything} >= {"chaos", "cross"}
    small = desk.flips(share=0.1, min_margin=0.01, bankroll=30)
    assert small and {p["kind"] for p in small} == {"chaos"}, "a Divine costs over 100: no Divine orders"
    for plan in small:
        buy, sell = plan["buy_order"], plan["sell_order"]
        assert buy["currency_units"] <= 30 and plan["lot_cost"] <= 30
        assert sell["item_units"] * plan["buy"] <= 30 + 1e-9, "can afford the units one sell order needs"
        # The margin is what the whole-number orders really state, not the ideal levels.
        assert plan["margin"] == pytest.approx(
            (sell["currency_units"] / sell["item_units"]) / (buy["currency_units"] / buy["item_units"]) - 1, abs=1e-3)
        assert plan["profit_per_hour"] <= plan["margin"] * 15 + 1e-3, "30 Chaos cannot turn over faster than this"
    assert desk.flips(min_margin=0.01, bankroll=1) == []


def test_recipes_chain_through_tiers_and_loops_use_side_markets(desk, archive_root):
    from vaalstreet import items

    meta = items.build({
        "Metadata/Items/Currency/CurrencyEssenceWoe5": {"name": "Screaming Essence of Woe", "tags": ["essence"], "properties": {"stack_size": 9}},
        "Metadata/Items/Currency/CurrencyEssenceWoe6": {"name": "Shrieking Essence of Woe", "tags": ["essence"], "properties": {"stack_size": 9}},
        "Metadata/Items/Currency/CurrencyEssenceWoe7": {"name": "Deafening Essence of Woe", "tags": ["essence"], "properties": {"stack_size": 9}},
        "Metadata/Items/Currency/Mushrune6": {"name": "Azure Oil", "tags": ["mushrune"], "properties": {"stack_size": 10}},
        "Metadata/Items/Currency/Mushrune6b": {"name": "Indigo Oil", "tags": ["mushrune"], "properties": {"stack_size": 10}},
        "Metadata/Items/Currency/CurrencyAfflictionShard": {"name": "Simulacrum Splinter", "tags": [], "properties": {"stack_size": 300}},
        "Metadata/Items/Currency/CurrencyAfflictionFragment": {"name": "Simulacrum", "tags": [], "properties": {"stack_size": 1}},
    })
    recipe = lambda name: next(v.get("set") for v in meta.values() if v["name"] == name)
    assert recipe("Screaming Essence of Woe") == {"need": 3, "gives": 1, "reward": "Shrieking Essence of Woe", "how": "vendor"}
    assert recipe("Deafening Essence of Woe") is None, "top tier"
    assert recipe("Azure Oil")["reward"] == "Indigo Oil"
    assert recipe("Simulacrum Splinter") == {"need": 300, "gives": 1, "reward": "Simulacrum", "how": "stack"}

    # In the fixture five cards make one orb. Pretend three orbs then make one shard: the chain is 15 cards.
    desk.meta = dict(desk.meta) | {ITEM: desk.meta[ITEM] | {"set": {"need": 3, "gives": 1, "reward": "Test Shard"}}}
    chain = {(r["name"], r["steps"]): r for r in desk.conversions()}
    two = chain[("Test Card", 2)]
    assert (two["need"], two["gives"], two["reward"]) == (15, 1, "Test Shard")
    assert two["cost"] == pytest.approx(15 * 1.5, rel=0.01) and two["proceeds"] == pytest.approx(2.0, rel=0.01)

    # Side market: 1 orb fetches 3 shards. Buying shards (2c), swapping 3 for an orb and selling it is the loop.
    loop = desk.loops(chaos_only=True)[0]
    assert [x["id"] for x in loop["path"]] == [OTHER, ITEM]
    orb_chaos = desk.legs["r"].fair[-1, desk.panel.index(ITEM)]
    assert loop["edge"] == pytest.approx(orb_chaos / 6 - 1, rel=0.02)
    assert loop["first_swap"] == {"give": 3, "get": 1} and loop["lot_cost"] == pytest.approx(6, rel=0.02)
    # The fixture leaves 500 orbs resting in that market: they are there to be taken.
    assert loop["resting_value"] == pytest.approx(500 * desk.fair(ITEM), rel=0.01)
    richer = next(row for row in desk.loops(chaos_only=False) if row["path"] == loop["path"])
    assert richer["edge"] > loop["edge"], "with Divines in hand the orb sells in the dearer market"


def test_set_conversion_compares_cost_with_reward(desk):
    rows = desk.conversions()
    card = next(r for r in rows if r["id"] == CARD and r["steps"] == 1)
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
    outlook = book.divine_outlook(NEW, horizons=(7,))
    # Divine gains 0.25 Chaos an hour: ~141.9 averaged over days 6-7, ~186.8 over days 13-15.
    assert outlook[0]["horizon"] == 7 and outlook[0]["median"] == pytest.approx(186.8 / 141.9 - 1, abs=0.01)
    standing = book.standing(NEW, unit="chaos", min_value=0)
    orb_s = next(r for r in standing["items"] if r["id"] == ITEM)
    assert orb_s["vs_typical"] == pytest.approx(1 / 1.65 - 1, abs=0.03)


# ------------------------------------------------------------ fair value range and the two-market model

def test_fair_value_range_is_wider_for_noisy_items_and_narrows_with_data():
    _, calm = noisy_walk(noise=0.01)
    _, noisy = noisy_walk(noise=0.10)
    a, b = pricing.fair_value(calm), pricing.fair_value(noisy)
    assert (b.sd[200:] > a.sd[200:]).mean() > 0.95
    assert np.all(a.low <= a.fair) and np.all(a.fair <= a.high)
    for fv in (a, b):
        assert np.all(fv.sd[10] < 0.75 * fv.sd[0]), "the band narrows as prints accumulate"


def test_fair_value_range_coverage_on_a_known_walk():
    # noisy_walk: true log value moves 0.01 per hour, prints add 0.05 of noise. A one-sd band should
    # hold the true value, and one `spread` around fair value the next print, about 68% of the time.
    true, seen = noisy_walk(T=800)
    fv = pricing.fair_value(seen)
    f = np.log(fv.fair)
    assert 0.58 < np.mean(np.abs(f[100:] - np.log(true[100:])) <= fv.sd[100:]) < 0.78
    assert 0.58 < np.mean(np.abs(np.log(seen[101:]) - f[100:-1]) <= fv.spread[100:-1]) < 0.78


def test_fair_value_range_is_causal():
    _, seen = noisy_walk()
    full, cut = pricing.fair_value(seen), pricing.fair_value(seen[:400])
    for name in ("fair", "sd", "spread", "low", "high", "z"):
        assert np.array_equal(getattr(full, name)[:400], getattr(cut, name), equal_nan=True), name


def two_markets(T=1200, N=6, noise=0.05, keep=0.25, premium=0.08, seed=5):
    """One true log value per item; the Divine market pays `premium` (log) more and prints in `keep` of hours."""
    rng = np.random.default_rng(seed)
    v = 2.0 + np.cumsum(rng.normal(0, 0.01, (T, N)), axis=0)
    r = np.exp(v + rng.normal(0, noise, (T, N)))
    b = np.exp(v + premium + rng.normal(0, noise, (T, N)))
    b[rng.random((T, N)) > keep] = np.nan
    return v, r, b


def test_two_market_model_is_causal():
    _, r, b = two_markets()
    full, cut = pricing.two_market(r, b), pricing.two_market(r[:500], b[:500])
    for name in ("value", "premium", "value_sd", "premium_sd", "divine"):
        assert np.array_equal(getattr(full, name)[:500], getattr(cut, name), equal_nan=True), name


def test_two_market_model_recovers_the_archive_premium(con):
    # The synthetic archive's Divine market pays exactly PREMIUM over the Chaos market every hour.
    for league in (OLD, NEW):
        panel = Panel.load(con, league, CORE + LEGS)
        tm = pricing.two_market(panel["r_price"], panel["b_price"], panel["r_price"] * panel["r_volume"],
                                panel["b_price"] * panel["b_volume"])
        i = panel.index(ITEM)
        assert np.max(np.abs(tm.premium[24:, i] - math.log(PREMIUM))) < 0.005
        assert np.isnan(tm.premium[:, panel.index(CARD)]).all(), "one market, so no premium"
        rows = pricing.market_scorecard(panel["price"], panel["value"], panel["r_price"],
                                        panel["r_price"] * panel["r_volume"], panel["b_price"],
                                        panel["b_price"] * panel["b_volume"])
        assert {row["market"] for row in rows} == {"Chaos", "Divine"}


def test_two_market_model_pools_a_thin_divine_market_with_the_chaos_market():
    v, r, b = two_markets()
    tm = pricing.two_market(r, b)
    assert np.median(tm.premium[300:]) == pytest.approx(0.08, abs=0.01)
    pooled = np.abs(np.log(tm.divine[300:]) - v[300:] - 0.08).mean()
    alone = np.nanmean(np.abs(np.log(pricing.fair_value(b).fair[300:]) - v[300:] - 0.08))
    assert pooled < 0.8 * alone, "the Divine market borrows the Chaos market's prints"
    one = np.ones_like(r)
    divine = [row for row in pricing.market_scorecard(np.where(np.isfinite(r), r, b), one, r, one, b, one)
              if row["market"] == "Divine"]
    assert divine and divine[0]["two_market_mean"] < divine[0]["own_market_mean"]


def test_two_market_model_accepts_a_repricing_and_keeps_the_premium():
    _, r, b = two_markets()
    r[600:, 0] *= 0.03
    b[600:, 0] *= 0.03
    tm = pricing.two_market(r, b)
    assert tm.value[600, 0] == tm.value[599, 0], "not believed on the first print"
    assert abs(math.log(tm.value[601, 0] / r[601, 0])) < 0.1, "believed on the second"
    assert tm.premium[700, 0] == pytest.approx(0.08, abs=0.02)


def test_desk_carries_the_fair_value_range(desk):
    orb = next(row for row in desk.market() if row["id"] == ITEM)
    assert orb["fair_low"] < orb["price"] < orb["fair_high"]
    low, fair, high = desk.fair_range(ITEM)
    assert fair == desk.fair(ITEM) and low < fair < high
    s = desk.series(ITEM)
    assert len(s["fair_low"]) == len(s["fair"]) == len(s["fair_high"])
    json.dumps(s)


def test_round_trips_through_divine_only_when_they_beat_the_cross_flip(desk):
    from vaalstreet.ingest import DIVINE

    rows = [row for row in desk.loops(min_value=10) if "divine" in row["via"]]
    # Sell orbs for Divines where they are 10% dear and buy embers where they are 5% cheap. The reverse
    # path, and every path that only sells in the dearer market, is the cross flip and is left out.
    # Shards -> orb -> Divine -> embers pays too, but only the best Divine path per item bought with the
    # Divines is kept, and selling orbs straight for Divines is far deeper than the shard market.
    by_path = {tuple(x["id"] for x in row["path"]): row for row in rows}
    assert set(by_path) == {(ITEM, DIVINE, EMBER)}
    pair = by_path[(ITEM, DIVINE, EMBER)]
    assert pair["edge"] == pytest.approx(PREMIUM / EMBER_PREMIUM - 1, rel=0.05)
    assert pair["swaps"] == 2 and pair["via"] == ["divine", "divine"]
    # The shard loop itself still shows.
    assert any([x["id"] for x in r["path"]] == [OTHER, ITEM] for r in desk.loops(min_value=10))
    assert not [r for r in desk.loops(min_value=10, swaps=2) if len(r["path"]) > 3]
    rows = [pair]
    # Both ends are priced in their Chaos markets, since the Divine markets are the swaps.
    i, j = desk.panel.index(ITEM), desk.panel.index(EMBER)
    assert rows[0]["buy"] == pytest.approx(desk.legs["r"].fair[-1, i], rel=1e-4)
    assert rows[0]["sell"] == pytest.approx(desk.legs["r"].fair[-1, j], rel=1e-4)
    assert not [row for row in desk.loops(min_value=10, chaos_only=True) if "divine" in row["via"]]


def test_longer_round_trips_and_vendor_prices_you_enter(desk):
    from vaalstreet.ingest import CHAOS

    # Made-up markets: one card fetches one shard, and three shards one orb (the fixture's own rate).
    desk.cross = [
        {"item": CARD, "quote": OTHER, "volume": 1000, "rate": 1.0, "hours": 24, "item_resting": 50, "quote_resting": 70},
        {"item": ITEM, "quote": OTHER, "volume": 100, "rate": 3.0, "hours": 20, "item_resting": 500, "quote_resting": 900},
    ]
    paths = lambda rows: {tuple(x["id"] for x in row["path"]): row for row in rows}
    two = paths(desk.loops(min_edge=-1, min_value=10, chaos_only=True, swaps=2))
    assert (CARD, OTHER, ITEM) in two and all(len(p) <= 3 for p in two)
    assert len(paths(desk.loops(min_edge=-1, min_value=10, chaos_only=True, swaps=1))) < len(two)

    # A made-up vendor sells a card for 1 Chaos. Chaos -> card -> shard -> orb is three swaps:
    # 1 Chaos buys 1 card = 1 shard = 1/3 orb, so the edge is the orb's Chaos price / 3 - 1.
    vendor = [{"kind": "vendor", "inputs": [(CHAOS, 1)], "output": CARD, "gives": 1}]
    orb = desk.legs["r"].fair[-1, desk.panel.index(ITEM)]
    assert (CHAOS, CARD, OTHER, ITEM) not in paths(desk.loops(min_value=10, chaos_only=True, swaps=2, fixed=vendor))
    three = paths(desk.loops(min_value=10, chaos_only=True, swaps=3, fixed=vendor))[(CHAOS, CARD, OTHER, ITEM)]
    assert three["edge"] == pytest.approx(orb / 3 - 1, rel=1e-3)
    assert three["via"] == ["vendor", "swap", "swap"] and three["vendor_keys"] == [f"vendor:1*{CHAOS}>1*{CARD}"]
    # The vendor has no limit, so the thinnest exchange swap sets the depth: 100 orbs a day.
    assert three["value_24h"] == pytest.approx(100 * desk.fair(ITEM), rel=1e-4)
    assert three["resting_value"] == pytest.approx(min(70 * desk.fair(OTHER), 500 * desk.fair(ITEM)), rel=1e-4)
    assert three["recipe_key"] == f"loop:{CHAOS}>{CARD}>{OTHER}>{ITEM}"

    # Buying an orb from a vendor for 5 Chaos and selling it: as deep as the orb's own markets.
    vendor = [{"kind": "vendor", "inputs": [(CHAOS, 5)], "output": ITEM, "gives": 1}]
    only = paths(desk.loops(min_value=10, chaos_only=True, fixed=vendor))[(CHAOS, ITEM)]
    assert only["edge"] == pytest.approx(orb / 5 - 1, rel=1e-3)
    assert only["value_24h"] > 0 and only["resting_value"] is None
    json.dumps(only, allow_nan=False)


def test_round_trips_skip_swap_markets_far_out_of_line(desk):
    # Live data had 1 Vaal Orb fetching 5 Scours every hour, items worth about the same. A side
    # market whose rate is more than 3x off the two items' own prices is joke trades: here 1 card
    # (1.5 Chaos) "fetches" 5 shards (10 Chaos), while 3 shards per orb (implied ~5.3) stays.
    desk.cross = [
        {"item": CARD, "quote": OTHER, "volume": 1000, "rate": 5.0, "hours": 24, "item_resting": 50, "quote_resting": 70},
        {"item": ITEM, "quote": OTHER, "volume": 100, "rate": 3.0, "hours": 20, "item_resting": 500, "quote_resting": 900},
    ]
    paths = {tuple(x["id"] for x in row["path"]) for row in desk.loops(min_edge=-1, min_value=10, chaos_only=True)}
    assert (OTHER, ITEM) in paths
    assert not [p for p in paths if CARD in p]


def test_a_round_trip_is_as_deep_as_the_market_it_buys_in(desk):
    """Known answer: shards -> orb is limited by its swap market, 100 orbs an hour (about 25,000 Chaos a day).
    When the shard's Chaos market trades one shard an hour (2 Chaos), the trip is 48 Chaos a day deep."""
    trip = lambda: next(row for row in desk.loops(min_value=10, chaos_only=True)  # noqa: E731
                        if [x["id"] for x in row["path"]] == [OTHER, ITEM])
    swap_value = 100 * 24 * desk.fair(ITEM)
    assert trip()["value_24h"] == pytest.approx(swap_value, rel=0.05)
    desk.legs["r"].flow[desk.panel.index(OTHER)] = 1.0
    desk._cache.clear()
    thin = trip()
    assert thin["value_24h"] == pytest.approx(24 * 2.0, rel=0.02)
    assert thin["profit_per_day"] == pytest.approx(0.1 * 48 * thin["edge"], rel=0.02)


def test_round_trips_skip_a_divine_stop_far_out_of_line(desk):
    from vaalstreet.ingest import DIVINE

    path = (ITEM, DIVINE, EMBER)
    trips = lambda: {tuple(x["id"] for x in row["path"]) for row in desk.loops(min_value=10)}
    assert path in trips()
    # The orb's Divine market now "pays" 4x its Chaos market, as thin joke hours do: no Divine stop.
    desk.legs["b"].fair[-1, desk.panel.index(ITEM)] *= 4
    desk._cache.clear()  # loops() remembers its answer for the desk's hour
    assert not [p for p in trips() if p[0] == ITEM and DIVINE in p]


def test_your_recipes_are_priced_like_the_built_in_ones(desk):
    from vaalstreet.ingest import CHAOS

    # Made up: 2 cards and 1 shard make 1 orb. Cost 2 * 1.5 + 2 = 5 Chaos for an orb worth about 10.6.
    mine = [{"id": 7, "kind": "recipe", "inputs": [(CARD, 2), (OTHER, 1)], "output": ITEM, "gives": 1, "note": "test"},
            {"id": 8, "kind": "recipe", "inputs": [(CHAOS, 3)], "output": OTHER, "gives": 2, "note": ""},
            {"id": 9, "kind": "vendor", "inputs": [(CHAOS, 5)], "output": ITEM, "gives": 1, "note": ""}]
    rows = desk.conversions(fixed=mine)
    built_in = next(r for r in rows if r["how"] != "yours")
    assert built_in["recipe_key"] == f"set:{CARD}>{ITEM}", "keys of checks already in your ledger stay the same"
    yours = [r for r in rows if r["how"] == "yours"]
    assert len(yours) == 2, "vendor prices are round-trip swaps, not conversions"
    row = next(r for r in yours if r["fixed_id"] == 7)
    assert (row["id"], row["need"], row["reward_id"]) == (CARD, 2, ITEM)
    assert [x["units"] for x in row["inputs"]] == [2, 1]
    assert row["cost"] == pytest.approx(2 * desk.fair(CARD) + desk.fair(OTHER), rel=1e-4)
    assert row["margin"] == pytest.approx(desk.fair(ITEM) / row["cost"] - 1, rel=1e-3)
    assert row["margin_now"] <= row["margin"]
    assert row["recipe_key"] == f"recipe:2*{CARD}+1*{OTHER}>1*{ITEM}"
    chaos = next(r for r in yours if r["fixed_id"] == 8)  # 3 Chaos for 2 shards worth 2 each
    assert chaos["margin"] == pytest.approx(2 * desk.fair(OTHER) / 3 - 1, rel=1e-3)
    json.dumps(rows, allow_nan=False)


def test_answers_are_remembered_per_desk_and_keyed_by_every_argument(desk):
    from vaalstreet.desk import Costs

    first = desk.loops(min_value=10)
    size = len(desk._cache)
    desk.cross = []  # a fresh walk would now find no side-market round trips
    again = desk.loops(min_value=10)
    assert again == first and len(desk._cache) == size, "the second call is the remembered answer"
    again[0]["check"] = "confirmed"
    assert "check" not in desk.loops(min_value=10)[0], "a caller's added fields do not leak into the cache"
    # Positional and keyword forms of the same question share one entry.
    assert desk.loops(0.03, 10) == first and len(desk._cache) == size

    desk.loops(min_value=10, swaps=2)
    assert len(desk._cache) == size + 1, "a different argument is a different question"
    priced = desk.loops(min_value=10, costs=Costs(assumed=2.0))
    assert len(desk._cache) == size + 2 and priced[0]["gold"] is not None and first[0]["gold"] is None
    assert desk.loops(min_value=10, costs=Costs(assumed=3.0))[0]["gold"] != priced[0]["gold"]
    mine = [{"id": 1, "kind": "recipe", "inputs": [(CARD, 2)], "output": ITEM, "gives": 1}]
    assert len(desk.conversions(fixed=mine)) == len(desk.conversions()) + 1
