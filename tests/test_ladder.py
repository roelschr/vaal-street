"""The bankroll ladder and the compounding plan, on the synthetic archive and on injected rates."""

from __future__ import annotations

import shutil

import pytest

from conftest import NEW, call
from vaalstreet import ladder


@pytest.fixture()
def desk(con, archive_root):
    from vaalstreet.desk import Desk

    return Desk.build(con, NEW, root=archive_root)


def test_rungs_are_sorted_and_divines_convert_at_the_current_price():
    steps = ladder.rungs(150.0)
    assert [s["chaos"] for s in steps] == [25, 50, 100, 150, 200, 300, 400, 750, 1500]
    assert steps[3]["label"] == "1 Divine"
    # A Divine worth 100 Chaos lands on the 100 Chaos rung; the two are merged.
    assert [s["chaos"] for s in ladder.rungs(100.0)].count(100) == 1


def test_ladder_grows_and_says_what_opens(desk):
    rows = ladder.ladder(desk)
    by_label = {r["label"]: r for r in rows}
    # What a purse can run never shrinks as it grows.
    for lower, upper in zip(rows, rows[1:]):
        assert upper["can_run"] >= lower["can_run"]
    # Known answers on the synthetic archive (Divine about 148 Chaos):
    # the Chaos-market flip of the test orb first fits at 25 Chaos (one lot costs 19),
    assert "Chaos-market flips" in by_label["25 Chaos"]["opens_groups"]
    # buying for Chaos and selling for Divines needs a 135 Chaos lot, so it opens at 1 Divine,
    assert by_label["100 Chaos"]["counts"]["flips"] == 1
    assert "Chaos-to-Divine flips" in by_label["1 Divine"]["opens_groups"]
    # and the Divine-market flip needs a 285 Chaos lot, so it opens at 2 Divines, not before.
    assert "Divine-market flips" in by_label["2 Divines"]["opens_groups"]
    assert not any(o["group"] == "Divine-market flips" for r in rows if r["chaos"] < 285 for o in r["opens"])
    # Every option opens exactly once.
    keys = [o["key"] for r in rows for o in r["opens"]]
    assert len(keys) == len(set(keys))
    # The replayed rate sits next to the promised one, and the cautious rate is never above either.
    for r in rows:
        s = r["session"]
        assert s["rate"] <= s["expected_per_hour"] + 1e-9
        assert all(p["replayed_per_hour"] is not None for p in s["picks"])
        if r["next"] is not None and s["rate"] > 0:
            assert r["hours_to_next"] == pytest.approx((r["next"] - r["chaos"]) / s["rate"], rel=1e-3)


def test_compounding_switches_at_the_right_rung():
    # 10 Chaos an hour below 100, 50 an hour from 100: 25 -> 50 takes 2.5 h, 50 -> 100 takes 5 h,
    # then 100 -> 200 takes 2 h. A 9 hour session ends 1.5 h into the third step, at 175 Chaos.
    rate = lambda purse: (10.0, [{"label": "A"}]) if purse < 100 else (50.0, [{"label": "B"}])  # noqa: E731
    segs = ladder.compound(25, [25, 50, 100, 200, 400], rate, hours=9)
    assert [(s["from_hour"], s["to_hour"], s["switch_at"]) for s in segs] == [(0, 2.5, 50), (2.5, 7.5, 100), (7.5, 9, None)]
    assert segs[-1]["end_purse"] == 175 and segs[-1]["hours_to_next"] == 0.5
    assert [s["plans"][0]["label"] for s in segs] == ["A", "A", "B"]
    text = ladder.describe(segs)
    assert text[1].startswith("2.5–7.5 h: run A at 50 Chaos") and "switch plans" in text[1]
    assert "keep the same plans" in text[0]
    # A start between rungs is evaluated at the actual purse; a target stops the plan.
    segs = ladder.compound(30, [25, 50, 100], rate, target=60)
    assert [(s["purse"], s["end_purse"], s["reason"]) for s in segs] == [(30, 50, "next rung"), (50, 60, "target reached")]
    # Nothing earns: say so, never divide by zero.
    segs = ladder.compound(5, [25, 50], lambda p: (0.0, []), hours=6)
    assert segs[0]["to_hour"] is None and "nothing to compound" in ladder.describe(segs)[0]


@pytest.fixture()
def flat_desk(flat_root):
    from vaalstreet.db import connect
    from vaalstreet.desk import Desk

    con = connect(flat_root)
    yield Desk.build(con, NEW, root=flat_root)
    con.close()


def test_plan_end_to_end_uses_replayed_rates(flat_desk):
    out = ladder.plan(flat_desk, 25, target=50)
    assert out["schedule"][0]["rate"] == out["ladder"][0]["session"]["rate"] > 0
    assert out["schedule"][-1]["reason"] == "target reached"
    assert "last week" in out["note"]


