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

from conftest import CARD, ITEM, NEW, OTHER, call
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


def test_a_stash_line_remembers_when_it_was_last_counted(tmp_path):
    """Known answers with a clock the test sets: a line set at 100 and another at 200. Setting the first to the
    units it already holds at 500 counts it again; a pasted list counts every line it names; a removed line
    is forgotten."""
    now = [100]
    ledger = Ledger(tmp_path, clock=lambda: now[0])
    ledger.set_stash("L", "orb", 7)
    now[0] = 200
    ledger.set_stash("L", "card", 1)
    assert ledger.stash_counted("L") == {"orb": 100, "card": 200} and ledger.stash_counted("Other") == {}
    now[0] = 500
    ledger.set_stash("L", "orb", 7)
    assert ledger.stash_counted("L") == {"orb": 500, "card": 200} and ledger.stash("L") == {"card": 1, "orb": 7}
    now[0] = 900
    ledger.set_stash_many("L", {"card": 1, "shard": 3})
    assert ledger.stash_counted("L") == {"orb": 500, "card": 900, "shard": 900}
    ledger.set_stash("L", "orb", 0)
    assert "orb" not in ledger.stash_counted("L")


def test_api_stash_says_how_long_ago_each_line_was_counted(archive_root, tmp_path):
    """The age is by the ledger's clock, so it is right in a rehearsal too. Known answers: lines set 3 hours
    and 10 minutes before "now". Ledger positions are not counted lines, so they carry no age."""
    from vaalstreet.server import App

    now = [1_000_000]
    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path, clock=lambda: now[0])
    app.ledger.set_stash(NEW, ITEM, 100, t=now[0] - 3 * 3600)
    app.ledger.set_stash(NEW, CARD, 6, t=now[0] - 600)
    rows = {row["id"]: row["counted_ago"] for row in app.api_stash({"league": NEW})["rows"]}
    assert rows == {ITEM: 3 * 3600, CARD: 600}
    app.ledger.add_trade(NEW, ITEM, "buy", 10, 9.0)
    assert all("counted_ago" not in row for row in app.api_stash({"league": NEW, "source": "ledger"})["rows"])


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


def test_a_pasted_list_is_read_line_by_line():
    """Known answers for every way a line may be written. A mark followed by exactly three digits splits
    thousands ("2,448", "1.250"); a row of a CSV file gives its first text cell and its first number cell,
    so "Test Orb,12,148.5" is 12 and never 12,148.5."""
    text = "\n".join([
        "# my stash", "", "45 Screaming Essence of Greed", "Divine Orb 12", "Divine Orb: 3", "Orb of Alchemy x300",
        "300x Orb of Fusing", "7 x Exalted Orb", "2,448 Portal Scroll", "Portal Scroll 1.250", "Chaos Orb\t1,024\t1.0",
        '"Mirror Shard";4;900', "Test Orb,12,148.5", "Test Orb, 5", "Item,Count", "just a name", "Gemcutter's Prism = 2.5"])
    read, unread = check.parse_holdings(text)
    assert [(row["line"], row["name"], row["units"]) for row in read] == [
        (3, "Screaming Essence of Greed", 45), (4, "Divine Orb", 12), (5, "Divine Orb", 3), (6, "Orb of Alchemy", 300),
        (7, "Orb of Fusing", 300), (8, "Exalted Orb", 7), (9, "Portal Scroll", 2448), (10, "Portal Scroll", 1250),
        (11, "Chaos Orb", 1024), (12, "Mirror Shard", 4), (13, "Test Orb", 12), (14, "Test Orb", 5),
        (17, "Gemcutter's Prism", 2.5)]
    assert [(row["line"], row["text"]) for row in unread] == [(15, "Item,Count"), (16, "just a name")]


def test_a_pasted_list_becomes_holdings(desk):
    """Two lines for one item are two stacks, so they add: 40 + 2 = 42 orbs. A name that does not trade is
    not read, and says which names are close."""
    holdings, unread = check.read_holdings(desk, "40 test orb\nTest Card: 6\nTest Orb x2\n3 Test Orbb\nnonsense")
    assert holdings == {ITEM: {"units": 42, "lines": [1, 3]}, CARD: {"units": 6, "lines": [2]}}
    assert [row["line"] for row in unread] == [4, 5] and "Did you mean: Test Orb" in unread[0]["why"]


