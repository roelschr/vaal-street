"""The ledger's Gold, orders, sessions and recipe checks, and its additive migration."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from vaalstreet.ledger import Ledger, recipe_key
from vaalstreet.paths import ledger_file

# The schema as it shipped before Gold, orders, sessions and recipe checks; copied verbatim.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, item TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')), units REAL NOT NULL CHECK(units > 0),
    price REAL NOT NULL CHECK(price >= 0), note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS trades_league ON trades(league, t, id);
CREATE TABLE IF NOT EXISTS watchlist(league TEXT NOT NULL, item TEXT NOT NULL, PRIMARY KEY(league, item));
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
    field TEXT NOT NULL, op TEXT NOT NULL CHECK(op IN ('above','below')), threshold REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, hot INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_old_ledger_file_keeps_its_rows_and_gains_the_new_features(tmp_path):
    db = sqlite3.connect(ledger_file(tmp_path))
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO trades(t, league, item, side, units, price, note) VALUES (1,'L','orb','buy',10,2,'old')")
    db.execute("INSERT INTO trades(t, league, item, side, units, price, note) VALUES (2,'L','orb','sell',4,3,'')")
    db.execute("INSERT INTO watchlist VALUES ('L','orb')")
    db.execute("INSERT INTO alerts(league, item, field, op, threshold) VALUES ('L','orb','price','above',5)")
    db.execute("INSERT INTO settings VALUES ('league', '\"L\"')")
    db.commit()
    db.close()

    Ledger(tmp_path)  # opening twice proves the migration can run again safely
    ledger = Ledger(tmp_path)
    trades = ledger.trades("L")
    assert [t["note"] for t in trades] == ["old", ""] and all(t["gold"] is None for t in trades)
    pos = ledger.positions("L")["orb"]
    assert pos["units"] == 6 and pos["realized"] == pytest.approx(4)
    assert ledger.watchlist("L") == ["orb"] and len(ledger.alerts("L")) == 1 and ledger.get("league") == "L"
    assert ledger.gold_fees("L") == {}, "old trades carry no Gold, so the fee stays unknown"

    ledger.add_trade("L", "orb", "buy", 5, 2, gold=50)
    order = ledger.add_order("L", "orb", "sell", "chaos", 3, 1, 5)
    ledger.fill_order(order, 5)
    session = ledger.start_session("L", 10)
    ledger.check_recipe(recipe_key("set", ["a", "b"]), "confirmed")
    assert ledger.gold_fees("L")["orb"]["gold_per_unit"] == 10
    assert ledger.sessions("L")[0]["id"] == session and ledger.recipe_checks()


def test_gold_fees_are_learned_from_records_and_never_counted_twice(tmp_path):
    ledger = Ledger(tmp_path)
    assert ledger.gold_fees("L") == {}
    ledger.add_trade("L", "orb", "buy", 10, 1, gold=100)  # 10 Gold per unit
    ledger.add_trade("L", "orb", "buy", 10, 1, gold=120)  # 12
    ledger.add_trade("L", "orb", "buy", 10, 1, gold=5000)  # a typo: the median shrugs it off
    ledger.add_trade("L", "orb", "buy", 10, 1)  # no Gold given: unknown, not zero
    ledger.add_trade("L", "gem", "buy", 1, 1, gold=0)  # a real zero is kept
    order = ledger.add_order("L", "orb", "sell", "chaos", 2, 1, 10, gold=110)  # 11 per unit
    ledger.fill_order(order, 10)
    fees = ledger.gold_fees("L")
    # Samples 10, 12, 500 and the order's 11: the fill's trade carries no Gold of its own.
    assert fees["orb"] == {"gold_per_unit": pytest.approx(11.5), "trades": 4}
    assert fees["gem"]["gold_per_unit"] == 0 and "chaos" not in fees
    assert ledger.trades("L")[-1]["gold"] is None and ledger.trades("L")[-1]["order_id"] == order
    with pytest.raises(ValueError):
        ledger.add_trade("L", "orb", "buy", 1, 1, gold=-1)


def test_order_fills_create_trades_at_the_right_price(tmp_path):
    ledger = Ledger(tmp_path)
    buy = ledger.add_order("L", "orb", "buy", "divine", 1, 64, 128, t=10)  # 64 orbs for 1 Divine
    with pytest.raises(ValueError):
        ledger.fill_order(buy, 64)  # a Divine fill needs the Divine price
    ledger.fill_order(buy, 64, bridge=200, t=11)
    assert ledger.order(buy)["status"] == "part-filled"
    trade = ledger.trades("L")[-1]
    assert trade["price"] == pytest.approx(200 / 64) and trade["units"] == 64
    with pytest.raises(ValueError):
        ledger.fill_order(buy, 65, bridge=200)  # more than is still open
    ledger.fill_order(buy, 64, bridge=200, t=12)
    assert ledger.order(buy)["status"] == "filled" and ledger.positions("L")["orb"]["units"] == 128

    sell = ledger.add_order("L", "orb", "sell", "chaos", 4, 1, 500)
    with pytest.raises(ValueError):
        ledger.fill_order(sell, 501)  # more than the order holds
    assert ledger.order(sell)["status"] == "open" and ledger.order(sell)["filled"] == 0, "a refused fill changes nothing"
    # The ledger holds 128 at 200/64 = 3.125 each. A fill of 200 sold them all, so 72 came from somewhere the
    # ledger never saw: they cost nothing. Known answer: 128 × (4 − 3.125) + 72 × 4 = 112 + 288 = 400.
    fill = ledger.fill_order(sell, 200, t=13)
    assert ledger.uncovered("L") == {("trade", fill): pytest.approx(72)}
    assert ledger.positions("L")["orb"]["realized"] == pytest.approx(400)
    ledger.fill_order(sell, 10, record_trade=False)
    assert len(ledger.trades("L")) == 3
    ledger.set_order_status(sell, "cancelled")
    assert [o["id"] for o in ledger.orders("L")] == [buy]
    with pytest.raises(ValueError):
        ledger.add_order("L", "orb", "buy", "chaos", 1.5, 1, 1)  # the exchange takes whole numbers only
    with pytest.raises(ValueError):
        ledger.set_order_status(buy, "filled")



def test_a_trade_on_the_wrong_side_can_be_corrected(tmp_path):
    ledger = Ledger(tmp_path)
    buy = ledger.add_trade("L", "orb", "buy", 10, 2, t=1)
    wrong = ledger.add_trade("L", "orb", "buy", 4, 3, note="meant to sell", gold=8, t=2)
    assert ledger.positions("L")["orb"]["units"] == 14
    ledger.edit_trade(wrong, side="sell")
    orb = ledger.positions("L")["orb"]
    # 10 bought at 2, 4 sold at 3: 6 left at cost 12, and 4 x (3 - 2) realised.
    assert orb["units"] == 6 and orb["cost"] == pytest.approx(12) and orb["realized"] == pytest.approx(4)
    edited = ledger.trades("L")[-1]
    assert (edited["t"], edited["note"], edited["gold"]) == (2, "meant to sell", 8), "only what was changed changes"

    ledger.edit_trade(wrong, units=5, price=2.5, gold=None, note="")
    edited = ledger.trades("L")[-1]
    assert (edited["units"], edited["price"], edited["gold"], edited["note"]) == (5, 2.5, None, "")
    ledger.edit_trade(wrong, item="gem", side="buy")
    assert ledger.positions("L")["gem"]["units"] == 5 and ledger.positions("L")["orb"]["units"] == 10


def test_an_edit_that_would_oversell_history_is_refused(tmp_path):
    ledger = Ledger(tmp_path)
    buy = ledger.add_trade("L", "orb", "buy", 10, 2, t=1)
    sale = ledger.add_trade("L", "orb", "sell", 8, 3, t=2)
    ledger.add_trade("L", "orb", "buy", 50, 2, t=3)  # holdings today would cover the sale; history does not
    for change in ({"side": "sell"}, {"units": 5}, {"item": "gem"}):
        with pytest.raises(ValueError, match="more than the ledger held"):
            ledger.edit_trade(buy, **change)
    assert [(t["item"], t["side"], t["units"]) for t in ledger.trades("L")][:1] == [("orb", "buy", 10)], "a refused edit changes nothing"
    with pytest.raises(ValueError):
        ledger.edit_trade(sale, units=0)
    with pytest.raises(ValueError):
        ledger.edit_trade(sale, t=5)  # the time is not editable
    with pytest.raises(ValueError):
        ledger.edit_trade(999, price=1)

    # A file that is already oversold (older versions let you delete a buy that a sale needed) does not
    # block an unrelated correction, nor an unrelated delete.
    with pytest.raises(ValueError, match="Deleting this would leave a sale selling more than the ledger held"):
        ledger.delete_trade(buy)
    assert ledger.trades("L")[0]["id"] == buy, "a refused delete changes nothing"
    ledger.close()
    db = sqlite3.connect(ledger_file(tmp_path))
    db.execute("DELETE FROM trades WHERE id = ?", (buy,))
    db.commit()
    db.close()
    ledger = Ledger(tmp_path)
    ledger.edit_trade(sale, note="typo fixed", price=3.5)
    assert ledger.trades("L")[0]["note"] == "typo fixed"
    spare = ledger.add_trade("L", "gem", "buy", 1, 1, t=4)
    ledger.delete_trade(spare)
    ledger.delete_trade(999)  # no such trade: nothing to do
    assert [t["id"] for t in ledger.trades("L")] == [sale, sale + 1]


def test_an_item_you_made_carries_what_its_inputs_cost(tmp_path):
    """Known answer: 3 small essences bought at 10 each merge into 1 big one that cost 30; sold at 40 it realises 10."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 3, 10, t=100)
    for bad in ([], [["small", 0]], [["small", -1]], [["", 1]], [["big", 1]]):
        with pytest.raises(ValueError):
            ledger.add_made("L", bad, "big")
    assert ledger.made("L") == [], "a refused entry leaves nothing behind"

    session = ledger.start_session("L", t=150)
    made = ledger.add_made("L", [["small", 3]], "big", note="vendor", t=200)
    positions = ledger.positions("L")
    assert positions["small"]["units"] == 0 and positions["small"]["cost"] == pytest.approx(0)
    assert positions["big"]["units"] == 1 and positions["big"]["cost"] == pytest.approx(30)
    assert positions["big"]["realized"] == 0 and positions["small"]["realized"] == 0, "making an item realises nothing"
    ledger.add_trade("L", "big", "sell", 1, 40, t=300)
    assert ledger.positions("L")["big"]["realized"] == pytest.approx(10)
    ledger.stop_session(session, t=400)
    assert ledger.sessions("L", now=500)[0]["profit"] == pytest.approx(10)
    [entry] = ledger.made("L")
    assert (entry["id"], entry["inputs"], entry["output"], entry["gives"], entry["note"]) == (made, [("small", 3.0)], "big", 1, "vendor")
    assert ledger.made("Other") == [] and Ledger(tmp_path).made("L")[0]["id"] == made, "per league, and survives a restart"


