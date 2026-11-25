"""What the screen check read, shown on the desk, and the corrections a player teaches it.

The companion tells the desk what each press recognised, success or failure, so a misread can be
seen. The desk keeps the last 10 in memory only. A correction ("when it reads this, it means that
item") lives in the ledger file, is a fact about the player's setup (kept during a rehearsal in the
real ledger), and is used by `match_name` before its similarity match, for that exact text only.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import call, exchange
from vaalstreet import clipboard, screen
from vaalstreet.ledger import Ledger
from vaalstreet.paths import ledger_file
from vaalstreet.rehearsal import PracticeLedger
from vaalstreet.screen import ScreenError, parse_screen
from vaalstreet.server import App

NAMES = ["Orb of Alteration", "Orb of Augmentation", "Orb of Alchemy", "Exalted Orb", "Divine Orb", "Test Orb"]
GOOD = exchange("OrbofAlteration", "DivineOrb", "64:1")
# "Orb of Altrtn" is near Alteration but not near enough to be taken for it without a correction.
DAMAGED = exchange("Orb of Altrtn", "DivineOrb", "64:1")


class TellingDesk:
    """A desk stand-in that also takes readings, and can hand out corrections with the names."""

    def __init__(self, corrections=None):
        self.checks, self.events, self.readings = [], [], []
        self.corrections = corrections or {}

    def names(self):
        return "New", NAMES

    def check(self, reading, league):
        self.checks.append(reading)
        return {"verdict": "go", "name": reading.item, "price": 0.6, "gap": -0.05, "reasons": ["Cheap."]}

    def event(self, title, body, league):
        self.events.append(title)

    def reading(self, report):
        self.readings.append(report)


def test_a_press_tells_the_desk_what_it_read_on_success():
    desk = TellingDesk()
    screen.handle_press(lambda: "image", lambda image: GOOD, desk, lambda s: None)
    (report,) = desk.readings
    assert report["kind"] == "screen" and report["lines"] == GOOD and report["error"] is None
    assert report["said"] == "buy 64 Orb of Alteration for 1 Divine"
    assert report["reading"]["item"] == "Orb of Alteration" and report["reading"]["market"] == "divine"
    assert "image" not in repr(report), "the screenshot is never sent"


def test_a_press_tells_the_desk_what_it_read_on_failure():
    desk, said = TellingDesk(), []
    assert screen.handle_press(lambda: "image", lambda image: DAMAGED, desk, said.append) is None
    (report,) = desk.readings
    assert report["reading"] is None and "couldn't tell which item" in report["error"].casefold()
    assert report["lines"] == DAMAGED and report["unmatched"] == "Orb of Altrtn"
    assert desk.checks == [] and desk.events == []


def test_the_report_is_capped():
    lines = [f"line {k} " + "x" * 200 for k in range(80)]
    report = screen.reading_report(lines, None, "nope", "New")
    assert len(report["lines"]) == screen.MAX_LINES == 60
    assert all(len(line) <= screen.MAX_CHARS == 120 for line in report["lines"])


def test_a_desk_that_cannot_take_readings_does_not_stop_the_check():
    class OldDesk(TellingDesk):
        def reading(self, report):
            raise RuntimeError("Unknown endpoint.")

    desk = OldDesk()
    assert screen.handle_press(lambda: "image", lambda image: GOOD, desk, lambda s: None)["verdict"] == "go"
    assert len(desk.checks) == 1 and len(desk.events) == 1


def test_a_correction_changes_what_is_understood_and_nothing_else():
    with pytest.raises(ScreenError):
        parse_screen(DAMAGED, NAMES)
    corrections = {screen.correction_key("Orb of Altrtn"): "Orb of Alteration"}
    assert parse_screen(DAMAGED, NAMES, corrections).item == "Orb of Alteration"
    # A similar but different text is not the corrected one: still refused.
    similar = exchange("Orb of Altrtm", "DivineOrb", "64:1")
    with pytest.raises(ScreenError):
        parse_screen(similar, NAMES, corrections)
    assert screen.correction_key("Orb of Altrtn") == screen.correction_key("orb of altrtn") == "orbofaltrtn"
    # A correction to an item the league does not know is ignored.
    assert screen.match_name("Orb of Altrtn", NAMES, {"orbofaltrtn": "Mirror of Kalandra"}) is None


def test_the_companion_uses_the_corrections_it_was_given():
    desk = TellingDesk({"orbofaltrtn": "Orb of Alteration"})
    screen.handle_press(lambda: "image", lambda image: DAMAGED, desk, lambda s: None)
    assert desk.checks[0].item == "Orb of Alteration"


def test_the_stack_check_sends_only_the_name_and_the_count():
    from test_clipboard import ORBS, FakeClip, FakeDesk

    class Desk(FakeDesk):
        def __init__(self):
            super().__init__()
            self.readings = []

        def reading(self, report):
            self.readings.append(report)

    desk = Desk()
    clipboard.handle_copy(FakeClip(), 7, desk, lambda s: None)
    (report,) = desk.readings
    assert report == {"kind": "stack", "league": "New", "item": "Test Orb", "units": 100,
                      "said": "100 Test Orb", "error": None}
    # Nothing of the copied text beyond the name and the count.
    for line in ORBS.splitlines():
        if line.strip() and line.strip() != "Test Orb":
            assert line.strip() not in repr(report)
    # Text that is not an item is not reported at all.
    desk = Desk()
    clipboard.handle_copy(FakeClip(text="my password"), 7, desk, lambda s: None)
    assert desk.readings == []


def _files(root):
    return sorted((p.relative_to(root), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


def test_the_desk_keeps_only_the_last_10_and_writes_no_file(tmp_path):
    app = App(tmp_path, sync=False)
    before = _files(tmp_path)
    for k in range(12):
        app.api_reading(screen.reading_report([f"line {k}", "<b>x</b>"], None, f"failed {k}", "New"))
    assert _files(tmp_path) == before, "readings live in memory only"
    rows = app.api_readings({})["readings"]
    assert len(rows) == 10 and rows[0]["error"] == "failed 11" and rows[-1]["error"] == "failed 2"
    assert rows[0]["lines"] == ["line 11", "<b>x</b>"], "the page escapes; the desk keeps the text as read"
    # A restarted desk has none.
    assert App(tmp_path, sync=False).api_readings({})["readings"] == []


def test_the_desk_cuts_and_rebuilds_what_it_is_sent(tmp_path):
    app = App(tmp_path, sync=False)
    app.api_reading({"kind": "screen", "lines": ["y" * 500] * 100, "said": "<script>",
                     "reading": {"item": "Test Orb", "market": "chaos", "side": "buy", "item_units": 1,
                                 "currency_units": 9, "stock": None, "extra": "ignored"}})
    row = app.api_readings({})["readings"][0]
    assert len(row["lines"]) == 60 and all(len(line) == 120 for line in row["lines"])
    assert row["said"] == "buy 1 Test Orb for 9 Chaos" and "extra" not in row["reading"]
    from vaalstreet.server import ApiError

    for bad in ({"kind": "other"}, {"reading": {"item": "x", "market": "gold", "side": "buy", "item_units": 1,
                                                "currency_units": 1}},
                {"reading": {"item": "x", "market": "chaos", "side": "buy", "item_units": 0, "currency_units": 1}},
                {"kind": "stack", "item": "x"}, {"lines": "not a list"}):
        with pytest.raises(ApiError):
            app.api_reading(bad)


def test_readings_and_corrections_through_the_api(server):
    league = call(f"{server}/api/names")[1]["league"]
    report = screen.reading_report(exchange("Tst0rb", "ChaosOrb", "1:9"), None,
                                   "Couldn't tell which item \"Tst0rb\" is", league, "Tst0rb")
    assert call(f"{server}/api/reading", report)[0] == 200
    status, out = call(f"{server}/api/readings")
    assert status == 200 and out["readings"][0]["unmatched"] == "Tst0rb" and out["corrections"] == []
    # Still local only.
    assert call(f"{server}/api/reading", report, headers={"Origin": "https://evil.example"})[0] == 403

    status, out = call(f"{server}/api/screen_correction", {"text": "Tst0rb", "item": "test orb", "league": league})
    assert status == 200 and out["corrections"][0] | {"t": 0} == {"key": "tst0rb", "text": "Tst0rb",
                                                                  "item": "Test Orb", "t": 0}
    assert call(f"{server}/api/names")[1]["corrections"] == {"tst0rb": "Test Orb"}
    # The companion, given the correction with the names, now understands the line.
    client = screen.DeskClient(server)
    _, names = client.names()
    assert parse_screen(exchange("Tst0rb", "ChaosOrb", "1:9"), names,
                        client.corrections).item == "Test Orb"

    refused = [({"text": "Te", "item": "Test Orb"}, "three letters"),
               ({"text": "I Want", "item": "Test Orb"}, "three letters"),
               ({"text": "Test Orb", "item": "Test Orb"}, "nothing to correct"),
               ({"text": "Tst0rbb", "item": "No Such Orb"}, "choose the item")]
    for body, words in refused:
        status, out = call(f"{server}/api/screen_correction", body | {"league": league})
        assert status == 400 and words in out["error"], body

    status, out = call(f"{server}/api/screen_correction", {"delete": "tst0rb"})
    assert status == 200 and out["corrections"] == [] and call(f"{server}/api/names")[1]["corrections"] == {}


def test_corrections_are_facts_kept_in_the_real_ledger_during_a_rehearsal(tmp_path):
    real, practice = Ledger(tmp_path / "real"), Ledger(tmp_path / "practice")
    during = PracticeLedger(practice, real)
    during.set_screen_correction("orbofa", "Orb of A", "Orb of Alteration")
    assert [c["item"] for c in real.screen_corrections()] == ["Orb of Alteration"]
    assert practice.screen_corrections() == []
    assert during.screen_corrections() == real.screen_corrections()
    during.delete_screen_correction("orbofa")
    assert real.screen_corrections() == []


def test_a_ledger_file_from_before_opens_and_keeps_corrections(tmp_path):
    # A file as the ledger made it before this change: no screen_corrections table.
    db = sqlite3.connect(ledger_file(tmp_path))
    db.executescript("""
        CREATE TABLE trades(id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, item TEXT NOT NULL,
            side TEXT NOT NULL, units REAL NOT NULL, price REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
        CREATE TABLE watchlist(league TEXT NOT NULL, item TEXT NOT NULL, PRIMARY KEY(league, item));
    """)
    db.execute("INSERT INTO trades(t, league, item, side, units, price) VALUES (1, 'L', 'orb', 'buy', 10, 2)")
    db.commit()
    db.close()
    ledger = Ledger(tmp_path)
    assert ledger.trades("L")[0]["units"] == 10 and ledger.screen_corrections() == []
    ledger.set_screen_correction("orbofa", "Orb of A", "Orb of Alteration")
    ledger.set_screen_correction("orbofa", "Orb ofA", "Orb of Augmentation")  # saving a text again replaces it
    ledger.close()
    (row,) = Ledger(tmp_path).screen_corrections()
    assert (row["key"], row["text"], row["item"]) == ("orbofa", "Orb ofA", "Orb of Augmentation")
