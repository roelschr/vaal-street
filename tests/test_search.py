"""Strategy search and walk-forward evaluation, on a small archive of its own.

Known structure (hours from the archive's first hour; each challenge league lasts 144 hours):

    Alpha    0..143    the test orb rises 50% over the league
    Beta   160..303    rises 50%
    Gamma  290..433    falls 20%; starts while Beta is still running (tail overlap)
    Delta  450..593    falls 20%
    Standard runs through all of it and is never a challenge league.
    Bonus Event 470..569, inside Delta: an event league ("Event" in its name), never a fold by default.

The grid is `hold` with warmup=24 (buy the orb on day one) or warmup=100000 (never buy, return 0).
Scored on Chaos return, buying wins on Alpha and Beta and loses on Gamma and Delta. So:

* Beta is tuned on Alpha and picks warmup=24.
* Gamma is tuned on Alpha only (Beta had not ended when Gamma started) and picks warmup=24. Tuned on
  Gamma itself it would pick 100000, so any look-ahead would show.
* Delta is tuned on Alpha, Beta and Gamma: the mean favours buying (+~45%, +~45%, -~18%), so 24 again,
  although Delta itself favours never buying.
"""

from __future__ import annotations

import gzip
import json

import pytest

from conftest import H0, HOUR, ITEM, call, market
from vaalstreet import backtest, items, search
from vaalstreet.archive import hour_path
from vaalstreet.ingest import CHAOS, DIVINE, ingest

LEN = 144
LEAGUES = {"Alpha": (0, 0.5), "Beta": (160, 0.5), "Gamma": (290, -0.2), "Delta": (450, -0.2)}
EVENT = "Bonus Event"
END = 450 + LEN
GRID = "warmup=24,100000"


def _digest(hour: int, names: set[str]) -> dict:
    h0 = (hour - H0) // HOUR
    markets = [market("Standard", ITEM, CHAOS, 100, 1000), market("Standard", CHAOS, DIVINE, 4000, 40)]
    for league, (start, move) in LEAGUES.items():
        h = h0 - start
        if league not in names or not 0 <= h < LEN:
            continue
        p = 10 * (1 + move * h / LEN)
        markets += [market(league, CHAOS, DIVINE, 4000, 40), market(league, ITEM, CHAOS, 1000, round(1000 * p))]
    if "Delta" in names and 470 <= h0 < 570:
        markets += [market(EVENT, CHAOS, DIVINE, 4000, 40), market(EVENT, ITEM, CHAOS, 1000, 10000)]
    return {"next_change_id": hour + HOUR, "markets": markets}


def build(root, names: set[str]):
    hours = [H0 + h * HOUR for h in range(END)
             if any(n in names and LEAGUES[n][0] <= h < LEAGUES[n][0] + LEN for n in LEAGUES)]
    for hour in hours:
        path = hour_path(root, "poe1", hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(_digest(hour, names)).encode()))
    meta = items.build({
        CHAOS: {"name": "Chaos Orb", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {}},
        DIVINE: {"name": "Divine Orb", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {}},
        ITEM: {"name": "Test Orb", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {}},
    })
    target = root / "items" / "poe1.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"version": items.VERSION, "items": meta}))
    items.load.cache_clear()
    ingest(root, log=None)
    items.load.cache_clear()
    from vaalstreet.db import connect

    return connect(root)


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    con = build(tmp_path_factory.mktemp("search_full"), set(LEAGUES))
    yield con
    con.close()


def run(con, **kw):
    return search.walk_forward(con, "hold", GRID, how="return", capital=1000, slippage=0.0, **kw)


def test_folds_skip_overlapping_leagues_and_standard(full):
    rows = search.league_rows(full)
    assert [r["league"] for r in rows] == ["Alpha", "Beta", "Gamma", "Delta"]
    plan = {test["league"]: [r["league"] for r in train] for test, train in search.folds(rows)}
    assert plan == {"Beta": ["Alpha"], "Gamma": ["Alpha"], "Delta": ["Alpha", "Beta", "Gamma"]}


def test_event_league_is_not_a_fold_by_default(full):
    family = dict(full.execute("SELECT league, family FROM leagues").fetchall())
    assert family[EVENT] == "event" and family["Delta"] == "challenge"
    assert EVENT not in [r["league"] for r in search.league_rows(full)]
    assert EVENT in [r["league"] for r in search.league_rows(full, ["Alpha", EVENT])]  # only when asked for


