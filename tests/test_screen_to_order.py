"""Recording the order you are placing from a screen reading.

Two parts. `player_amounts` reads the amounts the player typed under "I Want" and "I Have", but only
when that is safe: the in-game layout of those amounts is untested, so any doubt gives nothing for
both. `posted_prefill` (each row of /api/readings carries it) chooses what the "Posted this order…"
form opens with: the player's own amounts, else the flip plan's order, else the market ratio read.

Known answers from the shared synthetic archive (tests/conftest.py, league New): the best Test Orb
plan buys in the Chaos market at 19 Chaos for 2 and sells in the Chaos market at 10 Chaos for 1. No
other item has a flip plan, so Test Ember falls through to the market ratio.
"""

from __future__ import annotations

import pytest

from conftest import ITEM, call
from vaalstreet import screen
from vaalstreet.screen import parse_screen, player_amounts
from vaalstreet.server import plan_orders, posted_prefill

NAMES = ["Orb of Alteration", "Orb of Augmentation", "Exalted Orb", "Divine Orb", "Test Orb", "Tier 2 Thing"]

AMOUNTS = [
    # (recognised lines, expected (want, have))
    # Each amount on its own line under its item.
    (["IWant", "OrbofAlteration", "640", "IHave", "DivineOrb", "10", "MarketRatio", "64:1"], (640, 10)),
    # Right after the item's name, with a thousands comma and an O read for 0.
    (["I Want", "Orb of Alteration 1,28O", "I Have", "Divine Orb 20", "Market Ratio", "64 : 1"], (1280, 20)),
    # Label, item and amount on one line; the stock ends the "I Have" region. (A short name with its
    # amount on one line, "I Have: Chaos Orb 420", is not read as an item at all, as before this change.)
    (["I Want: Exalted Orb 3", "I Have: Chaos Orb", "420", "Stock 12", "Market Ratio", "1:140"], (3, 420)),
    # Lines out of order: "I Have" first.
    (["IHave", "DivineOrb", "2", "IWant", "OrbofAlteration", "128", "MarketRatio", "64:1"], (128, 2)),
    # Missing on one side: nothing for both.
    (["IWant", "OrbofAlteration", "640", "IHave", "DivineOrb", "MarketRatio", "64:1"], (None, None)),
    # Missing on both sides: the windows the existing tests read.
    (["IWant", "OrbofAlteration", "IHave", "DivineOrb", "MarketRatio", "64:1"], (None, None)),
    # Two numbers under one label: which one is the amount? Nothing.
    (["IWant", "OrbofAlteration", "640", "12", "IHave", "DivineOrb", "10", "MarketRatio", "64:1"], (None, None)),
    # A lone letter that could be a misread 1 is not an amount.
    (["IWant", "OrbofAlteration", "l", "IHave", "DivineOrb", "10", "MarketRatio", "64:1"], (None, None)),
    # The last region has no label after it, so where it ends is unknown: nothing.
    (["MarketRatio", "64:1", "IWant", "OrbofAlteration", "640", "IHave", "DivineOrb", "10"], (None, None)),
    # A part of a unit, or zero, is not an amount you can type.
    (["IWant", "OrbofAlteration", "64.5", "IHave", "DivineOrb", "1", "MarketRatio", "64:1"], (None, None)),
    (["IWant", "OrbofAlteration", "0", "IHave", "DivineOrb", "1", "MarketRatio", "64:1"], (None, None)),
    # A number after text that is not the item's name.
    (["IWant", "OrbofAlteration", "Quantity 640", "IHave", "DivineOrb", "10", "MarketRatio", "64:1"], (None, None)),
]


@pytest.mark.parametrize("lines, expected", AMOUNTS)
def test_the_players_amounts_are_read_only_when_sure(lines, expected):
    r = parse_screen(lines, NAMES)
    assert (r.want_amount, r.have_amount) == expected
    # The reading itself does not change because of the amounts.
    assert r.describe().startswith(("buy", "sell")) and "640" not in r.describe()


def test_a_number_in_the_items_own_name_is_never_an_amount():
    lines = ["IWant", "Tier 2 Thing", "IHave", "Chaos Orb", "5", "MarketRatio", "1:5"]
    assert player_amounts(lines, NAMES, "Tier 2 Thing", "Chaos Orb") == (None, None)
    lines = ["IWant", "Tier 2 Thing 4", "IHave", "Chaos Orb 20", "MarketRatio", "1:5"]
    assert player_amounts(lines, NAMES, "Tier 2 Thing", "Chaos Orb") == (None, None)


