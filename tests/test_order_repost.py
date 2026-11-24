"""Re-posting an order: cancel the old one and record the new one in a single step."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import ITEM, NEW, OTHER, call
from vaalstreet.ledger import Ledger
from vaalstreet.paths import ledger_file

T0 = 1_700_000_000


def test_repost_cancels_the_old_order_and_links_the_new_one(tmp_path):
    """A buy of 30 orbs at 9 for 40 Chaos, 6 already filled, re-posted at 7 for 30 with the 24 still open:
    the old order is cancelled at the re-post's time, the new one buys the same orb in the same market,
    points back at the old one, and starts with nothing filled."""
    ledger = Ledger(tmp_path)
    old = ledger.add_order(NEW, ITEM, "buy", "chaos", 40, 9, 30, gold=300, t=T0)
    ledger.fill_order(old, 6, t=T0 + 100, record_trade=False)
    new = ledger.repost_order(old, 30, 7, 24, gold=240, t=T0 + 3600)
    was, now = ledger.order(old), ledger.order(new)
    assert (was["status"], was["updated"], was["filled"], was["gold"]) == ("cancelled", T0 + 3600, 6, 300)
    assert was["reposted_from"] is None
    assert (now["league"], now["item"], now["side"], now["market"]) == (NEW, ITEM, "buy", "chaos")
    assert (now["currency_units"], now["item_units"], now["units"], now["filled"]) == (30, 7, 24, 0)
    assert (now["status"], now["gold"], now["created"], now["reposted_from"]) == ("open", 240, T0 + 3600, old)
    assert [o["id"] for o in ledger.orders(NEW)] == [new], "the cancelled order leaves the open list"


def test_repost_takes_item_side_and_market_from_the_old_order(tmp_path):
    ledger = Ledger(tmp_path)
    old = ledger.add_order(NEW, OTHER, "sell", "divine", 1, 50, 100, t=T0)
    new = ledger.order(ledger.repost_order(old, 1, 55, 100, t=T0 + 10))
    assert (new["item"], new["side"], new["market"], new["gold"]) == (OTHER, "sell", "divine", None)


@pytest.mark.parametrize("status", ["filled", "closed", "cancelled"])
def test_only_an_order_on_the_exchange_can_be_reposted(tmp_path, status):
    ledger = Ledger(tmp_path)
    old = ledger.add_order(NEW, ITEM, "buy", "chaos", 40, 9, 9, t=T0)
    if status == "filled":
        ledger.fill_order(old, 9, t=T0 + 1, record_trade=False)
    else:
        ledger.set_order_status(old, status, t=T0 + 1)
    before = ledger.order(old)
    with pytest.raises(ValueError, match=f"That order is {status}"):
        ledger.repost_order(old, 30, 7, 9, t=T0 + 2)
    assert ledger.order(old) == before
    assert len(ledger.orders(NEW, include_done=True)) == 1, "no new order was written"
    with pytest.raises(ValueError, match="No such order"):
        ledger.repost_order(old + 99, 30, 7, 9)


@pytest.mark.parametrize("ratio", [(30, 7, 0), (0, 7, 24), (30, 0, 24), (30.5, 7, 24), (30, 7, -1), ("x", 7, 24)])
def test_a_refused_new_order_leaves_the_old_one_open(tmp_path, ratio):
    ledger = Ledger(tmp_path)
    old = ledger.add_order(NEW, ITEM, "buy", "chaos", 40, 9, 27, t=T0)
    before = ledger.order(old)
    with pytest.raises(ValueError):
        ledger.repost_order(old, *ratio, t=T0 + 5)
    assert ledger.order(old) == before and before["status"] == "open"
    assert len(ledger.orders(NEW, include_done=True)) == 1


def test_repost_over_the_api(server):
    status, _ = call(f"{server}/api/order", {"league": NEW, "item": ITEM, "side": "buy", "market": "chaos",
                                             "currency_units": 40, "item_units": 9, "units": 27, "gold": 270})
    assert status == 200
    [old] = call(f"{server}/api/ledger?league={NEW}")[1]["orders"]
    # The page sends the old order's item, side and market as well; the server takes them from the old order.
    status, body = call(f"{server}/api/order", {"league": NEW, "item": OTHER, "side": "sell", "market": "divine",
                                                "currency_units": 30, "item_units": 7, "units": 21, "gold": 210,
                                                "repost": old["id"]})
    assert status == 200 and body["ok"]
    [new] = call(f"{server}/api/ledger?league={NEW}")[1]["orders"]
    assert (new["item"], new["side"], new["market"], new["reposted_from"]) == (ITEM, "buy", "chaos", old["id"])
    assert (new["currency_units"], new["item_units"], new["units"], new["gold"]) == (30, 7, 21, 210)
    # A second re-post of the old, now cancelled, order is refused with a message the page can show.
    status, err = call(f"{server}/api/order", {"league": NEW, "currency_units": 30, "item_units": 7, "units": 21,
                                               "repost": old["id"]})
    assert status == 400 and "only an order still on the exchange can be re-posted" in err["error"]
    # A refused ratio keeps the new order open.
    status, err = call(f"{server}/api/order", {"league": NEW, "currency_units": 30, "item_units": 7, "units": 0,
                                               "repost": new["id"]})
    assert status == 400 and "positive" in err["error"]
    assert [o["id"] for o in call(f"{server}/api/ledger?league={NEW}")[1]["orders"]] == [new["id"]]


def test_a_ledger_from_before_reposts_opens_and_can_repost(tmp_path):
    """A desk.sqlite whose orders table was made before `reposted_from` existed, written with the old
    statement as it stood: it opens, gains the column, and its order can be re-posted."""
    path = ledger_file(tmp_path)
    old_file = sqlite3.connect(path)
    old_file.executescript("""
        CREATE TABLE orders(
            id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')), market TEXT NOT NULL CHECK(market IN ('chaos','divine')),
            currency_units INTEGER NOT NULL CHECK(currency_units > 0), item_units INTEGER NOT NULL CHECK(item_units > 0),
            units REAL NOT NULL CHECK(units > 0), filled REAL NOT NULL DEFAULT 0, gold REAL,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','part-filled','filled','cancelled','closed')),
            created INTEGER NOT NULL, updated INTEGER NOT NULL);
        INSERT INTO orders(league, item, side, market, currency_units, item_units, units, created, updated)
            VALUES ('New', 'Metadata/Items/Currency/CurrencyTestOrb', 'sell', 'chaos', 11, 1, 5, 1700000000, 1700000000);
    """)
    old_file.commit()
    old_file.close()
    ledger = Ledger(tmp_path)
    [kept] = ledger.orders(NEW)
    assert kept["reposted_from"] is None and kept["units"] == 5
    new = ledger.repost_order(kept["id"], 10, 1, 5, t=T0 + 60)
    assert ledger.order(new)["reposted_from"] == kept["id"] and ledger.order(kept["id"])["status"] == "cancelled"


def test_a_repost_in_a_rehearsal_stays_in_the_practice_ledger(archive_root, tmp_path):
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = real = Ledger(tmp_path)
    real_order = real.add_order(NEW, ITEM, "buy", "chaos", 40, 9, 27, t=T0)
    app.start_rehearsal({"league": NEW, "start_day": 1})
    try:
        practice = app.ledger
        app.api_post("/api/order", {"league": NEW, "item": ITEM, "side": "buy", "market": "chaos",
                                    "currency_units": 40, "item_units": 9, "units": 27})
        [mine] = practice.orders(NEW)
        app.api_post("/api/order", {"league": NEW, "currency_units": 30, "item_units": 7, "units": 21, "repost": mine["id"]})
        [new] = practice.orders(NEW)
        assert new["reposted_from"] == mine["id"] and new["created"] == app.rehearsal.hour + 3600 - 1
    finally:
        app.stop_rehearsal()
    # The real ledger still holds only its own order, open and untouched.
    [still] = Ledger(tmp_path).orders(NEW, include_done=True)
    assert still["id"] == real_order and still["status"] == "open" and still["reposted_from"] is None