def test_walk_forward_reports_out_of_sample_choice(full):
    rep = run(full)
    picked = {f["league"]: f["setting"]["warmup"] for f in rep["folds"]}
    assert picked == {"Beta": 24, "Gamma": 24, "Delta": 24}
    assert rep["settings_tried"] == 2 and rep["stability"] == {"changes": 0, "of": 2}
    gamma = next(f for f in rep["folds"] if f["league"] == "Gamma")
    assert gamma["return"] < 0 < gamma["fit_on_earlier"], "fitted on a rising league, tested on a falling one"
    # hold's own default is warmup=24, so here the search only matches the default
    assert all(f["score"] == f["default_score"] for f in rep["folds"]) and rep["pooled"]["beat_default"] == 0
    text = search.format_report(rep)
    assert "2 settings tried" in text and "fit on earlier leagues" in text


def test_choice_does_not_see_the_test_league_or_later(full, tmp_path):
    rep = run(full)
    chosen = {f["league"]: f["setting"] for f in rep["folds"]}
    # Control: tuned on Gamma itself, the search would pick "never buy". A leak would show.
    tried = search.settings(search.parse_grid(GRID, "hold"))
    runner = search.Runner(full, "hold", dict(capital=1000, slippage=0.0))
    gamma = [r for r in search.league_rows(full) if r["league"] == "Gamma"]
    assert tried[search._pick(runner, tried, gamma, "return")[0]] == {"warmup": 100000}

    # Without Delta in the archive, the folds that remain choose the same.
    early = build(tmp_path / "no_delta", {"Alpha", "Beta", "Gamma"})
    assert {f["league"]: f["setting"] for f in run(early)["folds"]} == {k: chosen[k] for k in ("Beta", "Gamma")}
    # Without Gamma or Delta at all, the pick for a league starting at Gamma's hour is the same.
    start = next(r["first_hour"] for r in search.league_rows(full) if r["league"] == "Gamma")
    bare = build(tmp_path / "no_gamma", {"Alpha", "Beta"})
    pick = search.choose(bare, "hold", GRID, start, how="return", capital=1000, slippage=0.0)
    assert pick["setting"] == chosen["Gamma"] and pick["tuned_on"] == ["Alpha"]
    for c in (early, bare):
        c.close()


def test_cached_run_matches_backtest(full):
    runner = search.Runner(full, "hold", dict(capital=1000, slippage=0.0))
    assert runner("Beta", {"warmup": 24})["end"] == backtest.run(
        full, "Beta", "hold", params={"warmup": 24}, capital=1000, slippage=0.0).summary()["end"]


def test_grid_syntax_and_limits():
    assert search.parse_grid("reach=0.2,0.3; attention=1,4;whole_lots=false,true", "swing") == {
        "reach": [0.2, 0.3], "attention": [1, 4], "whole_lots": [False, True]}
    assert search.parse_grid('{"schedule": ["sat,sun 10-14", "mon-fri 19-22"]}', "swing")["schedule"][0] == "sat,sun 10-14"
    with pytest.raises(ValueError, match="no setting 'nope'"):
        search.parse_grid("nope=1", "swing")
    with pytest.raises(ValueError, match="limit is 200"):
        search.settings(search.parse_grid("reach=" + ",".join(map(str, range(15))) + ";top=" + ",".join(map(str, range(15))), "swing"))


def test_options_in_grid_reach_the_simulation(full):
    runner = search.Runner(full, "hold", dict(capital=1000, slippage=0.0))
    every = runner("Beta", {"warmup": 24})
    rare = runner("Beta", {"warmup": 24, "attention": 1000})  # hour 24 is never a visit
    assert every["fills"] > 0 and rare["fills"] == 0


def test_search_api_runs_a_capped_search(server):
    status, rep = call(server + "/api/search", {"strategy": "hold", "grid": "warmup=24,48", "leagues": ["Old", "New"],
                                               "score": "return"})
    assert status == 200 and rep["settings_tried"] == 2 and [f["league"] for f in rep["folds"]] == ["New"]
    status, err = call(server + "/api/search", {"strategy": "hold", "grid": "warmup=" + ",".join(map(str, range(30)))})
    assert status == 400 and "limit" in err["error"]
    status, err = call(server + "/api/search", {"strategy": "x.py:Mine", "grid": "a=1"})
    assert status == 400