def test_the_amounts_never_cost_the_reading(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(screen, "_side_amount", broken)
    r = parse_screen(["IWant", "OrbofAlteration", "640", "IHave", "DivineOrb", "10", "MarketRatio", "64:1"], NAMES)
    assert (r.item, r.item_units, r.currency_units, r.want_amount, r.have_amount) == (
        "Orb of Alteration", 64, 1, None, None)


READING = {"item": "Test Orb", "market": "chaos", "side": "buy", "item_units": 1.0, "currency_units": 9.0,
           "stock": None, "want_amount": None, "have_amount": None}


def test_prefill_prefers_the_players_amounts_reduced_to_lowest_terms():
    # Buying: 40 Test Orb wanted for 380 Chaos given is 19 Chaos for 2, 40 items in all.
    out = posted_prefill(READING | {"want_amount": 40, "have_amount": 380}, ITEM, "Test Orb",
                         {"currency_units": 10, "item_units": 1})
    assert (out["source"], out["currency_units"], out["item_units"], out["units"]) == ("amounts", 19, 2, 40)
    assert not out.get("editable") and out["title"] == "Buy order you posted"
    # Selling: the item is under "I Have".
    out = posted_prefill(READING | {"side": "sell", "want_amount": 30, "have_amount": 3}, ITEM, "Test Orb", None)
    assert (out["currency_units"], out["item_units"], out["units"], out["side"]) == (10, 1, 3, "sell")


def test_prefill_without_amounts_uses_the_plan_then_the_market_ratio():
    out = posted_prefill(READING, ITEM, "Test Orb", {"currency": "Chaos", "currency_units": 19, "item_units": 2})
    assert (out["source"], out["currency_units"], out["item_units"], out["editable"]) == ("plan", 19, 2, True)
    assert "units" not in out and "flip plan" in out["lead"] and "change" in out["lead"].casefold()
    out = posted_prefill(READING | {"market": "divine", "item_units": 64.0, "currency_units": 1.0}, ITEM, "Test Orb",
                         None)
    assert (out["source"], out["item_units"], out["currency_units"], out["currency"]) == ("market", 64, 1, "Divine")
    assert out["editable"] and "units" not in out and "market ratio" in out["lead"]
    # A ratio with a part of a unit cannot be posted: those fields are left to type.
    out = posted_prefill(READING | {"item_units": 2.5, "currency_units": 1.0}, ITEM, "Test Orb", None)
    assert out["item_units"] is None and out["currency_units"] == 1


def test_plan_orders_maps_each_plan_to_its_two_markets_best_first():
    plans = [{"id": "a", "kind": "cross", "buy_order": {"n": 1}, "sell_order": {"n": 2}},
             {"id": "a", "kind": "chaos", "buy_order": {"n": 3}, "sell_order": {"n": 4}}]
    assert plan_orders(plans) == {("a", "buy", "chaos"): {"n": 1}, ("a", "sell", "divine"): {"n": 2},
                                  ("a", "sell", "chaos"): {"n": 4}}


def _post(server, league, lines, reading):
    report = screen.reading_report(lines, None, None, league)
    report["reading"], report["said"] = reading, "x"
    assert call(f"{server}/api/reading", report)[0] == 200


def test_each_reading_carries_its_prefill_from_the_synthetic_archive(server):
    league = call(f"{server}/api/names")[1]["league"]
    # Newest first: the ember (no plan: market ratio), the orb sold (plan), the orb bought with amounts read.
    _post(server, league, ["l"], READING | {"want_amount": 40, "have_amount": 380})
    _post(server, league, ["l"], READING | {"side": "sell", "item_units": 1.0, "currency_units": 11.0})
    _post(server, league, ["l"], READING | {"item": "Test Ember", "item_units": 3.0, "currency_units": 2.0})
    _post(server, league, ["l"], None)  # not understood: no order
    status, out = call(f"{server}/api/readings")
    assert status == 200
    none, ember, sold, bought = (r["order"] for r in out["readings"])
    assert none is None
    assert (ember["source"], ember["item_units"], ember["currency_units"]) == ("market", 3, 2)
    assert (sold["source"], sold["currency_units"], sold["item_units"], sold["item"]) == ("plan", 10, 1, ITEM)
    assert (bought["source"], bought["currency_units"], bought["item_units"], bought["units"]) == ("amounts", 19, 2, 40)
    # A buy without amounts takes the plan's buy order: 19 Chaos for 2.
    _post(server, league, ["l"], READING)
    out = call(f"{server}/api/readings")[1]
    assert (out["readings"][0]["order"]["currency_units"], out["readings"][0]["order"]["item_units"]) == (19, 2)
    # Asked again with the newest id, the desk says only that nothing changed.
    again = call(f"{server}/api/readings?since={out['last']}")[1]
    assert again["readings"] is None and again["last"] == out["last"]
    # Only one of the two amounts is not kept: both or neither.
    _post(server, league, ["l"], READING | {"want_amount": 40})
    assert call(f"{server}/api/readings")[1]["readings"][0]["order"]["source"] == "plan"
    # Nothing was recorded by any of this.
    assert call(f"{server}/api/ledger")[1]["orders"] == []


def test_the_console_says_where_to_record_the_order():
    from test_screen_readings import GOOD, TellingDesk

    said = []
    screen.handle_press(lambda: "image", lambda image: GOOD, TellingDesk(), said.append)
    assert any("Posted this order" in s and "hideout" in s for s in said)
    said = []

    class Silent(TellingDesk):
        reading = None  # a desk that does not take readings has no button to point at

    screen.handle_press(lambda: "image", lambda image: GOOD, Silent(), said.append)
    assert not any("Posted this order" in s for s in said)


def test_a_ratio_the_ratio_rules_refuse_still_reaches_the_page():
    # Found while finishing this branch: "0:1" (a misread "O:1") is refused by the typed check's ratio
    # rules with a plain ValueError, not a ScreenError, and was not posted to the desk.
    from test_screen_readings import TellingDesk

    desk = TellingDesk()
    lines = ["IWant", "OrbofAlteration", "IHave", "DivineOrb", "MarketRatio", "0:1"]
    with pytest.raises(ValueError, match="more than zero"):
        screen.handle_press(lambda: "image", lambda image: lines, desk, lambda s: None)
    (report,) = desk.readings
    assert report["reading"] is None and "more than zero" in report["error"] and report["lines"] == lines
    assert desk.checks == [] and desk.events == []
