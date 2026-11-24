"""Saving the ledger's tables as CSV, against the shared synthetic archive's New league.

Known answers for the records written here: 10 test orbs bought at 9 Chaos and 5 test cards at 1.5,
the 5 cards turned into 1 orb, and 4 orbs sold at 12, leaving 7 orbs. One buy order for 10 orbs at
"5 for 1 Divine". A stash of 40 test orbs. Every number in a file must be the number the Ledger
page's API answers for the same row.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request

import pytest

from conftest import CARD, ITEM, NEW, call
from vaalstreet import export

NOTE = '=HYPERLINK("http://evil.example","click")'


def fetch(url: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def parse(body: bytes) -> list[dict]:
    assert body.startswith(b"\xef\xbb\xbf"), "a UTF-8 byte order mark, so Excel reads the names right"
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))


@pytest.fixture()
def records(server):
    for body in ({"item": ITEM, "side": "buy", "units": 10, "price": 9, "gold": 40, "note": NOTE},
                 {"item": CARD, "side": "buy", "units": 5, "price": 1.5, "note": "+5 from a friend"},
                 {"item": ITEM, "side": "sell", "units": 4, "price": 12, "note": "@ the hideout"}):
        assert call(f"{server}/api/trade", {"league": NEW, **body})[0] == 200
    assert call(f"{server}/api/made", {"league": NEW, "inputs": [[CARD, 5]], "output": ITEM, "gives": 1,
                                       "note": "-turned in"})[0] == 200
    assert call(f"{server}/api/order", {"league": NEW, "item": ITEM, "side": "buy", "market": "divine",
                                        "currency_units": 1, "item_units": 5, "units": 10, "gold": 60})[0] == 200
    assert call(f"{server}/api/stash", {"league": NEW, "item": ITEM, "units": 40})[0] == 200
    return server


def get(server, what: str) -> tuple[dict, list[dict]]:
    status, headers, body = fetch(f"{server}/api/export?what={what}&league={NEW}")
    assert status == 200, body
    assert headers["Content-Type"] == "text/csv; charset=utf-8"
    assert headers["Content-Disposition"] == f'attachment; filename="vaalstreet-New-{what}.csv"'
    rows = parse(body)
    assert list(rows[0]) == export.HEADERS[what] if rows else True
    return headers, rows


def test_trades_match_the_ledger(records):
    ledger = call(f"{records}/api/ledger?league={NEW}")[1]
    _, rows = get(records, "trades")
    assert len(rows) == 3 == len(ledger["trades"])
    for row, trade in zip(rows, ledger["trades"][::-1]):  # the page lists the newest first, the file the oldest
        assert int(row["Unix time"]) == trade["t"] and row["Time (UTC)"].endswith("Z")
        assert row["Item"] == trade["name"] and row["Item id"] == trade["item"] and row["Side"] == trade["side"]
        assert float(row["Units"]) == trade["units"] and float(row["Price each (Chaos)"]) == trade["price"]
    assert rows[0]["Gold"] == "40.0" and rows[1]["Gold"] == ""
    # Free text that a spreadsheet would run as a formula starts with an apostrophe.
    assert [r["Note"] for r in rows] == ["'" + NOTE, "'+5 from a friend", "'@ the hideout"]


def test_positions_orders_made_and_stash_match_the_ledger(records):
    ledger = call(f"{records}/api/ledger?league={NEW}")[1]
    _, rows = get(records, "positions")
    page = {p["id"]: p for p in ledger["positions"]}
    assert {r["Item id"] for r in rows} == {ITEM}, "the cards were all used: the page lists no line for them"
    orb = rows[0]
    assert float(orb["Units"]) == page[ITEM]["units"] == 7
    for header, key in (("Average cost (Chaos)", "average"), ("Fair value now (Chaos)", "mark"), ("Value (Chaos)", "value"),
                        ("Unrealised (Chaos)", "unrealized"), ("Realised (Chaos)", "realized")):
        assert float(orb[header]) == pytest.approx(page[ITEM][key]), header

    _, rows = get(records, "orders")
    assert len(rows) == 1
    order, shown = rows[0], ledger["orders"][0]
    assert (order["Item"], order["Side"], order["Market"], order["Status"]) == ("Test Orb", "buy", "Divine", shown["status"])
    assert (order["Ratio items"], order["Ratio currency"], order["Units"], order["Gold"]) == ("5", "1", "10.0", "60.0")

    _, rows = get(records, "made")
    assert [(r["Used"], r["Got"], r["Note"]) for r in rows] == [("5 Test Card", "1 Test Orb", "'-turned in")]

    stash = call(f"{records}/api/stash?league={NEW}")[1]["rows"][0]
    _, rows = get(records, "stash")
    assert len(rows) == 1
    line = rows[0]
    assert (line["Item"], float(line["Units"]), line["Better market"]) == ("Test Orb", 40, stash["best"].title())
    assert float(line["Worth (Chaos)"]) == stash["value"] and float(line["Low (Chaos)"]) == stash["low"]
    assert float(line["Chaos-market value (Chaos)"]) == stash["chaos"]["value"]
    assert float(line["Divine-market value (Chaos)"]) == stash["divine"]["value"]
    assert line["Last counted (UTC)"].endswith("Z")


def test_an_empty_table_has_its_header(server):
    _, _, body = fetch(f"{server}/api/export?what=orders&league={NEW}")
    assert body.decode("utf-8-sig").splitlines() == [",".join(export.HEADERS["orders"])]


def test_bad_requests_and_other_sites_are_refused(records):
    status, _, body = fetch(f"{records}/api/export?what=passwords&league={NEW}")
    assert status == 400 and b"what must be one of" in body
    assert fetch(f"{records}/api/export?what=trades", {"Origin": "https://evil.example"})[0] == 403
    assert fetch(f"{records}/api/export?what=trades", {"Host": "evil.example"})[0] == 403, "DNS rebinding"


def test_the_formula_guard():
    text = export.text("made", [["2026-01-01T00:00:00Z", "=1+1", "+2", "@SUM(A1)"], ["", "-3 Orb", "\tx", "fine = ok"]])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[1] == ["2026-01-01T00:00:00Z", "'=1+1", "'+2", "'@SUM(A1)"]
    assert rows[2] == ["", "'-3 Orb", "'\tx", "fine = ok"]
    # Numbers are numbers, not text: a loss of -5 Chaos stays -5.
    assert list(csv.reader(io.StringIO(export.text("made", [[-5, None, 1.5, ""]]))))[1] == ["-5", "", "1.5", ""]


def test_file_names():
    assert export.filename("New", "trades") == "vaalstreet-New-trades.csv"
    assert export.filename("Dead (PL1)", "stash") == "vaalstreet-Dead-PL1-stash.csv"
    assert export.filename("Hardcore New", "orders", practice=True) == "vaalstreet-Hardcore-New-practice-orders.csv"


def test_a_rehearsal_exports_the_practice_ledger(archive_root, tmp_path):
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = real = Ledger(tmp_path)
    real.add_trade(NEW, ITEM, "buy", 3, 9.0, "a real trade")
    app.start_rehearsal({"league": NEW, "start_day": 1})
    app.api_post("/api/trade", {"league": NEW, "item": ITEM, "side": "buy", "units": 5, "price": 10.0, "note": "practice"})
    name, text = app.export("trades", NEW)
    assert name == "vaalstreet-New-practice-trades.csv"
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["Note"] for r in rows] == ["practice"], "the practice ledger, never the real one"
    app.stop_rehearsal()
    name, text = app.export("trades", NEW)
    assert name == "vaalstreet-New-trades.csv"
    assert [r["Note"] for r in csv.DictReader(io.StringIO(text))] == ["a real trade"]


def test_cli_export(archive_root, tmp_path, monkeypatch, capsys):
    import shutil

    from vaalstreet import cli
    from vaalstreet.ledger import Ledger

    root = tmp_path / "data"
    shutil.copytree(archive_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    Ledger(root).add_trade(NEW, ITEM, "buy", 3, 9.5, "=cli")
    cli.main(["export", "trades", "--league", NEW])
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert [(r["Item"], r["Units"], r["Price each (Chaos)"], r["Note"]) for r in rows] == [("Test Orb", "3.0", "9.5", "'=cli")]
    target = tmp_path / "trades.csv"
    cli.main(["export", "trades", "--league", NEW, "-o", str(target)])
    assert parse(target.read_bytes())[0]["Item"] == "Test Orb"
    with pytest.raises(SystemExit):
        cli.main(["export", "passwords"])