def test_a_delete_may_not_leave_later_entries_short(tmp_path):
    """Known answer: 3 at 10 make 1 that cost 30, sold at 40 for 10 realised. Deleting the made entry or the
    buy under that sale would put units back, or take them away, while the sale's profit stays."""
    ledger = Ledger(tmp_path)
    buy = ledger.add_trade("L", "small", "buy", 3, 10, t=1)
    made = ledger.add_made("L", [["small", 3]], "big", t=2)
    sale = ledger.add_trade("L", "big", "sell", 1, 40, t=3)
    with pytest.raises(ValueError, match="Deleting this would leave a sale selling more than the ledger held"):
        ledger.delete_made(made)
    with pytest.raises(ValueError, match="Deleting this would leave an item you made using more than the ledger held"):
        ledger.delete_trade(buy)
    positions = ledger.positions("L")
    assert positions["small"]["units"] == 0 and positions["big"]["realized"] == pytest.approx(10), "a refused delete changes nothing"
    assert len(ledger.trades("L")) == 2 and len(ledger.made("L")) == 1
    # Newest first, each delete is fine: the sale, then the made entry, then the buy.
    ledger.delete_trade(sale)
    ledger.delete_made(made)
    assert ledger.made("L") == [] and ledger.positions("L")["small"]["units"] == 3
    ledger.delete_made(made)  # already gone: nothing to do
    ledger.delete_trade(buy)
    assert ledger.trades("L") == []


