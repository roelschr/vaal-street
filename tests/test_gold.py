"""Gold per plan, the Gold-budgeted session plan, and the time each plan takes."""

from __future__ import annotations

import pytest

from conftest import ITEM, NEW
from vaalstreet.desk import Costs, Desk, session_plan
from vaalstreet.ingest import CHAOS, DIVINE


@pytest.fixture()
def desk(con, archive_root):
    return Desk.build(con, NEW, root=archive_root)


def asked(plan):
    """What one item's sell order wants: (the currency's id, how much of it per item)."""
    order = plan["sell_order"]
    return (DIVINE if order["currency"] == "Divine" else CHAOS), order["currency_units"] / order["item_units"]


def test_flip_gold_from_learned_table_assumed_and_unknown_fees(desk):
    learned = {ITEM: {"gold_per_unit": 5.0, "trades": 3}}
    # The table knows the two currencies, and has a number for the orb that your own record must beat.
    table = {CHAOS: 0.5, DIVINE: 20.0, ITEM: 99.0}
    plans = desk.flips(min_margin=0.0, costs=Costs(learned, assumed=2.0, minutes_per_order=1.5, table=table))
    mine = next(p for p in plans if p["id"] == ITEM)
    # Known answer: the buy order wants the orb, 5 Gold from your record. The sell order wants its
    # market's currency: what one orb is sold for, times 0.5 Gold a Chaos or 20 a Divine.
    currency, per_item = asked(mine)
    assert mine["gold_buy"] == 5 and mine["gold_sell"] == pytest.approx(per_item * table[currency], rel=1e-3)
    assert mine["gold"] == pytest.approx(5 + per_item * table[currency], rel=1e-3)
    assert mine["gold_sources"] == [{"id": ITEM, "source": "your trades", "trades": 3},
                                    {"id": currency, "source": "fee table"}]
    assert mine["chaos_per_1000_gold"] == pytest.approx(1000 * (mine["sell"] - mine["buy"]) / mine["gold"], rel=1e-3)
    units = mine["profit_per_hour"] / (mine["sell"] - mine["buy"])
    assert mine["gold_per_hour"] == pytest.approx(mine["gold"] * units, rel=1e-3)
    # Two orders at 1.5 minutes each: three minutes of your time per hour of market time.
    assert mine["orders"] == 2 and mine["minutes"] == 3
    assert mine["profit_per_own_hour"] == pytest.approx(mine["profit_per_hour"] * 20, rel=1e-3)

    # Items neither your records nor the table know fall back to your assumed 2 Gold.
    others = [p for p in plans if p["id"] not in (ITEM, DIVINE)]
    assert others and all(p["gold_buy"] == 2 and p["gold_sources"][0]["source"] == "your assumption" for p in others)

    blind = desk.flips(min_margin=0.0)
    assert len(blind) == len(plans), "an unknown fee never drops a plan"
    assert all(p["gold"] is None and p["chaos_per_1000_gold"] is None and p["gold_per_hour"] is None
               and p["gold_sources"][0]["source"] == "unknown" and p["minutes"] == 2 for p in blind)
    # The item's fee alone is not enough: the sell order's Gold is unknown without the currency's fee.
    half = next(p for p in desk.flips(min_margin=0.0, costs=Costs(learned)) if p["id"] == ITEM)
    assert half["gold_buy"] == 5 and half["gold_sell"] is None and half["gold"] is None


def test_an_order_pays_for_what_it_wants():
    # The two buy orders read off the game in November 2026, at the fees poedb lists (a Stacked Deck
    # 35, an Eldritch Exalted Orb 525), and the same rule applied to an order that wants Chaos (15 each),
    # which is how the desk assumes a sale is charged.
    costs = Costs(table={"deck": 35.0, "eldritch": 525.0, CHAOS: 15.0, DIVINE: 250.0})
    assert costs.order("deck", 13)[0] == 455       # 13 Stacked Decks wanted for 43 Chaos
    assert costs.order("eldritch", 1)[0] == 525    # 1 Eldritch Exalted Orb wanted for 2 Chaos
    assert costs.order(CHAOS, 140)[0] == 2100      # 140 Chaos wanted for 1 Divine
    # As the ledger records orders: a buy wants the item, a sell wants its market's currency.
    assert costs.posted("deck", "buy", "chaos", 13, 43 / 13) == 455
    assert costs.posted("deck", "sell", "chaos", 20, 3.5) == 20 * 3.5 * 15
    assert costs.posted("deck", "sell", "divine", 49, 1 / 49) == pytest.approx(250)
    assert Costs().posted("deck", "sell", "divine", 49, 1 / 49) is None

    # Known answer for one deck flipped: 35 Gold to buy it. Sold 2 for 7 Chaos, the sell order wants
    # 3.5 Chaos: 52.5 Gold. Sold 49 for 1 Divine, it wants 1/49 Divine: about 5.1 Gold.
    deck = {"id": "deck", "buy": 3.33, "sell": 3.5, "profit_per_hour": 17.0, "units_per_hour": 100.0,
            "buy_order": {"currency": "Chaos", "currency_units": 10, "item_units": 3},
            "sell_order": {"currency": "Chaos", "currency_units": 7, "item_units": 2}}
    in_chaos = costs.flip(deck)
    assert (in_chaos["gold_buy"], in_chaos["gold_sell"], in_chaos["gold"]) == (35, 52.5, 87.5)
    in_divines = costs.flip(deck | {"sell_order": {"currency": "Divine", "currency_units": 1, "item_units": 49}})
    assert in_divines["gold"] == pytest.approx(35 + 250 / 49, rel=1e-3)
    assert [s["id"] for s in in_divines["gold_sources"]] == ["deck", DIVINE]


