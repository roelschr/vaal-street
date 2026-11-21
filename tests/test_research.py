"""The measurement harness, proved on planted signals (must pass) and pure noise (must not).

Every panel here is synthetic numpy, built in a few milliseconds, with a known answer. Leagues
are 15 days long and back to back, so each has a known start, is long enough to count, and every
league after the first has training leagues that ended before it began.
"""

from __future__ import annotations

import numpy as np
import pytest

from vaalstreet import research
from vaalstreet.panel import LEGS, Panel
from vaalstreet.research import FAIL, PASS, THIN, Season

DAYS = 15
T = DAYS * 24
N = 12
SEEDS = (1, 2, 3)


def make_panel(league: int | str, r_price, b_price=None, *, volume=None, bid=None, stock=None, names=None) -> Panel:
    """A league's panel from Chaos-market prices (and optionally Divine-market prices in Chaos)."""
    index = league if isinstance(league, int) else 0
    hours = (1_700_000_000 // 3600 + index * (T + 24)) * 3600 + np.arange(len(r_price)) * 3600
    n = r_price.shape[1]
    volume = np.where(np.isfinite(r_price), 100.0, np.nan) if volume is None else volume
    b_price = np.full_like(r_price, np.nan) if b_price is None else b_price
    data = {c: np.full_like(r_price, np.nan) for c in LEGS}
    data.update({
        "price": r_price, "volume": volume, "value": r_price * volume,
        "r_price": r_price, "r_volume": volume,
        "b_price": b_price, "b_volume": np.where(np.isfinite(b_price), 100.0, np.nan),
        "r_stock_lo": np.full_like(r_price, 1000.0) if stock is None else stock,
        "r_bid_lo": np.full_like(r_price, np.nan) if bid is None else bid,
    })
    items = [f"item{j}" for j in range(n)]
    return Panel(f"league{index}" if isinstance(league, int) else league, hours, items,
                 names or items, ["Currency"] * n, np.full(len(hours), 200.0), data)


def walk(rng, sd=0.01, n=N, start=100.0):
    return start * np.exp(np.cumsum(rng.normal(0, sd, (T, n)), axis=0))


# ---- the rule itself ----------------------------------------------------------------------------
def test_splits_never_train_on_an_overlapping_league():
    rng = np.random.default_rng(0)
    a, b = make_panel(0, walk(rng)), make_panel(1, walk(rng))
    overlapping = Panel("overlap", b.hours - 24 * 3600 * 5, a.items, a.names, a.categories, a.bridge, a.data)
    pairs = {test.name: [s.name for s in train] for test, train in
             research.splits([Season(a), Season(b), Season(overlapping)])}
    # "overlap" starts five days before league1, which is also before league0 ended: it overlaps
    # both, so it trains neither and neither trains it.
    assert pairs == {"league1": ["league0"]}


def test_two_test_leagues_are_not_enough():
    scored = [{"league": f"l{k}", "counts": True, "signal": np.zeros((T, 2)), "fair": np.ones((T, 2)),
               "last": np.ones((T, 2))} for k in range(research.MIN_TEST_LEAGUES - 1)]
    assert research.forecast_verdict("x", scored, 1).verdict == THIN


def test_a_league_that_does_not_count_is_shown_but_not_counted():
    scored = [{"league": f"l{k}", "counts": k > 0, "signal": np.zeros((T, 2)), "fair": np.ones((T, 2)),
               "last": np.ones((T, 2))} for k in range(research.MIN_TEST_LEAGUES)]
    result = research.forecast_verdict("x", scored, 1)
    assert result.verdict == THIN and result.leagues[0]["counts toward verdict"] == "no"


def test_bootstrap_interval_is_bonferroni_corrected_and_seeded():
    rng = np.random.default_rng(5)
    groups = [(rng.normal(1, 1, 30), np.ones(30))]
    wide = research.bootstrap_interval(groups, family=7, resamples=4000)
    narrow = research.bootstrap_interval(groups, family=1, resamples=4000)
    assert wide[0] < narrow[0] < narrow[1] < wide[1]
    assert wide == research.bootstrap_interval(groups, family=7, resamples=4000)


# ---- two-market ---------------------------------------------------------------------------------
def two_market_leagues(seed: int, planted: bool) -> list[Season]:
    """Planted: one true value; the Chaos market prints it every hour with 1% noise, the Divine market
    prints it 5% dearer in a fifth of the hours with 15% noise. The Divine market alone is a poor guide
    to its own next hours; the value plus a steady premium is a good one. Null: the Divine market is
    its own random walk, unrelated to the Chaos market, so tying the two together can only hurt."""
    rng = np.random.default_rng(seed)
    out = []
    for k in range(4):
        true = walk(rng, 0.02)
        r = true * np.exp(rng.normal(0, 0.01, (T, N)))
        base = true * 1.05 if planted else walk(rng, 0.02)
        b = base * np.exp(rng.normal(0, 0.15, (T, N)))
        b[rng.random((T, N)) > 0.2] = np.nan
        out.append(Season(make_panel(k, r, b)))
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_two_market_planted_passes(seed):
    result = research.two_market(two_market_leagues(seed, True))
    assert result.verdict == PASS, result.lines()
    assert result.settings_tried == 3 and result.family == research.FAMILY


@pytest.mark.parametrize("seed", SEEDS)
def test_two_market_null_fails(seed):
    assert research.two_market(two_market_leagues(seed, False)).verdict == FAIL


# ---- premium ------------------------------------------------------------------------------------
def premium_leagues(seed: int, planted: bool) -> list[Season]:
    """Planted: the Divine premium jumps to a new level each league day, the same schedule in every
    league (a curve by league day knows it; the trailing 24 hours lag a day behind). Null: each
    league draws its own daily levels, so earlier leagues say nothing about this one."""
    rng = np.random.default_rng(seed)
    schedule = rng.uniform(0.0, 0.12, (DAYS, N))
    out = []
    for k in range(4):
        r = walk(rng, 0.01)
        levels = schedule if planted else rng.uniform(0.0, 0.12, (DAYS, N))
        prem = np.repeat(levels, 24, axis=0)
        b = r * np.exp(prem + rng.normal(0, 0.01, (T, N)))
        out.append(Season(make_panel(k, r, b)))
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_premium_planted_passes(seed):
    result = research.premium(premium_leagues(seed, True))
    assert result.verdict == PASS, result.lines()


@pytest.mark.parametrize("seed", SEEDS)
def test_premium_null_fails(seed):
    assert research.premium(premium_leagues(seed, False)).verdict == FAIL


def test_premium_baselines_are_causal():
    season = premium_leagues(1, True)[0]
    _, trailing, last = research.premium_target(season.panel)
    cut = 200
    _, trailing_cut, last_cut = research.premium_target(season.panel.window(0, cut))
    np.testing.assert_array_equal(trailing[:cut], trailing_cut)
    np.testing.assert_array_equal(last[:cut], last_cut)


# ---- orderbook ----------------------------------------------------------------------------------
def orderbook_leagues(seed: int, planted: bool) -> list[Season]:
    """Planted: buyers' stock over sellers' stock (log) wanders, and an hour after it rises the price
    rises by 4% of the change. Fair value cannot see that coming; the book can. Null: the book
    wanders the same way but the price ignores it."""
    rng = np.random.default_rng(seed)
    out = []
    for k in range(4):
        step = rng.normal(0, 0.5, (T, N))
        level = np.cumsum(step, axis=0)
        drift = rng.normal(0, 0.005, (T, N))
        if planted:
            drift[1:] += 0.04 * step[:-1]
        true = 100 * np.exp(np.cumsum(drift, axis=0))
        price = true * np.exp(rng.normal(0, 0.005, (T, N)))
        ask = np.full((T, N), 1000.0)
        bid = ask * np.exp(level) * price
        out.append(Season(make_panel(k, price, bid=bid, stock=ask)))
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_orderbook_planted_passes(seed):
    result = research.orderbook(orderbook_leagues(seed, True))
    assert result.verdict == PASS, result.lines()


@pytest.mark.parametrize("seed", SEEDS)
def test_orderbook_null_fails(seed):
    assert research.orderbook(orderbook_leagues(seed, False)).verdict == FAIL


def test_book_pressure_is_causal_and_treats_zero_stock_as_missing():
    panel = orderbook_leagues(1, True)[0].panel
    panel.data["r_stock_lo"][100, 0] = 0.0
    full = research.book_pressure(panel, 6)
    assert np.isnan(full[100, 0]) and np.isnan(full[106, 0])
    cut = research.book_pressure(panel.window(0, 150), 6)
    np.testing.assert_array_equal(full[:150], cut)


# ---- regime -------------------------------------------------------------------------------------
def regime_leagues(seed: int, planted: bool) -> list[Season]:
    """Twelve quiet random walks; in "Covens", item 0 is the Foulborn Exalted Orb. Planted: on day 6
    it falls from about 800 to about 32 over three hours, on heavy volume, as it did in the real
    Covens. Null: it stays a quiet walk like the rest, so there is nothing to flag."""
    rng = np.random.default_rng(seed)
    out = []
    for k, name in enumerate(("Old", "Covens")):
        price = walk(rng, 0.01, start=800.0) * np.exp(rng.normal(0, 0.02, (T, N)))
        volume = np.full((T, N), 100.0) * np.exp(rng.normal(0, 0.3, (T, N)))
        if planted and name == "Covens":
            crash = 6 * 24 + 5
            price[crash:, 0] *= np.r_[0.3, 0.1, np.full(T - crash - 2, 0.04)]
            volume[crash:crash + 24, 0] *= 5
        names = ["Foulborn Exalted Orb"] + [f"item{j}" for j in range(1, N)]
        out.append(Season(make_panel(name if name == "Covens" else k, price, volume=volume, names=names)))
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_regime_planted_passes(seed):
    result = research.regime(regime_leagues(seed, True))
    assert result.verdict == PASS, result.lines()
    case = result.candidates[0]
    assert case["event day"] == pytest.approx(6.2, abs=0.1) and case["flagged"]


@pytest.mark.parametrize("seed", SEEDS)
def test_regime_null_fails(seed):
    assert research.regime(regime_leagues(seed, False)).verdict == FAIL


def test_regime_without_the_known_case_is_not_enough_data():
    seasons = [s for s in regime_leagues(1, True) if s.name != "Covens"]
    assert research.regime(seasons).verdict == THIN


def test_regime_alarms_are_causal():
    panel = regime_leagues(1, True)[1].panel
    full = research.regime_alarms(panel["price"], panel["value"], panel["volume"])
    assert full[:, 0].any()
    for cut in (150, 160, 200):
        part = research.regime_alarms(panel["price"][:cut], panel["value"][:cut], panel["volume"][:cut])
        np.testing.assert_array_equal(full[:cut], part)


# ---- discovery and report-only ------------------------------------------------------------------
def test_recipes_find_a_fixed_ratio_that_holds_across_leagues():
    rng = np.random.default_rng(3)
    seasons = []
    for k in range(3):
        price = walk(rng, 0.02, n=6)
        price[:, 1] = price[:, 0] * 3 * np.exp(rng.normal(0, 0.005, T))  # three of item0 make one item1
        seasons.append(Season(make_panel(k, price)))
    result = research.recipes(seasons)
    pairs = {(c["pair"], c["ratio"]) for c in result.candidates}
    assert ("item0 / item1", "1:3") in pairs or ("item1 / item0", "3:1") in pairs
    assert len(result.candidates) == 1  # the free random walks drift; none hold a ratio


def test_relationships_find_a_leader():
    rng = np.random.default_rng(4)
    seasons = []
    for k in range(3):
        step = rng.normal(0, 0.02, (T, 6))
        step[6:, 1] = rng.normal(0, 0.005, T - 6) + step[:-6, 0]  # item1 repeats item0's move 6 hours later
        price = 100 * np.exp(np.cumsum(step, axis=0)) * np.exp(rng.normal(0, 0.002, (T, 6)))
        seasons.append(Season(make_panel(k, price)))
    result = research.relationships(seasons)
    leads = [c for c in result.candidates if c["kind"] == "leads"]
    assert any(c["pair"] == "item0 -> item1" and c["direction"] == "same way" for c in leads), leads


def test_spread_recovers_a_planted_bounce():
    rng = np.random.default_rng(6)
    true = walk(rng, 0.002)
    side = rng.choice([-1, 1], (T, N))
    price = true * np.exp(0.02 * side)  # each print lands half of a 4% spread above or below
    result = research.spread([Season(make_panel(0, price))])
    chaos = next(r for r in result.leagues if r["market"] == "Chaos")
    assert chaos["median spread"] == pytest.approx(0.04, rel=0.25)
    assert result.verdict == research.REPORT
