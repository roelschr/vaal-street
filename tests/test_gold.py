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


def plan(id, margin, lot, flow, per_1000, kind="chaos", buy=1.0, gold=None):
    """A hand-made flip plan. `gold` is Gold per unit flipped (both orders)."""
    if gold is None and per_1000 is not None:
        gold = 1000 * margin * buy / per_1000
    return {"id": id, "name": id, "kind": kind, "margin": margin, "lot_cost": lot, "flow_value": flow,
            "replay": {"return": 0.1, "sales": 3}, "buy_order": {}, "sell_order": {}, "buy": buy,
            "gold": gold, "chaos_per_1000_gold": per_1000, "minutes": 2}


def test_session_plan_without_budget_matches_the_old_rule():
    plans = [plan("a", 0.10, 10, 1000, 5), plan("b", 0.20, 10, 1000, 1), plan("c", 0.05, 500, 1000, 50),
             plan("a", 0.30, 10, 1000, 9, "cross")]
    out = session_plan(plans, 100)
    # Known answer: stakes are 25 each (a quarter of 100), ranked by margin; one plan per item; "c"'s lot is too big.
    assert [p["id"] for p in out["flips"]] == ["a", "b"] and out["flips"][0]["kind"] == "cross"
    assert out["expected_per_hour"] == pytest.approx(0.3 * 12.5 + 0.2 * 12.5) and out["deployed"] == 50
    assert out["gold_unknown"] == [] and out["gold_used"] is None


def test_a_lot_bigger_than_a_quarter_must_fill_within_the_session():
    # Like Omen of Fortune on Covens: one unit costs 84% of a 3,000 c purse and trades 0.05 an hour,
    # a lot every 20 hours. A lot of the same size that fills in 2 hours stays, flagged.
    slow = plan("slow", 0.50, 2521, 10_000, None) | {"units_per_hour": 0.05, "buy_order": {"item_units": 1}}
    fast = plan("fast", 0.20, 1200, 10_000, None) | {"units_per_hour": 0.5, "buy_order": {"item_units": 1}}
    small = plan("small", 0.10, 50, 10_000, None) | {"units_per_hour": 0.01}
    out = session_plan([slow, fast, small], 3000)
    picks = {p["id"]: p for p in out["flips"]}
    assert set(picks) == {"fast", "small"}
    assert picks["fast"]["stake"] == 1200 and picks["fast"]["lot_share"] == 0.4, "one lot is 40% of your purse"
    assert picks["small"]["stake"] == 750 and picks["small"]["lot_share"] is None, "a quarter, as before"


def test_session_plan_under_a_gold_budget_for_the_session():
    plans = [plan("a", 0.10, 10, 1000, 5), plan("b", 0.20, 10, 1000, 1), plan("d", 0.10, 10, 1000, 20),
             plan("u", 0.50, 10, 1000, None)]
    out = session_plan(plans, 100, gold_budget=300)
    # Known answer: at buy price 1, "d" costs 1000 x 0.1 / 20 = 5 Gold per unit flipped, so posting its
    # 25 c stake once costs 125 Gold. "a" costs 20 Gold a unit: 25 units would be 500, more than the 175
    # left, so its stake shrinks to 175 / 20 = 8.75 - below its lot of 10, so it is skipped. "b" (200 a
    # unit) cannot post one lot either. The plan stops at 125 of 300 Gold.
    assert [p["id"] for p in out["flips"]] == ["d"] and out["gold_used"] == pytest.approx(125)
    assert out["flips"][0]["gold_to_post"] == pytest.approx(125)
    assert out["gold_unknown"] == [{"id": "u", "name": "u", "kind": "chaos"}], "unknown Gold is listed, not included"
    cut = session_plan([plan("a", 0.10, 5, 1000, 5)], 100, gold_budget=300)["flips"][0]
    assert cut["stake"] == pytest.approx(15) and cut["gold_to_post"] == pytest.approx(300), "cut at a known total"


def test_free_orders_rank_first_not_as_unknown():
    free = plan("f", 0.05, 10, 1000, None, gold=0)
    out = session_plan([plan("a", 0.10, 10, 1000, 5), free], 100, gold_budget=10)
    assert [p["id"] for p in out["flips"]] == ["f"] and out["gold_unknown"] == [] and out["gold_used"] == 0


def test_gold_for_vendor_swaps_divine_stops_and_your_recipes(desk):
    from conftest import CARD, EMBER, OTHER
    from vaalstreet.ingest import CHAOS, DIVINE

    fees = {item: {"gold_per_unit": g, "trades": 1} for item, g in
            ((ITEM, 10.0), (OTHER, 2.0), (CARD, 1.0), (EMBER, 3.0), (DIVINE, 50.0))}
    costs = Costs(fees)
    # A made-up vendor sells an orb for 5 Chaos: no order to buy the Chaos, none for the vendor,
    # one to sell the orb. Gold is per unit of the first item: 1 Chaos buys 1/5 orb, so 10 / 5 = 2.
    vendor = [{"kind": "vendor", "inputs": [(CHAOS, 5)], "output": ITEM, "gives": 1}]
    row = next(r for r in desk.loops(min_value=10, chaos_only=True, fixed=vendor, costs=costs) if r["via"] == ["vendor"])
    assert row["gold"] == 2 and row["orders"] == 1 and [s["id"] for s in row["gold_sources"]] == [ITEM]

    # Orb -> Divine -> ember, per orb: buy the orb (10), give it for Divines (10), give the Divines
    # for embers (50 x Divines per orb), sell the embers (3 x embers per orb). Four orders.
    row = next(r for r in desk.loops(min_value=10, costs=costs) if "divine" in r["via"])
    divines, embers = row["rates"][0], row["rates"][0] * row["rates"][1]
    assert row["gold"] == pytest.approx(10 + 10 + 50 * divines + 3 * embers, rel=1e-3)
    assert row["orders"] == 4 and row["gold_per_day"] is not None

    # Your recipe: 2 cards + 1 shard + 4 Chaos make 1 orb. Gold = 2 x 1 + 1 x 2 (no order for Chaos) + 10 to sell.
    mine = [{"id": 1, "kind": "recipe", "inputs": [(CARD, 2), (OTHER, 1), (CHAOS, 4)], "output": ITEM, "gives": 1}]
    row = next(r for r in desk.conversions(fixed=mine, costs=costs) if r["how"] == "yours")
    assert row["gold"] == 14 and row["orders"] == 3
    assert {s["id"] for s in row["gold_sources"]} == {CARD, OTHER, ITEM}
