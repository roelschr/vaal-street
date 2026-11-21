"""Typed trade check and stash valuation, against the synthetic archive's known prices.

In the fixture's New league, at its last hour, the test orb's Chaos market is worth about 9.75 Chaos
and its Divine market about 10.71 Chaos (the Divine market pays 10% more by construction). The
item's blended fair value sits near 10.5, closer to the Divine market, which trades four times the
volume. A Divine is worth 147.75 Chaos. The Chaos market trades 1000 orbs an hour, the Divine
market 4000.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import ITEM, NEW, call
from vaalstreet import check
from vaalstreet.desk import Desk
from vaalstreet.ledger import Ledger, _SCHEMA


@pytest.fixture(scope="module")
def desk(archive_root):
    from vaalstreet.db import connect

    con = connect(archive_root)
    yield Desk.build(con, NEW, root=archive_root)
    con.close()


@pytest.fixture(scope="module")
def plans(desk):
    return desk.flips(min_margin=0.0)


def test_known_prices(desk):
    i = desk.panel.index(ITEM)
    assert desk.legs["r"].fair[-1, i] == pytest.approx(9.75, rel=0.01)
    assert desk.legs["b"].fair[-1, i] == pytest.approx(10.71, rel=0.01)
    assert desk.bridge == pytest.approx(147.75)


@pytest.mark.parametrize("ratio, side, verdict", [
    ("1:9", "buy", "go"),           # 9 Chaos each: 8% under the Chaos market's 9.75
    ("1:9", "sell", "no-go"),       # ...which a seller should not accept
    ("4:39", "buy", "caution"),     # 9.75 each: right on the value
    ("4:39", "sell", "caution"),
    ("1:11", "sell", "go"),         # 11 each: 13% over
    ("1:11", "buy", "no-go"),
])
def test_chaos_market_verdicts(desk, plans, ratio, side, verdict):
    items, currency = check.parse_ratio(ratio, "chaos")
    r = check.check(desk, "test orb", "chaos", side, items, currency, plans=plans)
    assert r["verdict"] == verdict, r["reasons"]
    assert r["id"] == ITEM and r["trusted"] and r["reasons"]


def test_divine_market_is_converted_and_judged_on_its_own_value(desk, plans):
    items, currency = check.parse_ratio("14 items = 1 divine", "divine")
    r = check.check(desk, "Test Orb", "divine", "buy", items, currency, stock=2000, plans=plans)
    assert r["price"] == pytest.approx(147.75 / 14, rel=1e-4)
    # 10.55 each is over the item's blended value, but under the Divine market's own 10.71: about fair.
    assert r["vs_fair"] > 0 and r["verdict"] == "caution"
    assert r["stock_hours"] == pytest.approx(0.5, rel=0.01), "2000 on offer against 4000 an hour"
    assert r["plan"]["status"] in {"beats", "matches", "misses"}
    cheap = check.check(desk, "Test Orb", "divine", "buy", *check.parse_ratio("16:1", "divine"), plans=plans)
    assert cheap["verdict"] == "go", "9.23 Chaos each in the Divine market is a bargain"


def test_plan_level_comparison(desk, plans):
    level = check._plan_level(plans, ITEM, "buy", "chaos")
    order = level["order"]
    r = check.check(desk, ITEM, "chaos", "buy", order["item_units"], order["currency_units"], plans=plans)
    assert r["plan"]["status"] == "matches"
    r = check.check(desk, ITEM, "chaos", "buy", 1, 12, plans=plans)
    assert r["plan"]["status"] == "misses"


def test_unknown_item_and_bad_ratios(desk):
    with pytest.raises(LookupError, match="Test Orb"):
        check.resolve(desk, "Test Orbb")
    with pytest.raises(ValueError, match="names Divine"):
        check.parse_ratio("64 items = 1 divine", "chaos")
    with pytest.raises(ValueError):
        check.parse_ratio("sixty-four", "chaos")
    with pytest.raises(ValueError):
        check.check(desk, "Divine Orb", "divine", "buy", 1, 1)
    assert check.parse_ratio("1 item = 140 chaos", "chaos") == (1, 140)


def test_stash_value_against_known_prices(desk):
    out = check.stash_value(desk, {ITEM: 100, "Metadata/Items/Currency/CurrencyRerollRare": 50,
                                   "Metadata/Items/Currency/CurrencyModValues": 2, "nope": 3})
    orb = next(r for r in out["rows"] if r["id"] == ITEM)
    assert orb["value"] == pytest.approx(100 * desk.fair(ITEM), rel=1e-4)
    assert orb["low"] <= orb["value"] <= orb["high"]
    assert orb["best"] == "divine" and orb["best_by"] == pytest.approx(0.10, abs=0.01)
    assert orb["hours"] == pytest.approx(100 / 4000, rel=0.01) and not orb["thin"]
    chaos = next(r for r in out["rows"] if r["id"] == "Metadata/Items/Currency/CurrencyRerollRare")
    assert chaos["value"] == 50
    divine = next(r for r in out["rows"] if r["name"] == "Divine Orb")
    assert divine["value"] == pytest.approx(2 * 147.75, rel=0.02) and divine["divine"] is None
    assert out["unpriced"] == 1
    t = out["total"]
    assert t["low"] <= t["value"] <= t["high"]
    assert t["value"] == pytest.approx(sum(r["value"] for r in out["rows"] if r["value"]), rel=1e-4)


def test_stash_table_is_added_to_an_existing_ledger(tmp_path):
    # A ledger file from before the stash existed: today's schema without the stash table.
    old = _SCHEMA.split("CREATE TABLE IF NOT EXISTS stash")[0]
    db = sqlite3.connect(tmp_path / "desk.sqlite")
    db.executescript(old)
    db.execute("INSERT INTO trades(t, league, item, side, units, price) VALUES (1, 'L', 'orb', 'buy', 5, 2)")
    db.commit()
    db.close()
    ledger = Ledger(tmp_path)
    assert ledger.positions("L")["orb"]["units"] == 5, "existing trades survive"
    ledger.set_stash("L", "orb", 7)
    ledger.set_stash("L", "orb", 9)
    ledger.set_stash("L", "card", 1)
    assert Ledger(tmp_path).stash("L") == {"card": 1, "orb": 9} and ledger.stash("Other") == {}
    ledger.set_stash("L", "card", 0)
    assert ledger.stash("L") == {"orb": 9}
    with pytest.raises(ValueError):
        ledger.set_stash("L", "orb", -1)
    ledger.clear_stash("L")
    assert ledger.stash("L") == {}


def test_api_check_and_stash(server):
    status, r = call(f"{server}/api/check", {"item": "Test Orb", "market": "chaos", "side": "buy", "ratio": "1:9"})
    assert status == 200 and r["verdict"] == "go"
    status, r = call(f"{server}/api/check", {"item": "Test Orb", "market": "divine", "side": "sell",
                                             "item_units": 14, "currency_units": 1, "stock": 100})
    assert status == 200 and r["price"] == pytest.approx(147.75 / 14, rel=1e-4) and r["stock_hours"]
    assert call(f"{server}/api/check", {"item": "Test Orbb", "market": "chaos", "side": "buy", "ratio": "1:9"})[0] == 404
    assert call(f"{server}/api/check", {"item": "Test Orb", "market": "chaos", "side": "buy", "ratio": "x"})[0] == 400

    assert call(f"{server}/api/stash")[1]["rows"] == []
    status, out = call(f"{server}/api/stash", {"item": "test orb", "units": 100})
    assert status == 200 and out["rows"][0]["id"] == ITEM and out["rows"][0]["units"] == 100
    assert call(f"{server}/api/stash")[1]["total"]["value"] == out["total"]["value"]
    assert call(f"{server}/api/stash", {"item": "Nope", "units": 1})[0] == 404
    assert call(f"{server}/api/stash", {"item": ITEM, "units": 0})[1]["rows"] == []

    call(f"{server}/api/trade", {"item": ITEM, "side": "buy", "units": 10, "price": 9})
    ledger = call(f"{server}/api/stash?source=ledger")[1]
    assert ledger["source"] == "ledger" and ledger["rows"][0]["units"] == 10


def test_cli_check_and_stash(archive_root, tmp_path, monkeypatch, capsys):
    import shutil

    from vaalstreet import cli

    # A copy of the archive, so the stash written here stays out of the shared fixture's ledger.
    root = tmp_path / "data"
    shutil.copytree(archive_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    cli.main(["check", "Test Orb", "--market", "chaos", "--side", "buy", "--ratio", "1 = 9 chaos", "--stock", "500"])
    out = capsys.readouterr().out
    assert "GO" in out and "0.5 hours" in out
    cli.main(["stash", "set", "Test Orb", "40"])
    out = capsys.readouterr().out
    assert "Test Orb" in out and "Divine +" in out
    cli.main(["stash", "clear"])
    assert "Nothing held" in capsys.readouterr().out