def test_a_made_item_is_sold_once_even_after_a_trade_was_deleted(tmp_path):
    """SQLite gives the id of a deleted newest trade to the next trade. With one time for every record, as in
    a rehearsal, that id used to sort the sale before the entry that made the item: the sale realised its
    whole 40, the item stayed in the holdings, and a second sale realised 10 more. Known answer: one sale, 10."""
    ledger = Ledger(tmp_path, clock=lambda: 5000)
    ledger.add_trade("L", "small", "buy", 3, 10)
    spare = ledger.add_trade("L", "dust", "buy", 1, 1)
    ledger.add_made("L", [["small", 3]], "big")  # remembers `spare` as the newest trade
    ledger.delete_trade(spare)
    sale = ledger.add_trade("L", "big", "sell", 1, 40)
    assert sale > spare, "the id a made entry remembers is not handed out again"
    assert ledger.uncovered("L") == {}, "the sale sold the big one that was made"
    big = ledger.positions("L")["big"]
    assert big["units"] == 0 and big["realized"] == pytest.approx(10)
    # A second sale finds nothing left: it is a big one from elsewhere, at no cost, not the made one sold twice.
    extra = ledger.add_trade("L", "big", "sell", 1, 40)
    assert ledger.uncovered("L") == {("trade", extra): pytest.approx(1)}
    # Deleting the newest trade and recording again keeps counting up when nothing remembers the id.
    ledger.delete_trade(extra)
    assert ledger.add_trade("L", "big", "sell", 1, 40) == extra


def test_made_items_keep_their_order_after_a_trade_was_deleted(tmp_path):
    """The same id reuse, between two made entries: the second must still come after the first, although
    the newest trade the first one remembers is gone. Known answer: 3 at 10 make 1 big, which makes 1 huge
    that cost 30."""
    ledger = Ledger(tmp_path, clock=lambda: 5000)
    ledger.add_trade("L", "small", "buy", 3, 10)
    spare = ledger.add_trade("L", "dust", "buy", 1, 1)
    ledger.add_made("L", [["small", 3]], "big")
    ledger.delete_trade(spare)
    ledger.add_made("L", [["big", 1]], "huge")
    assert [m["output"] for m in ledger.made("L")] == ["big", "huge"]
    positions = ledger.positions("L")
    assert positions["huge"]["units"] == 1 and positions["huge"]["cost"] == pytest.approx(30) and positions["big"]["units"] == 0
    ledger.add_trade("L", "huge", "sell", 1, 45)
    assert ledger.positions("L")["huge"]["realized"] == pytest.approx(15)