def test_many_stash_lines_are_written_together_or_not_at_all(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.set_stash("L", "orb", 7)
    ledger.set_stash("L", "card", 1)
    ledger.set_stash("Other", "orb", 5)
    ledger.set_stash_many("L", {"orb": 9, "shard": 2, "card": 0})  # zero removes a line, as for one line
    assert ledger.stash("L") == {"orb": 9, "shard": 2}
    with pytest.raises(ValueError):
        ledger.set_stash_many("L", {"orb": 1, "shard": -1})
    assert ledger.stash("L") == {"orb": 9, "shard": 2}, "a refused list changes nothing"
    ledger.set_stash_many("L", {"card": 4}, replace=True)
    assert ledger.stash("L") == {"card": 4} and ledger.stash("Other") == {"orb": 5}, "another league is untouched"


def test_api_pasted_list_is_shown_before_it_is_written(server):
    """Known answers: the stash holds 100 orbs and 7 shards. The list names 40 + 2 orbs, 6 cards and one line
    that cannot be read. Read only, it changes nothing and says what would change. Written, the orbs become 42,
    the cards are added and the shards stay. Replacing the whole stash is refused while a line is unread; with
    the line fixed, the shards go."""
    assert call(f"{server}/api/stash", {"item": ITEM, "units": 100})[0] == 200
    assert call(f"{server}/api/stash", {"item": OTHER, "units": 7})[0] == 200
    text = "40 Test Orb\nTest Card: 6\nTest Orb x2\n3 Test Orbb"
    status, seen = call(f"{server}/api/stash/paste", {"text": text})
    assert status == 200 and seen["written"] is False
    assert [(row["id"], row["units"], row["was"], row["lines"]) for row in seen["lines"]] == [
        (ITEM, 42, 100, [1, 3]), (CARD, 6, None, [2])]
    assert [row["line"] for row in seen["unread"]] == [4] and seen["dropped"] == []
    held = {row["id"]: row["units"] for row in call(f"{server}/api/stash")[1]["rows"]}
    assert held == {ITEM: 100, OTHER: 7}, "reading a list writes nothing"

    seen = call(f"{server}/api/stash/paste", {"text": text, "replace": True})[1]
    assert [(row["id"], row["units"]) for row in seen["dropped"]] == [(OTHER, 7)]
    status, answer = call(f"{server}/api/stash/paste", {"text": text, "replace": True, "write": True})
    assert status == 400 and "could not be read" in answer["error"]

    status, done = call(f"{server}/api/stash/paste", {"text": text, "write": True})
    assert status == 200 and done["written"] is True
    held = {row["id"]: row["units"] for row in call(f"{server}/api/stash")[1]["rows"]}
    assert held == {ITEM: 42, CARD: 6, OTHER: 7}

    fixed = text.replace("Test Orbb", "Test Orb")
    assert call(f"{server}/api/stash/paste", {"text": fixed, "replace": True, "write": True})[0] == 200
    held = {row["id"]: row["units"] for row in call(f"{server}/api/stash")[1]["rows"]}
    assert held == {ITEM: 45, CARD: 6}
    for bad in ({}, {"text": "  "}, {"text": "nonsense", "write": True}, {"text": "x" * 200_001}):
        assert call(f"{server}/api/stash/paste", bad)[0] == 400


def test_a_list_pasted_during_a_rehearsal_goes_to_the_practice_stash(archive_root, tmp_path):
    """A rehearsal must never write to the real ledger, also not when the list replaces the whole stash."""
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = real = Ledger(tmp_path)
    real.set_stash(NEW, ITEM, 100)
    app.start_rehearsal({"league": NEW, "start_day": 1})
    app.api_stash_paste({"league": NEW, "text": "5 Test Orb", "write": True, "replace": True})
    assert app.ledger.stash(NEW) == {ITEM: 5} and real.stash(NEW) == {ITEM: 100}
    app.stop_rehearsal()
    assert app.ledger.stash(NEW) == {ITEM: 100}


def test_cli_stash_import_shows_first_and_writes_with_yes(archive_root, tmp_path, monkeypatch, capsys):
    import shutil

    from vaalstreet import cli

    root = tmp_path / "data"
    shutil.copytree(archive_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    listing = tmp_path / "stash.txt"
    listing.write_text("40 Test Orb\nTest Card,6\n3 Test Orbb\n", encoding="utf-8")
    cli.main(["stash", "set", "Test Shard", "7"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        cli.main(["stash", "import", str(listing)])
    out = capsys.readouterr().out
    assert "Test Orb  (new)" in out and "line 3 not read" in out and "--yes" in str(stop.value)
    assert Ledger(root).stash(NEW) == {OTHER: 7}, "without --yes nothing is written"
    with pytest.raises(SystemExit, match="could not be read"):
        cli.main(["stash", "import", str(listing), "--replace", "--yes"])
    assert Ledger(root).stash(NEW) == {OTHER: 7}
    cli.main(["stash", "import", str(listing), "--yes"])
    assert "Wrote 2 lines" in capsys.readouterr().out
    assert Ledger(root).stash(NEW) == {ITEM: 40, CARD: 6, OTHER: 7}


def test_check_reasons_use_plain_words(desk):
    # The synthetic orb has a cross plan; its name must reach the player as words, not the code's "cross".
    r = check.check(desk, "Test Orb", "chaos", "buy", 10, 100)
    text = " ".join(r["reasons"])
    assert "cross" not in text and "flip plan" not in text
    assert "The flip that buys for Chaos and sells for Divines buys at" in text or "Chaos-market flip" in text
