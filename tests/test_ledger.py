"""The ledger's Gold, orders, sessions and recipe checks, and its additive migration."""

from __future__ import annotations

import sqlite3

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
        ledger.fill_order(sell, 200)  # the ledger holds only 128
    assert ledger.order(sell)["status"] == "open" and ledger.order(sell)["filled"] == 0, "a refused fill changes nothing"
    ledger.fill_order(sell, 10, record_trade=False)
    assert len(ledger.trades("L")) == 2
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

    # A file that is already oversold (a buy deleted by hand) does not block an unrelated correction.
    ledger.delete_trade(buy)
    ledger.edit_trade(sale, note="typo fixed", price=3.5)
    assert ledger.trades("L")[0]["note"] == "typo fixed"


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