def test_a_back_dated_sale_may_not_take_what_a_later_sale_sold(tmp_path):
    """A sale is counted against the holdings at its own time, not today's. Known answers: a sale before the
    buy holds nothing, so all 5 come at no cost; one between the buy and a later sale of those 5 is refused,
    because the later sale would then sell units it had already lost."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "orb", "buy", 5, 2, t=100)
    early = ledger.add_trade("L", "orb", "sell", 5, 3, t=50)  # today's holdings would cover it; the history does not
    assert ledger.uncovered("L") == {("trade", early): pytest.approx(5)}
    ledger.add_trade("L", "orb", "sell", 5, 3, t=150)
    with pytest.raises(ValueError, match="A sale at that time would leave a sale selling more than the ledger held"):
        ledger.add_trade("L", "orb", "sell", 5, 3, t=120)
    assert len(ledger.trades("L")) == 3


def test_sales_that_arrive_together_cannot_merge_the_same_items(tmp_path):
    """Eight sales of one big essence, released at once, with 3 small ones held: exactly one sale merges them.
    Planning the merge, writing it and the sale are one step under the ledger's lock. Known answer: one merge
    of 3 small into 1 big, one sale covered by it, and seven sales of 1 at no cost."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 3, 2, t=1)
    gate, sold, errors = threading.Barrier(8), [], []

    def sell():
        gate.wait()
        try:
            sold.append(ledger.add_trade("L", "big", "sell", 1, 9, recipes=ESSENCES))
        except Exception as err:  # noqa: BLE001 - any failure is the test's answer
            errors.append(err)

    threads = [threading.Thread(target=sell) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(sold) == 8 and not errors
    assert len(ledger.made("L")) == 1 and ledger.positions("L")["small"]["units"] == 0
    assert sorted(ledger.uncovered("L").values()) == [pytest.approx(1)] * 7


def test_made_items_chain_and_take_several_inputs(tmp_path):
    """Known answers: 9 at 10 make 3 that cost 30 each, which make 1 that cost 90. Two of that one (180)
    and 4 dust bought at 5 (20) make 5 of a third item that cost 200 together, 40 each."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "tier1", "buy", 18, 10, t=1)
    ledger.add_made("L", [["tier1", 18]], "tier2", gives=6, t=2)
    ledger.add_made("L", [["tier2", 6]], "tier3", gives=2, t=3)
    assert ledger.positions("L")["tier3"]["cost"] == pytest.approx(180)
    ledger.add_trade("L", "dust", "buy", 8, 5, t=4)
    ledger.add_made("L", [["tier3", 2], ["dust", 4]], "relic", gives=5, t=5)
    positions = ledger.positions("L")
    assert positions["relic"]["units"] == 5 and positions["relic"]["cost"] == pytest.approx(200)
    assert positions["dust"]["units"] == 4 and positions["dust"]["cost"] == pytest.approx(20), "the dust not used keeps its cost"
    ledger.add_trade("L", "relic", "sell", 2, 50, t=6)
    assert ledger.positions("L")["relic"]["realized"] == pytest.approx(2 * (50 - 40))


def test_an_edit_may_not_leave_a_made_item_short_of_its_inputs(tmp_path):
    ledger = Ledger(tmp_path)
    buy = ledger.add_trade("L", "small", "buy", 3, 10, t=1)
    ledger.add_made("L", [["small", 3]], "big", t=2)
    sale = ledger.add_trade("L", "big", "sell", 1, 40, t=3)
    for change in ({"units": 2}, {"side": "sell"}, {"item": "other"}):
        with pytest.raises(ValueError, match="an item you made using more than the ledger held"):
            ledger.edit_trade(buy, **change)
    assert ledger.trades("L")[0]["units"] == 3, "a refused edit changes nothing"
    ledger.edit_trade(sale, units=2)  # only one was made: the other came from elsewhere, as for a new sale
    assert ledger.uncovered("L") == {("trade", sale): pytest.approx(1)}
    ledger.edit_trade(sale, units=1)
    ledger.edit_trade(buy, price=12)  # a plain correction still goes through, and the cost follows it
    ledger.edit_trade(sale, price=41)
    assert ledger.positions("L")["big"]["realized"] == pytest.approx(41 - 36)
    # Recorded for a time in the past, an entry may not take what a later sale already sold.
    ledger.add_trade("L", "ore", "buy", 3, 1, t=10)
    ledger.add_trade("L", "ore", "sell", 3, 2, t=20)
    with pytest.raises(ValueError, match="leave a later sale or item you made short"):
        ledger.add_made("L", [["ore", 3]], "bar", t=15)
    assert [m["output"] for m in ledger.made("L")] == ["big"]


def test_records_of_the_same_moment_keep_the_order_they_were_made_in(tmp_path):
    """A rehearsal stamps everything in one practice hour with one time. Buy, merge, sell and then the
    same again must replay in that order: each sale realises 40 - 30 = 10, and nothing counts as short."""
    ledger = Ledger(tmp_path, clock=lambda: 5000)
    for _ in range(2):
        ledger.add_trade("L", "small", "buy", 3, 10)
        ledger.add_made("L", [["small", 3]], "big")
        ledger.add_trade("L", "big", "sell", 1, 40)
    assert {tr["t"] for tr in ledger.trades("L")} == {5000} and {m["t"] for m in ledger.made("L")} == {5000}
    positions = ledger.positions("L")
    assert positions["big"]["realized"] == pytest.approx(20) and positions["big"]["units"] == 0
    assert positions["small"]["units"] == 0 and positions["small"]["cost"] == pytest.approx(0)
    ledger.edit_trade(ledger.trades("L")[-1]["id"], note="checked")  # the history holds no short entry to trip on
    with pytest.raises(ValueError, match="more than the ledger held"):
        ledger.edit_trade(ledger.trades("L")[0]["id"], units=2)


def test_a_trade_from_an_order_fill_keeps_the_orders_side_and_units(tmp_path):
    ledger = Ledger(tmp_path)
    order = ledger.add_order("L", "orb", "buy", "chaos", 2, 1, 10, gold=50)
    fill = ledger.fill_order(order, 10)
    for change in ({"side": "sell"}, {"units": 4}, {"item": "gem"}):
        with pytest.raises(ValueError, match="order fill"):
            ledger.edit_trade(fill, **change)
    ledger.edit_trade(fill, price=1.9, note="rounded in game", gold=99)
    trade = ledger.trades("L")[0]
    assert trade["price"] == 1.9 and trade["note"] == "rounded in game"
    assert trade["gold"] is None and ledger.gold_fees("L")["orb"]["gold_per_unit"] == 5, "the order keeps the Gold"

def test_order_advice(tmp_path):
    ledger = Ledger(tmp_path)
    bridge = 100.0
    # Orb: combined fair value 10 c, Chaos market 9.8 c, Divine market 10.6 c (the Divine market is dearer).
    prices = {"orb": {"fair": 10.0, "chaos": 9.8, "divine": 10.6}, "gem": {"fair": None, "chaos": None, "divine": None}}
    plans = {"orb": {"buy": {"chaos": {"currency_units": 19, "item_units": 2, "price": 9.5}},
                     "sell": {"divine": {"currency_units": 1, "item_units": 9, "price": bridge / 9}}}}
    keep = ledger.add_order("L", "orb", "buy", "chaos", 19, 2, 10)  # exactly the plan's buy order
    plan_sell = ledger.add_order("L", "orb", "sell", "divine", 1, 9, 9)  # 11.1 c: above fair, as planned
    stale = ledger.add_order("L", "orb", "buy", "chaos", 8, 1, 10)  # 8 c: far under the plan's 9.5
    wrong = ledger.add_order("L", "orb", "buy", "chaos", 11, 1, 10)  # paying 11 c in a 9.8 c market
    cheap_sell = ledger.add_order("L", "orb", "sell", "chaos", 9, 1, 10)  # asking 9 c in a 9.8 c market
    unknown = ledger.add_order("L", "gem", "buy", "chaos", 1, 1, 1)
    done = ledger.add_order("L", "orb", "buy", "chaos", 9, 1, 1)
    ledger.fill_order(done, 1)
    gone = ledger.add_order("L", "orb", "buy", "chaos", 9, 1, 1)
    ledger.set_order_status(gone, "cancelled")

    advice = {o["id"]: o for o in ledger.advise_orders("L", prices, plans, bridge)}
    assert gone not in advice
    assert advice[keep]["advice"] == "keep" and advice[plan_sell]["advice"] == "keep"
    assert advice[stale]["advice"] == "re-price" and advice[stale]["suggest"]["currency_units"] == 19
    assert advice[wrong]["advice"] == "cancel" and advice[cheap_sell]["advice"] == "cancel"
    assert advice[unknown]["advice"] == "keep" and "no recent price" in advice[unknown]["reason"]
    assert advice[done]["advice"] == "collect"
    assert all(o["reason"] for o in advice.values())
    ledger.set_order_status(done, "closed")
    assert done not in {o["id"] for o in ledger.advise_orders("L", prices, plans, bridge)}
    assert "Gold unknown" in advice[stale]["reason"], "no fee on record: re-price, and say the Gold is unknown"


def test_order_advice_weighs_the_gold_a_repost_costs(tmp_path):
    """Gold is not refunded on cancel (confirmed in game). Known answer at 3 Gold a unit: a 10-unit buy
    at 9 c against the plan's 9.5 c is 5% off; re-posting costs 30 Gold again, so keep it. At 8 c it is
    16% off: re-price. On the wrong side of the market, cancel whatever the Gold."""
    ledger = Ledger(tmp_path)
    ledger.set("assumed_gold_fee", 3)
    prices = {"orb": {"fair": 10.0, "chaos": 9.8, "divine": 10.6}}
    plans = {"orb": {"buy": {"chaos": {"currency_units": 19, "item_units": 2, "price": 9.5}}}}
    near = ledger.add_order("L", "orb", "buy", "chaos", 9, 1, 10)
    far = ledger.add_order("L", "orb", "buy", "chaos", 8, 1, 10)
    wrong = ledger.add_order("L", "orb", "buy", "chaos", 11, 1, 10)
    advice = {o["id"]: o for o in ledger.advise_orders("L", prices, plans, 100.0)}
    assert advice[near]["advice"] == "keep" and advice[near]["repost_gold"] == 30
    assert "costs about 30 Gold again" in advice[near]["reason"] and "5% off" in advice[near]["reason"]
    assert advice[far]["advice"] == "re-price" and "30 Gold" in advice[far]["reason"]
    assert advice[wrong]["advice"] == "cancel" and "not refunded" in advice[wrong]["reason"]


def test_session_figures(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "orb", "buy", 10, 2.0, t=100, gold=30)  # bought before the session
    session = ledger.start_session("L", expected_per_hour=5, plan={"flips": ["orb"]}, t=1000)
    with pytest.raises(ValueError):
        ledger.start_session("L", t=1001)  # one running session per league
    ledger.add_trade("L", "orb", "sell", 10, 3.0, t=2000, gold=20)  # realises 10 against the cost from before
    ledger.add_order("L", "orb", "buy", "chaos", 2, 1, 5, gold=15, t=2500)
    ledger.add_trade("L", "orb", "buy", 1, 2.0, t=9000)  # after the session: not counted

    live = ledger.sessions("L", now=1000 + 1800)[0]
    assert live["running"] and live["hours"] == 0.5 and live["profit"] == pytest.approx(10)
    assert live["per_hour"] == pytest.approx(20) and live["gold_spent"] == 35
    ledger.stop_session(session, gold_earned=400, t=1000 + 7200)
    done = ledger.sessions("L", now=99999)[0]
    assert not done["running"] and done["hours"] == 2 and done["per_hour"] == pytest.approx(5)
    assert done["gold_spent"] == 35 and done["gold_earned"] == 400
    assert done["expected_per_hour"] == 5 and done["plan"] == {"flips": ["orb"]}
    empty = ledger.start_session("L", t=5000)
    assert ledger.sessions("L", now=5000)[0]["per_hour"] is None, "no time has passed yet"
    assert ledger.sessions("Other") == [] and ledger.running_session("L")["id"] == empty


def test_recipe_checks_are_shared_across_leagues_and_keep_the_latest(tmp_path):
    ledger = Ledger(tmp_path)
    key = recipe_key("set", ["shard", "orb"])
    assert key == "set:shard>orb" and recipe_key("loop", ["shard", "orb"]) != key
    ledger.check_recipe(key, "failed", "wrong count", t=1)
    ledger.check_recipe(key, "confirmed", "20 for 1", t=2)
    checks = ledger.recipe_checks()
    assert checks[key]["status"] == "confirmed" and checks[key]["note"] == "20 for 1"
    with pytest.raises(ValueError):
        ledger.check_recipe(key, "maybe")


# The schema as it shipped with Gold, orders, sessions and recipe checks (wave 1), with the
# columns `_migrate` added to trades; copied verbatim, before your own conversions existed.
WAVE1_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, item TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')), units REAL NOT NULL CHECK(units > 0),
    price REAL NOT NULL CHECK(price >= 0), note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS trades_league ON trades(league, t, id);
CREATE TABLE IF NOT EXISTS watchlist(league TEXT NOT NULL, item TEXT NOT NULL, PRIMARY KEY(league, item));
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
    field TEXT NOT NULL, op TEXT NOT NULL CHECK(op IN ('above','below')), threshold REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, hot INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')), market TEXT NOT NULL CHECK(market IN ('chaos','divine')),
    currency_units INTEGER NOT NULL CHECK(currency_units > 0), item_units INTEGER NOT NULL CHECK(item_units > 0),
    units REAL NOT NULL CHECK(units > 0), filled REAL NOT NULL DEFAULT 0, gold REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','part-filled','filled','cancelled','closed')),
    created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS orders_league ON orders(league, status, id);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, started INTEGER NOT NULL, stopped INTEGER,
    expected_per_hour REAL, plan TEXT NOT NULL DEFAULT '{}', gold_earned REAL);
CREATE TABLE IF NOT EXISTS recipe_checks(
    id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('confirmed','failed')),
    note TEXT NOT NULL DEFAULT '', t INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS recipe_checks_key ON recipe_checks(key, t, id);
ALTER TABLE trades ADD COLUMN gold REAL;
ALTER TABLE trades ADD COLUMN order_id INTEGER;
"""


def test_wave1_ledger_file_keeps_its_rows_and_gains_your_conversions(tmp_path):
    db = sqlite3.connect(ledger_file(tmp_path))
    db.executescript(WAVE1_SCHEMA)
    db.execute("INSERT INTO trades(t, league, item, side, units, price, gold) VALUES (1,'L','orb','buy',10,2,30)")
    db.execute("INSERT INTO recipe_checks(key, status, note, t) VALUES ('set:a>b','confirmed','',1)")
    db.commit()
    db.close()

    Ledger(tmp_path)
    ledger = Ledger(tmp_path)
    assert ledger.trades("L")[0]["gold"] == 30 and ledger.recipe_checks()["set:a>b"]["status"] == "confirmed"
    assert ledger.fixed_conversions() == []
    assert ledger.made("L") == [], "an older file gains the table for items you made"
    ledger.add_made("L", [["orb", 3]], "big orb")
    assert ledger.positions("L")["big orb"]["cost"] == pytest.approx(6) and ledger.positions("L")["orb"]["units"] == 7
    ledger.add_fixed("vendor", [["chaos", 5]], "orb", note="town vendor")
    assert ledger.fixed_conversions()[0]["key"] == "vendor:5*chaos>1*orb"


def test_your_recipes_and_vendor_prices_are_kept_for_every_league(tmp_path):
    ledger = Ledger(tmp_path)
    recipe = ledger.add_fixed("recipe", [["a", 3], ["b", 1]], "c", 2, "made up")
    ledger.add_fixed("vendor", [["chaos", 5]], "orb")
    rows = ledger.fixed_conversions()
    assert [r["kind"] for r in rows] == ["recipe", "vendor"]
    assert rows[0]["inputs"] == [("a", 3.0), ("b", 1.0)] and rows[0]["gives"] == 2 and rows[0]["note"] == "made up"
    assert rows[0]["key"] == "recipe:3*a+1*b>2*c"
    # Correcting a count gives a new key, so the corrected recipe starts unchecked.
    assert rows[0]["key"] != recipe_key("set", ["a", "c"])
    ledger.delete_fixed(recipe)
    assert [r["kind"] for r in ledger.fixed_conversions()] == ["vendor"]
    for bad in (("trade", [["a", 1]], "c"), ("recipe", [], "c"), ("recipe", [["a", 0]], "c"),
                ("vendor", [["a", 1], ["b", 1]], "c"), ("recipe", [["c", 1]], "c")):
        with pytest.raises(ValueError):
            ledger.add_fixed(*bad)


def test_a_recipe_key_reads_as_words():
    from vaalstreet.ledger import describe_key, fixed_key, recipe_key

    name = {"a": "Some Card", "b": "Some Shard", "c": "Some Orb"}.get
    assert describe_key(fixed_key("recipe", [("a", 3), ("b", 1)], "c", 2), name) == "3 Some Card + 1 Some Shard → 2 Some Orb"
    assert describe_key(fixed_key("vendor", [("a", 5)], "c", 1), name) == "5 Some Card buys 1 Some Orb"
    assert describe_key(recipe_key("loop", ["a", "b", "c"]), name) == "Some Card → Some Shard → Some Orb"
    assert describe_key("recipe:garbled", name) == "a recipe you deleted"


# Three of one tier make one of the next, as the game's essences do: small -> big -> huge.
ESSENCES = {"big": [{"inputs": [("small", 3)], "gives": 1}], "huge": [{"inputs": [("big", 3)], "gives": 1}]}


def test_a_sale_merges_what_it_needs_from_lower_tiers(tmp_path):
    """Known answers: 45 small bought at 2 (90). Selling 5 huge needs 15 big, which need 45 small. Lowest
    tier first: 45 small make 15 big, then 15 big make 5 huge, which cost the 90 the small ones did."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 45, 2, t=1)
    sale = ledger.add_trade("L", "huge", "sell", 5, 30, t=2, recipes=ESSENCES)
    merges = ledger.merges_for(sale)
    assert [(m["inputs"], m["output"], m["gives"]) for m in merges] == [
        ([("small", 45.0)], "big", 15.0), ([("big", 15.0)], "huge", 5.0)]
    assert {m["sale"] for m in merges} == {sale} and ledger.uncovered("L") == {}
    positions = ledger.positions("L")
    # 5 huge cost the 90 the small ones did; sold at 30 each they realise 150 − 90 = 60.
    assert positions["huge"]["realized"] == pytest.approx(60) and positions["small"]["units"] == 0
    assert positions["big"]["units"] == 0


def test_a_sale_uses_what_it_holds_first_and_makes_only_whole_batches(tmp_path):
    """Known answers: 2 big held at 7 and 29 small at 2. Selling 12 big needs 10 more: 30 small would make
    them, but 29 make only 9 batches (27 small), so 1 big is uncovered and 2 small stay held. The sale's
    cost: 2 × 7 + 27 × 2 = 68 for 11 big, the twelfth at nothing; it realises 12 × 10 − 68 = 52."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "big", "buy", 2, 7, t=1)
    ledger.add_trade("L", "small", "buy", 29, 2, t=2)
    sale = ledger.add_trade("L", "big", "sell", 12, 10, t=3, recipes=ESSENCES)
    [merge] = ledger.merges_for(sale)
    assert merge["inputs"] == [("small", 27.0)] and merge["gives"] == 9
    assert ledger.uncovered("L") == {("trade", sale): pytest.approx(1)}
    positions = ledger.positions("L")
    assert positions["big"]["realized"] == pytest.approx(52) and positions["small"]["units"] == pytest.approx(2)
    assert positions["small"]["cost"] == pytest.approx(4)


def test_a_sale_the_holdings_cover_merges_nothing(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "big", "buy", 2, 7, t=1)
    ledger.add_trade("L", "small", "buy", 9, 2, t=2)
    sale = ledger.add_trade("L", "big", "sell", 2, 10, t=3, recipes=ESSENCES)
    assert ledger.merges_for(sale) == [] and ledger.positions("L")["small"]["units"] == 9
    # Without recipes nothing is merged either: the sale is simply uncovered.
    bare = ledger.add_trade("L", "big", "sell", 3, 10, t=4)
    assert ledger.merges_for(bare) == [] and ledger.uncovered("L") == {("trade", bare): pytest.approx(3)}


def test_deleting_a_sale_undoes_the_merges_recorded_for_it(tmp_path):
    """A merge the ledger worked out wrongly (you kept the small ones and sold a big one you looted) is undone
    with its sale. The merge alone cannot be deleted while its sale stands, and the sale's item and units
    cannot change: the merge was worked out for them."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 3, 2, t=1)
    sale = ledger.add_trade("L", "big", "sell", 1, 9, t=2, recipes=ESSENCES)
    [merge] = ledger.merges_for(sale)
    with pytest.raises(ValueError, match="would leave a sale selling more"):
        ledger.delete_made(merge["id"])
    with pytest.raises(ValueError, match="recorded merges for this sale"):
        ledger.edit_trade(sale, units=2)
    ledger.edit_trade(sale, price=10, note="fixed")  # the price and the note are fine
    ledger.delete_trade(sale)
    assert ledger.made("L") == [] and ledger.positions("L")["small"]["units"] == 3
    again = ledger.add_trade("L", "big", "sell", 1, 9, t=3)  # recorded without recipes: nothing merged
    assert ledger.merges_for(again) == [] and ledger.positions("L")["small"]["units"] == 3


def test_a_sale_does_not_guess_between_recipes(tmp_path):
    """Two card sets both make the reward and both are held: the ledger cannot know which you turned in.
    Known answer: no merge, the whole sale uncovered. With only one set held, it is that one."""
    recipes = {"orb": [{"inputs": [("card a", 5)], "gives": 1}, {"inputs": [("card b", 3)], "gives": 1}]}
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "card a", "buy", 5, 1, t=1)
    ledger.add_trade("L", "card b", "buy", 3, 1, t=2)
    sale = ledger.add_trade("L", "orb", "sell", 1, 10, t=3, recipes=recipes)
    assert ledger.merges_for(sale) == [] and ledger.uncovered("L") == {("trade", sale): pytest.approx(1)}
    ledger.add_trade("L", "card b", "sell", 3, 1, t=4)
    sale = ledger.add_trade("L", "orb", "sell", 1, 10, t=5, recipes=recipes)
    assert [m["inputs"] for m in ledger.merges_for(sale)] == [[("card a", 5.0)]]


def test_a_back_dated_sale_merges_from_the_holdings_of_its_time(tmp_path):
    """The small ones were bought at hour 10. A sale dated hour 5 finds nothing to merge then; one at hour 20
    merges them. A sale dated between the buy and a later merge-and-sale would take what that sale used, so
    it is recorded without its merge, uncovered."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 3, 2, t=10)
    early = ledger.add_trade("L", "big", "sell", 1, 9, t=5, recipes=ESSENCES)
    assert ledger.merges_for(early) == []
    late = ledger.add_trade("L", "big", "sell", 1, 9, t=20, recipes=ESSENCES)
    assert len(ledger.merges_for(late)) == 1
    between = ledger.add_trade("L", "big", "sell", 1, 9, t=15, recipes=ESSENCES)
    assert ledger.merges_for(between) == [] and ledger.uncovered("L")[("trade", between)] == pytest.approx(1)
    assert ("trade", late) not in ledger.uncovered("L")


def test_an_order_fill_merges_like_a_sale(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 6, 2, t=1)
    order = ledger.add_order("L", "big", "sell", "chaos", 9, 1, 2, t=2)
    fill = ledger.fill_order(order, 2, t=3, recipes=ESSENCES)
    [merge] = ledger.merges_for(fill)
    assert merge["inputs"] == [("small", 6.0)] and merge["gives"] == 2
    assert ledger.positions("L")["big"]["realized"] == pytest.approx(2 * 9 - 12)


def test_an_item_made_from_more_than_was_held_counts_the_rest_at_no_cost(tmp_path):
    """Recorded by hand, as before, but no longer refused. Known answer: 3 small held at 10, 4 used: the big
    one costs 30, and 1 small was used at no cost."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 3, 10, t=1)
    made = ledger.add_made("L", [["small", 4]], "big", t=2)
    assert ledger.uncovered("L") == {("made", made): pytest.approx(1)}
    assert ledger.positions("L")["big"]["cost"] == pytest.approx(30)


def test_a_chain_the_holdings_only_partly_cover_merges_no_more_than_it_uses(tmp_path):
    """Known answers. 15 small held, 2 huge sold: only 1 huge can be made, from 3 big made from 9 small, so 6
    small stay held and 1 huge is uncovered (not 15 small into 5 big with 2 big left that never existed).
    10 big and 9 small held, 5 huge sold: 4 huge need 12 big, 2 of them made from 6 small; 3 small stay."""
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "small", "buy", 15, 1, t=1)
    sale = ledger.add_trade("L", "huge", "sell", 2, 20, t=2, recipes=ESSENCES)
    assert [(m["inputs"], m["output"], m["gives"]) for m in ledger.merges_for(sale)] == [
        ([("small", 9.0)], "big", 3.0), ([("big", 3.0)], "huge", 1.0)]
    positions = ledger.positions("L")
    assert positions["small"]["units"] == pytest.approx(6) and positions["big"]["units"] == 0
    assert ledger.uncovered("L") == {("trade", sale): pytest.approx(1)}

    other = Ledger(tmp_path / "mixed")
    other.add_trade("L", "big", "buy", 10, 3, t=1)
    other.add_trade("L", "small", "buy", 9, 1, t=2)
    sale = other.add_trade("L", "huge", "sell", 5, 20, t=3, recipes=ESSENCES)
    assert [(m["inputs"], m["output"], m["gives"]) for m in other.merges_for(sale)] == [
        ([("small", 6.0)], "big", 2.0), ([("big", 12.0)], "huge", 4.0)]
    positions = other.positions("L")
    assert positions["small"]["units"] == pytest.approx(3) and positions["big"]["units"] == 0
    assert other.uncovered("L") == {("trade", sale): pytest.approx(1)}
