"""Baskets: a named list of items priced now and over time, against the synthetic archive's known prices.

Known answers in the fixture's New league at its last hour (see tests/conftest.py and the header of
tests/test_check.py): the test orb's fair value is about 10.52 Chaos, its Chaos market 9.75 and its
Divine market 10.71 (10% dearer), so a buyer buys it in the Chaos market. A test shard is 2 Chaos and
trades only for Chaos. A test ember is 4 Chaos in the Chaos market and 5% cheaper, 3.80, in the Divine
market; its fair value sits near 3.82, closer to the Divine market, which trades ten times the units.
The Test Lure first trades at league hour 5. The Divine costs 100 + 0.25 h Chaos at league hour h.
Test Scrap is in the item export but trades only in the separate scrap archive, so it has no price here.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import EMBER, H0, HOUR, ITEM, LURE, NEW, NEW_START, OTHER, SCRAP, call
from vaalstreet import basket
from vaalstreet.db import connect
from vaalstreet.desk import Desk
from vaalstreet.ingest import DIVINE
from vaalstreet.ledger import Ledger, _SCHEMA

KIT = [(ITEM, 10), (OTHER, 5), (EMBER, 4)]


@pytest.fixture(scope="module")
def desk(archive_root):
    con = connect(archive_root)
    yield Desk.build(con, NEW, root=archive_root)
    con.close()


def test_cost_now_at_fair_value_and_in_the_cheaper_market(desk):
    out = basket.cost(desk, KIT)
    # 10 x 10.52 + 5 x 2 + 4 x 3.82 = 130.4 at fair value.
    assert out["cost"] == pytest.approx(10 * desk.fair(ITEM) + 5 * desk.fair(OTHER) + 4 * desk.fair(EMBER), rel=1e-5)
    assert out["cost"] == pytest.approx(130.4, rel=0.01)
    # Bought where each is cheaper: orbs in the Chaos market, shards for Chaos, embers through Divines.
    # 10 x 9.75 + 5 x 2 + 4 x 3.80 = 122.7.
    assert out["cheaper"] == pytest.approx(10 * 9.75 + 5 * 2 + 4 * 3.8, rel=0.01)
    assert [r["market"] for r in out["items"]] == ["chaos", "chaos", "divine"]
    assert out["unpriced"] == [] and out["no_market"] == 0


def test_an_item_with_no_price_is_named_and_left_out(desk):
    out = basket.cost(desk, KIT + [(SCRAP, 2)])
    assert out["unpriced"] == ["Test Scrap"]
    scrap = out["items"][-1]
    assert scrap["value"] is None and "No price in New" in scrap["note"]
    # Never counted as zero silently: the totals are those of the three priced items, and say one is missing.
    assert out["cost"] == basket.cost(desk, KIT)["cost"]
    nothing = basket.cost(desk, [(SCRAP, 2)])
    assert nothing["cost"] is None and nothing["series"]["t"] == [] and nothing["since"] is None


def test_the_series_ends_at_the_cost_now_and_starts_when_it_is_complete(desk):
    out = basket.cost(desk, KIT + [(LURE, 7)])
    s = out["series"]
    assert s["cost"][-1] == out["cost"] and s["t"][-1] == desk.as_of
    # The Lure first trades at league hour 5: the hours before would be a sum without it, so they are left out.
    assert out["since"] == s["t"][0] == H0 + (NEW_START + 5) * HOUR
    assert len(s["t"]) == len(desk.panel.hours) - 5 == len(s["cost"]) == len(s["bridge"])


def test_changes_over_a_day_and_a_week(desk):
    # One Divine: 147.5 Chaos now (3-hour median at league hour 190) against 141.5 a day before, +4.2%.
    out = basket.cost(desk, [(DIVINE, 1)])
    assert out["change_24h"] == pytest.approx(147.5 / 141.5 - 1, abs=0.003)
    # New has 8 days on disk: the window a week back falls in the first day after launch, so no 7-day change.
    assert out["change_7d"] is None


@pytest.mark.parametrize("h", [30, 95, 150])
def test_the_series_does_not_look_ahead(desk, archive_root, h):
    """A desk as it stood at league hour h gives the same cost for every hour both desks have."""
    items = KIT + [(LURE, 7), (DIVINE, 1)]
    con = connect(archive_root, until=H0 + (NEW_START + h) * HOUR)
    try:
        early = basket.cost(Desk.build(con, NEW, root=archive_root), items)
    finally:
        con.close()
    full = basket.cost(desk, items)
    later = dict(zip(full["series"]["t"], full["series"]["cost"]))
    assert early["series"]["t"][-1] == H0 + (NEW_START + h) * HOUR
    assert len(early["series"]["t"]) == h + 1 - 5  # from the Lure's first hour to h
    assert all(later[t] == v for t, v in zip(early["series"]["t"], early["series"]["cost"]))
    assert early["cost"] == later[early["series"]["t"][-1]] and early["since"] == full["since"]


# ------------------------------------------------------------------ server
LIST = "10 Test Orb\nTest Shard: 5\n2 Test Scrap\n5 Nothing Orb\n4 Test Ember"


def test_preview_saves_nothing_then_save_and_delete(server):
    status, read = call(f"{server}/api/basket", {"league": NEW, "name": "Map run", "text": LIST, "preview": True})
    assert status == 200 and read["saved"] is None
    assert [r["name"] for r in read["lines"]] == ["Test Orb", "Test Shard", "Test Scrap", "Test Ember"]
    # Test Scrap trades in no league here but the item export knows it: kept, and named as having no price.
    assert read["unpriced"] == ["Test Scrap"] and read["lines"][2]["lines"] == [3]
    assert [u["line"] for u in read["unread"]] == [4]
    assert read["cost"] == pytest.approx(130.4, rel=0.01)
    assert call(f"{server}/api/baskets?league={NEW}")[1]["baskets"] == []

    status, saved = call(f"{server}/api/basket", {"league": NEW, "name": "Map run", "text": LIST, "note": "per map"})
    assert status == 200 and saved["saved"]
    [b] = call(f"{server}/api/baskets?league={NEW}")[1]["baskets"]
    assert b["name"] == "Map run" and b["note"] == "per map" and len(b["items"]) == 4
    assert b["cost"] == read["cost"] and b["series"]["cost"][-1] == b["cost"] and b["unpriced"] == ["Test Scrap"]

    assert call(f"{server}/api/basket", {"delete": b["id"]})[0] == 200
    assert call(f"{server}/api/baskets?league={NEW}")[1]["baskets"] == []


def test_a_basket_needs_a_name_and_a_line_that_reads(server):
    status, err = call(f"{server}/api/basket", {"league": NEW, "name": "", "text": "10 Test Orb"})
    assert status == 400 and "name" in err["error"]
    status, err = call(f"{server}/api/basket", {"league": NEW, "name": "Junk", "text": "5 Nothing Orb"})
    assert status == 400 and "no line" in err["error"]
    assert call(f"{server}/api/baskets?league={NEW}")[1]["baskets"] == []


def test_a_basket_saved_in_a_rehearsal_lands_in_the_real_ledger(archive_root, tmp_path):
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    real = app.ledger
    app.start_rehearsal({"league": NEW, "start_day": 2})
    assert app.ledger is not real
    app.api_basket({"league": NEW, "name": "Kit", "text": "10 Test Orb"})
    assert [b["name"] for b in real.baskets()] == ["Kit"]
    assert app.ledger.practice.baskets() == [], "nothing of it in the practice file"
    # Priced at the rehearsal's hour: the series ends there.
    [b] = app.api_baskets({"league": NEW})["baskets"]
    assert b["series"]["t"][-1] == app.rehearsal.hour
    app.stop_rehearsal()
    assert [b["name"] for b in Ledger(tmp_path).baskets()] == ["Kit"]


def test_a_ledger_from_before_baskets_opens_and_keeps_one(tmp_path):
    old = _SCHEMA.split("CREATE TABLE IF NOT EXISTS baskets")[0]
    db = sqlite3.connect(tmp_path / "desk.sqlite")
    db.executescript(old)
    db.execute("INSERT INTO trades(t, league, item, side, units, price) VALUES (1, 'L', 'orb', 'buy', 5, 2)")
    db.commit()
    db.close()
    ledger = Ledger(tmp_path)
    assert ledger.positions("L")["orb"]["units"] == 5, "existing trades survive"
    assert ledger.baskets() == []
    # Two lines for one item add up; a bad line is refused.
    ledger.add_basket("Run", [["orb", 3], ["card", 1], ["orb", 2]], t=100)
    with pytest.raises(ValueError):
        ledger.add_basket("Bad", [["orb", 0]])
    with pytest.raises(ValueError):
        ledger.add_basket(" ", [["orb", 1]])
    [b] = Ledger(tmp_path).baskets()
    assert b["name"] == "Run" and b["items"] == [("orb", 5.0), ("card", 1.0)] and b["t"] == 100
    ledger.delete_basket(b["id"])
    assert ledger.baskets() == []