def test_zero_fee_gives_no_ratio_rather_than_infinity(desk):
    plan = desk.flips(min_margin=0.0, costs=Costs(assumed=0.0))[0]
    assert plan["gold"] == 0 and plan["chaos_per_1000_gold"] is None


def test_conversion_and_round_trip_gold(desk):
    rows = desk.conversions(costs=Costs(assumed=1.0))
    card = next(r for r in rows if r["reward_id"] != "chaos")
    # At 1 Gold for anything wanted: the cards bought, and the Chaos the reward is sold for.
    assert card["gold"] == pytest.approx(card["need"] + card["proceeds"], rel=1e-3) and card["orders"] == 2
    assert card["chaos_per_1000_gold"] == pytest.approx(1000 * (card["proceeds"] - card["cost"]) / card["gold"], rel=1e-3)
    assert all(r["gold"] is None for r in desk.conversions())

    loops = desk.loops(costs=Costs(assumed=1.0))
    assert loops
    trip = loops[0]
    # Per unit of the first item: buy it (1), then each swap wants what it brings, and the sale
    # wants the Chaos the trip ends with.
    expected, units = 1.0, 1.0
    for rate in trip["rates"]:
        units *= rate
        expected += units
    expected += trip["buy"] * (1 + trip["edge"])
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

    fees = {item: {"gold_per_unit": g, "trades": 1} for item, g in
            ((ITEM, 10.0), (OTHER, 2.0), (CARD, 1.0), (EMBER, 3.0), (DIVINE, 50.0), (CHAOS, 0.5))}
    costs = Costs(fees)
    # A made-up vendor sells an orb for 5 Chaos: no order to buy the Chaos, none for the vendor, one
    # to sell the orb, which wants Chaos. Per Chaos handed over: 0.5 Gold for each Chaos the sale brings.
    vendor = [{"kind": "vendor", "inputs": [(CHAOS, 5)], "output": ITEM, "gives": 1}]
    row = next(r for r in desk.loops(min_value=10, chaos_only=True, fixed=vendor, costs=costs) if r["via"] == ["vendor"])
    assert row["gold"] == pytest.approx(0.5 * row["buy"] * (1 + row["edge"]), rel=1e-3)
    assert row["orders"] == 1 and [s["id"] for s in row["gold_sources"]] == [CHAOS]

    # Orb -> Divine -> ember, per orb: buy the orb (10), ask Divines for it (50 x Divines per orb), ask
    # embers for the Divines (3 x embers per orb), sell the embers (0.5 x the Chaos they bring). Four orders.
    row = next(r for r in desk.loops(min_value=10, costs=costs) if "divine" in r["via"])
    divines, embers = row["rates"][0], row["rates"][0] * row["rates"][1]
    assert row["gold"] == pytest.approx(10 + 50 * divines + 3 * embers + 0.5 * row["buy"] * (1 + row["edge"]), rel=1e-3)
    assert row["orders"] == 4 and row["gold_per_day"] is not None

    # Your recipe: 2 cards + 1 shard + 4 Chaos make 1 orb. Gold = 2 x 1 + 1 x 2 (no order for Chaos),
    # and 0.5 for each Chaos the orb sells for.
    mine = [{"id": 1, "kind": "recipe", "inputs": [(CARD, 2), (OTHER, 1), (CHAOS, 4)], "output": ITEM, "gives": 1}]
    row = next(r for r in desk.conversions(fixed=mine, costs=costs) if r["how"] == "yours")
    assert row["gold"] == pytest.approx(4 + 0.5 * row["proceeds"], rel=1e-3) and row["orders"] == 3
    assert {s["id"] for s in row["gold_sources"]} == {CARD, OTHER, CHAOS}