def test_a_rising_divine_empties_the_session_plan_and_says_why(desk):
    """Known answer: over the hours the replay trades, the synthetic Divine goes from 112 to 147.75 Chaos
    (+32%), and the test orb's flips make +15% to +25% in Chaos. None beats holding Divines."""
    from vaalstreet.desk import session_plan

    flips = desk.flips(bankroll=5000)
    assert flips and all(0.1 < p["replay"]["return"] < 0.3 for p in flips)
    plan = session_plan(flips, 5000)
    assert plan["flips"] == [] and plan["trailing_divines"] == len(flips)
    assert plan["divines"] == pytest.approx(147.75 / 112 - 1, abs=0.01)
    rows = ladder.ladder(desk)
    assert all(r["session"]["rate"] == 0 and r["hours_to_next"] is None for r in rows)
    assert "Divines" in ladder.describe(ladder.compound(25, [25, 50], lambda p: (0.0, []), hours=6))[0]


def test_ladder_replays_each_pick_at_its_own_stake(flat_desk):
    """With the Divine flat, holding Divines makes nothing: the figure against Divines equals the one in Chaos."""
    rows = ladder.ladder(flat_desk)
    picks = [p for r in rows for p in r["session"]["picks"]]
    assert picks, "the test orb's flips qualify when Divines make nothing"
    for r in rows:
        s = r["session"]
        assert s["rate"] <= s["expected_per_hour"] + 1e-9
        assert s["rate"] <= max(s["replayed_vs_divine_per_hour"], 0) + 1e-9
    for p in picks:
        at = p["replay_at_stake"]
        assert at["whole_lots"] and at["stake"] == pytest.approx(p["stake"], rel=1e-3)
        assert at["divines"] == 0 and p["replayed_vs_divine_per_hour"] == pytest.approx(p["replayed_per_hour"])
        assert p["replayed_per_hour"] == pytest.approx(at["return"] * at["stake"] / at["hours"], rel=0.01, abs=1e-4)


def test_api_ladder(server):
    status, body = call(f"{server}/api/ladder?bankroll=25&hours=6")
    assert status == 200
    assert body["ladder"][0]["label"] == "25 Chaos" and body["schedule"] and body["schedule_text"]


