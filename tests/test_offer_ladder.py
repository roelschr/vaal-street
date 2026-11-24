"""The offer ladder: where a checked offer sits among the prices around it, dearest first.

Known answers for the test orb in the fixture's New league, at its last hour (see tests/test_check.py):
the Divine market is worth about 10.71 Chaos, fair value 10.52 (range 10.49 to 10.54), the Chaos
market 9.75, and a Divine 147.75 Chaos. With `flips(min_margin=0)` the plans in the Chaos market buy
at "19 Chaos for 2" (9.5, the flip that sells for Divines) and sell at "1 for 10 Chaos" (10.0); in the
Divine market both orders are "14 for 1 Divine" (10.55). So, dearest first:
- Chaos market:  Divine market, fair high, fair, fair low, plan sells (10.0), Chaos market, plan buys (9.5).
- Divine market: Divine market, plan sells, plan buys (10.55), fair high, fair, fair low, Chaos market.
Test Shard trades only in the Chaos market, so its Divine market has no row.
"""

from __future__ import annotations

import pytest

from conftest import NEW, call
from vaalstreet import check
from vaalstreet.desk import Desk

CHAOS_ROWS = ["divine_market", "fair_high", "fair", "fair_low", "plan_sell", "chaos_market", "plan_buy"]
DIVINE_ROWS = ["divine_market", "plan_sell", "plan_buy", "fair_high", "fair", "fair_low", "chaos_market"]


@pytest.fixture(scope="module")
def desk(archive_root):
    from vaalstreet.db import connect

    con = connect(archive_root)
    yield Desk.build(con, NEW, root=archive_root)
    con.close()


@pytest.fixture(scope="module")
def plans(desk):
    return desk.flips(min_margin=0.0)


def keys(rows: list[dict]) -> list[str]:
    return [row["key"] for row in rows]


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("market, items, currency, rows, at", [
    ("chaos", 1, 9, CHAOS_ROWS, 7),     # 9 Chaos each: cheaper than everything, the plan's 9.5 buy too
    ("chaos", 4, 39, CHAOS_ROWS, 5),    # 9.75 each: just over the Chaos market's 9.746, under the plan's 10 sell
    ("chaos", 1, 12, CHAOS_ROWS, 0),    # 12 each: dearer than the Divine market
    ("divine", 15, 1, DIVINE_ROWS, 6),  # 9.85 each: over the Chaos market only
    ("divine", 69, 5, DIVINE_ROWS, 1),  # 10.706 each: just under the Divine market's 10.708
    ("divine", 13, 1, DIVINE_ROWS, 0),  # 11.37 each: on top
])
def test_rows_are_sorted_with_the_offer_in_place(desk, plans, side, market, items, currency, rows, at):
    r = check.check(desk, "Test Orb", market, side, items, currency, plans=plans)
    ladder = r["ladder"]
    prices = [row["price"] for row in ladder]
    assert prices == sorted(prices, reverse=True)
    expected = rows[:at] + ["offer"] + rows[at:]
    assert keys(ladder) == expected
    offer = ladder[at]
    assert offer["offer"] and offer["price"] == r["price"]
    assert sum(row["offer"] for row in ladder) == 1
    own = r[f"{market}_market"]
    assert f"{market.title()} market's price" in offer["note"]
    assert ("under" if r["price"] < own else "over") in offer["note"]


def test_answer_carries_both_markets_and_both_plan_orders(desk, plans):
    r = check.check(desk, "Test Orb", "chaos", "buy", 1, 9, plans=plans)
    assert r["chaos_market"] == pytest.approx(9.75, rel=0.01)
    assert r["divine_market"] == pytest.approx(10.71, rel=0.01)
    assert r["plan_levels"]["buy"]["price"] == pytest.approx(9.5)
    assert r["plan_levels"]["sell"]["price"] == pytest.approx(10.0)
    plan_rows = {row["key"]: row for row in r["ladder"] if row["key"].startswith("plan_")}
    assert plan_rows["plan_buy"]["order"]["currency_units"] == 19 and plan_rows["plan_buy"]["order"]["item_units"] == 2


def test_a_quiet_market_is_left_out_not_shown_as_zero(desk, plans):
    r = check.check(desk, "Test Shard", "chaos", "buy", 1, 2, plans=plans)
    assert r["divine_market"] is None
    assert "divine_market" not in keys(r["ladder"])
    assert all(row["price"] > 0 for row in r["ladder"])
    # An offer in the quiet market itself still gets its row, with no price to compare it with.
    quiet = check.check(desk, "Test Shard", "divine", "buy", 100, 1, plans=plans)
    assert "divine_market" not in keys(quiet["ladder"])
    assert "barely trades" in next(row for row in quiet["ladder"] if row["offer"])["note"]


def test_missing_prices_have_no_row():
    r = {"market": "chaos", "price": 5.0, "chaos_market": None, "divine_market": 0, "fair": None,
         "plan_levels": {"buy": None, "sell": None}}
    assert keys(check.ladder(r)) == ["offer"]


def test_api(server):
    status, r = call(f"{server}/api/check", {"league": NEW, "item": "Test Orb", "market": "chaos", "side": "buy",
                                              "item_units": 1, "currency_units": 12})
    assert status == 200 and keys(r["ladder"])[0] == "offer"
