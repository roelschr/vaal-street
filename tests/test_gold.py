"""Gold per plan, the Gold-budgeted session plan, and the time each plan takes."""

from __future__ import annotations

import pytest

from conftest import ITEM, NEW
from vaalstreet.desk import Costs, Desk, session_plan


@pytest.fixture()
def desk(con, archive_root):
    return Desk.build(con, NEW, root=archive_root)


def test_flip_gold_from_learned_assumed_and_unknown_fees(desk):
    learned = {ITEM: {"gold_per_unit": 5.0, "trades": 3}}
    plans = desk.flips(min_margin=0.0, costs=Costs(learned, assumed=2.0, minutes_per_order=1.5))
    mine = next(p for p in plans if p["id"] == ITEM)
    # Known answer: a unit is bought and sold, two orders at 5 Gold per unit each.
    assert mine["gold"] == 10 and mine["gold_sources"] == [{"id": ITEM, "source": "your trades", "trades": 3}]
    assert mine["chaos_per_1000_gold"] == pytest.approx(1000 * (mine["sell"] - mine["buy"]) / 10, rel=1e-3)
    units = mine["profit_per_hour"] / (mine["sell"] - mine["buy"])
    assert mine["gold_per_hour"] == pytest.approx(10 * units, rel=1e-3)
    # Two orders at 1.5 minutes each: three minutes of your time per hour of market time.
    assert mine["orders"] == 2 and mine["minutes"] == 3
    assert mine["profit_per_own_hour"] == pytest.approx(mine["profit_per_hour"] * 20, rel=1e-3)

    others = [p for p in plans if p["id"] != ITEM]
    assert others and all(p["gold"] == 4 and p["gold_sources"][0]["source"] == "your assumption" for p in others)

    blind = desk.flips(min_margin=0.0)
    assert len(blind) == len(plans), "an unknown fee never drops a plan"
    assert all(p["gold"] is None and p["chaos_per_1000_gold"] is None and p["gold_per_hour"] is None
               and p["gold_sources"][0]["source"] == "unknown" and p["minutes"] == 2 for p in blind)


def test_zero_fee_gives_no_ratio_rather_than_infinity(desk):
    plan = desk.flips(min_margin=0.0, costs=Costs(assumed=0.0))[0]
    assert plan["gold"] == 0 and plan["chaos_per_1000_gold"] is None


def test_conversion_and_round_trip_gold(desk):
    rows = desk.conversions(costs=Costs(assumed=1.0))
    card = next(r for r in rows if r["reward_id"] != "chaos")
    assert card["gold"] == card["need"] + card["gives"] and card["orders"] == 2
    assert card["chaos_per_1000_gold"] == pytest.approx(1000 * (card["proceeds"] - card["cost"]) / card["gold"], rel=1e-3)
    assert all(r["gold"] is None for r in desk.conversions())

    loops = desk.loops(costs=Costs(assumed=1.0))
    assert loops
    trip = loops[0]
    # Per unit of the first item: buy it (1), give it (1), then give or sell what each swap brought.
    expected, units = 2.0, 1.0
    for rate in trip["rates"]:
        units *= rate
        expected += units
    assert trip["gold"] == pytest.approx(expected, rel=1e-3) and trip["orders"] == len(trip["path"]) + 1
    first = trip["path"][0]["id"]
    partial = desk.loops(costs=Costs({first: {"gold_per_unit": 1.0, "trades": 1}}))
    same = next(r for r in partial if r["path"] == trip["path"])
    assert same["gold"] is None, "one item without a fee makes the trip's Gold unknown"


def plan(id, margin, lot, flow, per_1000, kind="chaos"):
    return {"id": id, "name": id, "kind": kind, "margin": margin, "lot_cost": lot, "flow_value": flow,
            "replay": {"return": 0.1, "sales": 3}, "buy_order": {}, "sell_order": {},
            "chaos_per_1000_gold": per_1000, "minutes": 2}


def test_session_plan_without_budget_matches_the_old_rule():
    plans = [plan("a", 0.10, 10, 1000, 5), plan("b", 0.20, 10, 1000, 1), plan("c", 0.05, 500, 1000, 50),
             plan("a", 0.30, 10, 1000, 9, "cross")]
    out = session_plan(plans, 100)
    # Known answer: stakes are 25 each (a quarter of 100), ranked by margin; one plan per item; "c"'s lot is too big.
    assert [p["id"] for p in out["flips"]] == ["a", "b"] and out["flips"][0]["kind"] == "cross"
    assert out["expected_per_hour"] == pytest.approx(0.3 * 12.5 + 0.2 * 12.5) and out["deployed"] == 50
    assert out["gold_unknown"] == [] and out["gold_per_hour"] is None


def test_session_plan_under_a_gold_budget():
    plans = [plan("a", 0.10, 10, 1000, 5), plan("b", 0.20, 10, 1000, 1), plan("d", 0.10, 10, 1000, 20),
             plan("u", 0.50, 10, 1000, None)]
    out = session_plan(plans, 100, gold_budget=200)
    # "d" earns 1.25 c/h at 20 c per 1,000 Gold: 62.5 Gold. "a" earns 1.25 at 5: 250 Gold, more than the
    # 137.5 left, so its stake shrinks to 25 x 137.5 / 250 = 13.75 (still above one lot). "b" gets no Gold.
    assert [p["id"] for p in out["flips"]] == ["d", "a"]
    assert out["flips"][1]["stake"] == pytest.approx(13.75) and out["gold_per_hour"] == pytest.approx(200)
    assert out["gold_unknown"] == [{"id": "u", "name": "u", "kind": "chaos"}], "unknown Gold is listed, not included"
    assert out["minutes"] == 4