def test_cli_ladder(archive_root, tmp_path, monkeypatch, capsys):
    from vaalstreet import cli

    root = tmp_path / "data"
    shutil.copytree(archive_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    cli.main(["ladder", "--league", NEW, "--from", "25", "--hours", "6"])
    out = capsys.readouterr().out
    assert "Hours to next" in out and "2 Divines (first Divine-market flips): " in out and "From 25 Chaos" in out and "not a promise" in out


def test_cli_ladder_uses_the_gold_fees_you_recorded(flat_root, tmp_path, monkeypatch, capsys):
    from conftest import ITEM
    from vaalstreet import cli
    from vaalstreet.ledger import Ledger

    root = tmp_path / "data"
    # Flat Divine, so some plan beats holding Divines and the session has Gold to count.
    shutil.copytree(flat_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    cli.main(["ladder", "--league", NEW, "--from", "25"])
    assert "Gold/hour" in (blind := capsys.readouterr().out)
    # No fee on record: the 25 Chaos row has no Gold figure.
    assert next(line for line in blind.splitlines() if line.startswith("25 Chaos")).split()[-1] == "-"
    # One trade of 10 test orbs that cost 50 Gold: 5 Gold a unit, the same figure the API would use.
    ledger = Ledger(root)
    ledger.add_trade(NEW, ITEM, "buy", 10, 9.5, gold=50)
    assert ledger.costs(NEW).fee(ITEM)[0] == 5
    cli.main(["ladder", "--league", NEW, "--from", "25"])
    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("25 Chaos"))
    assert row.split()[-1] != "-" and float(row.split()[-1].replace(",", "")) > 0


def test_ladder_counts_your_own_recipes(desk):
    from conftest import CARD, ITEM, OTHER

    # Made up, as in test_models: 2 cards and 1 shard make 1 orb, about 5 Chaos for an orb worth
    # about 10. One set fits a 25 Chaos purse, so it opens at the first step, and only with `fixed`.
    mine = [{"id": 7, "kind": "recipe", "inputs": [(CARD, 2), (OTHER, 1)], "output": ITEM, "gives": 1, "note": ""}]
    plain = ladder.ladder(desk)[0]
    yours = ladder.plan(desk, 25, fixed=mine)["ladder"][0]
    assert yours["counts"]["conversions"] == plain["counts"]["conversions"] + 1
    assert len(yours["opens"]) == len(plain["opens"]) + 1


def test_conversions_are_capped_by_the_purse_and_labels_read_well():
    # Known answer: a purse of 25 buys at most 12.5 Chaos of inputs an hour (once per two-hour cycle).
    # The 50% conversion takes 2 sets an hour at 5 c (10 c), the 10% one gets the 2.5 c left: 0.25 sets.
    rich = {"cost": 5.0, "sets_per_hour": 2.0, "profit_per_hour": 5.0}     # 2.5 c a set, 50%
    poor = {"cost": 10.0, "sets_per_hour": 100.0, "profit_per_hour": 100.0}  # 1 c a set, 10%, huge flow
    assert ladder.conversion_rate([poor, rich], 25) == pytest.approx(2 * 2.5 + 0.25 * 1.0)
    assert ladder.conversion_rate([poor, rich], 10_000) == pytest.approx(105.0), "a big purse takes the whole flow"
    assert ladder.counts_text({"flips": 1, "conversions": 2, "round_trips": 0}) == "1 flip, 2 conversions and 0 round trips"
    label = ladder._conversion_option({"id": "x", "name": "Test Card", "need": 2.0, "gives": 1.0, "reward": "Test Orb",
                                       "cost": 3})["label"]
    assert label == "2 Test Card into 1 Test Orb"


def test_ladder_conversion_rate_grows_with_the_purse_up_to_the_full_flow(desk):
    runner = ladder.Ladder(desk)
    full = sum(c["profit_per_hour"] for c in runner.conversions())
    rows = ladder.ladder(desk, runner=runner)
    rates = [r["conversion_per_hour"] for r in rows]
    assert rates == sorted(rates) and rates[-1] <= full + 1e-6
    assert rates[0] < full, "25 Chaos cannot take every conversion's full flow"
    assert all(r["counts_text"].count(",") == 1 for r in rows)


def test_a_replay_at_the_stake_is_never_rescaled():
    from vaalstreet.desk import replayed_rate

    # Stored stakes are rounded; the replay tied up more than the stake (stock marked up). Neither rescales it.
    at = {"return": 1.0, "stake": 528.2, "capital": 601.8, "hours": 100, "divines": 0.0, "at_stake": True}
    assert replayed_rate(at, 528.25, against_divine=False) == pytest.approx(5.282)
    # A 1,000 Chaos replay that only ever used 500 earns the same on 500, and half as much on 250.
    fixed = {"return": 0.5, "capital": 500.0, "hours": 100, "divines": 0.0}
    assert replayed_rate(fixed, 500, against_divine=False) == pytest.approx(5.0)
    assert replayed_rate(fixed, 250, against_divine=False) == pytest.approx(2.5)
    # Against Divines that rose 25%: (stake + profit) / 1.25 - stake.
    assert replayed_rate(fixed | {"divines": 0.25}, 500) == pytest.approx(((500 + 500) / 1.25 - 500) / 100)


def test_session_plan_carries_last_weeks_rates(flat_desk):
    from vaalstreet.desk import session_plan

    plan = session_plan(flat_desk.flips(bankroll=1000), 1000)
    assert plan["flips"]
    for p in plan["flips"]:
        assert p["replayed_per_hour"] is not None and p["replayed_vs_divine_per_hour"] == pytest.approx(p["replayed_per_hour"])
    assert plan["replayed_per_hour"] == pytest.approx(sum(p["replayed_per_hour"] for p in plan["flips"]))


def test_research_ladder_rates_reads_the_ladders_fields(flat_root):
    from vaalstreet import research
    from vaalstreet.db import connect

    con = connect(flat_root)
    try:
        rows = research.ladder_rates(con, NEW, root=flat_root).leagues
    finally:
        con.close()
    assert rows and all("replayed vs Divines per hour" in r and r["Divines over those hours"] in (0.0, None) for r in rows)
    assert any(r["picks"] for r in rows)


def test_plan_without_a_divine_price_is_valid_json():
    """In the first hour of a league nobody has traded a Divine yet, and some leagues never do. The plan
    then gives None for the Divine price, not NaN: NaN is not JSON, so the browser would refuse the whole
    answer and the Ladder page would not load. A desk in that state has no Divine-market plans either,
    so a bare stand-in desk with nothing to run is the realistic case."""
    import json
    import math

    class NoDivineYet:
        bridge = math.nan

        def flips(self, **_):
            return []

        conversions = loops = flips

        def replay_at(self, picks, share=0.1):
            return []

    out = ladder.plan(NoDivineYet(), 25, hours=6)
    assert out["bridge"] is None
    assert [r["label"] for r in out["ladder"]] == ["25 Chaos", "50 Chaos", "100 Chaos", "200 Chaos", "400 Chaos"]
    json.dumps(out, allow_nan=False)  # raises on any NaN left in the answer
