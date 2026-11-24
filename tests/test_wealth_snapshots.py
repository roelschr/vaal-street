"""Snapshots of what you hold, valued at their own hour and kept as they were.

Known answers come from the synthetic archive (tests/conftest.py). In the New league the test orb's
price swings 5% on a 12-hour cycle, so its fair value two days before the last hour differs from the
one at the last hour; the Divine price rises a quarter Chaos an hour, so it differs too.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading

import pytest

from conftest import CARD, HOUR, ITEM, NEW, OLD, call
from vaalstreet import check, wealth
from vaalstreet.desk import Desk
from vaalstreet.ledger import _SCHEMA, SNAPSHOT_KEEP_HOURLY, Ledger

DAY = 24 * HOUR


@pytest.fixture(scope="module")
def desks(archive_root):
    """The New league's desk at its last hour, and the one that stood 50 hours before."""
    from vaalstreet.db import connect

    con = connect(archive_root)
    late = Desk.build(con, NEW, root=archive_root)
    early = Desk.build(con, NEW, root=archive_root, as_of=late.as_of - 50 * HOUR)
    yield early, late
    con.close()


def _app(archive_root, tmp_path, clock=None):
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path, clock=clock)
    return app


def test_a_snapshot_is_valued_at_its_own_hour_and_never_again(desks, tmp_path):
    early, late = desks
    assert early.fair(ITEM) != pytest.approx(late.fair(ITEM), rel=1e-3) and early.bridge < late.bridge
    ledger = Ledger(tmp_path)
    ledger.set_stash(NEW, ITEM, 100)
    ledger.add_trade(NEW, ITEM, "buy", 10, 9.0)
    ledger.add_trade(NEW, ITEM, "sell", 4, 12.0)  # realises 4 x (12 - 9) = 12 Chaos
    first = wealth.take(ledger, early, "manual", "before the map")
    stored = ledger.snapshot(first)
    # Valued with the desk as it stood at its hour, and with the Stash panel's own numbers.
    assert stored["hour"] == early.as_of and stored["bridge"] == pytest.approx(early.bridge, rel=1e-5)
    assert stored["stash"] == {ITEM: [100.0, pytest.approx(early.fair(ITEM), rel=1e-5)]}
    assert stored["positions"] == {ITEM: [6.0, pytest.approx(early.fair(ITEM), rel=1e-5)]}
    assert stored["stash_value"] == check.stash_value(early, {ITEM: 100})["total"]["value"]
    assert stored["positions_value"] == check.stash_value(early, {ITEM: 6})["total"]["value"]
    assert stored["realized"] == pytest.approx(12) and stored["kind"] == "manual" and stored["note"] == "before the map"

    # Later hours, new prices and a changed stash change nothing that was stored.
    ledger.set_stash(NEW, ITEM, 50)
    ledger.add_trade(NEW, ITEM, "sell", 6, 20.0)
    second = wealth.take(ledger, late, "manual")
    assert ledger.snapshot(first) == stored
    again = ledger.snapshot(second)
    assert again["stash"][ITEM][1] == pytest.approx(late.fair(ITEM), rel=1e-5) and again["positions"] == {}
    # Sold out, the orb is still priced at the later hour, so a comparison can say what selling it was worth.
    assert again["known"] == {} and again["stash_value"] == pytest.approx(50 * late.fair(ITEM), rel=1e-4)
    rows = wealth.chart_rows(ledger, NEW, "ledger")
    assert [r["id"] for r in rows] == [first, second] and rows[0]["value"] == stored["positions_value"]
    assert rows[1]["value"] == 0 and rows[1]["realized"] == pytest.approx(12 + 6 * 11)


def test_items_no_longer_held_keep_a_price(desks, tmp_path):
    """A card held at the first snapshot and gone by the second is priced at the second (in `known`)."""
    early, late = desks
    ledger = Ledger(tmp_path)
    ledger.set_stash_many(NEW, {ITEM: 10, CARD: 3})
    wealth.take(ledger, early, "manual")
    ledger.set_stash(NEW, CARD, 0)
    second = ledger.snapshot(wealth.take(ledger, late, "manual"))
    assert set(second["stash"]) == {ITEM}
    assert second["known"] == {CARD: pytest.approx(late.fair(CARD), rel=1e-5)}


def test_one_auto_snapshot_per_league_and_hour(archive_root, tmp_path):
    app = _app(archive_root, tmp_path)
    app.ledger.set_stash(NEW, ITEM, 100)
    app.ledger.add_trade(OLD, ITEM, "buy", 5, 9.0)  # Old ended: its snapshot is at its own last hour
    taken = app._auto_snapshots()
    assert len(taken) == 2
    assert app._auto_snapshots() == [], "the same hour again takes nothing"
    app._after_refresh()  # what the sync runs after a new hour; the hour is not new here
    rows = {league: app.ledger.snapshots(league) for league in (NEW, OLD)}
    assert [len(r) for r in rows.values()] == [1, 1]
    assert rows[NEW][0]["hour"] == app.desk(NEW).as_of and rows[OLD][0]["hour"] == app.desk(OLD).as_of
    assert rows[NEW][0]["kind"] == "auto"
    # The ledger itself refuses a second auto snapshot of one hour; one you take by hand is always kept.
    desk = app.desk(NEW)
    assert wealth.take(app.ledger, desk, "auto") is None
    assert wealth.take(app.ledger, desk, "manual") is not None
    assert len(app.ledger.snapshots(NEW)) == 2


def test_nothing_is_taken_when_there_is_nothing_to_hold(archive_root, tmp_path):
    app = _app(archive_root, tmp_path)
    assert app._auto_snapshots() == []
    app.ledger.add_trade(NEW, ITEM, "buy", 5, 9.0)
    app.ledger.add_trade(NEW, ITEM, "sell", 5, 10.0)  # everything bought was sold: nothing held
    app.ledger.set_stash(NEW, ITEM, 3)
    app.ledger.set_stash(NEW, ITEM, 0)
    assert app.ledger.held_leagues() == [NEW] and app._auto_snapshots() == []
    assert app.ledger.snapshots(NEW) == []


def test_the_setting_turns_the_hourly_snapshot_off(archive_root, tmp_path):
    app = _app(archive_root, tmp_path)
    app.ledger.set_stash(NEW, ITEM, 100)
    assert app.settings()["auto_snapshots"] == 1.0, "on by default"
    app.api_post("/api/settings", {"auto_snapshots": 0})
    assert app._auto_snapshots() == [] and app.api_snapshots({"league": NEW})["auto"] is False
    app.api_post("/api/settings", {"auto_snapshots": 1})
    assert len(app._auto_snapshots()) == 1


def test_old_auto_snapshots_are_thinned_to_one_a_day(tmp_path):
    """Known answers with a clock the test sets. Auto snapshots every 6 hours over days 0 to 39, and one
    taken by hand on day 1. "Now" is day 40: days 0 to 9 are older than 30 days, so each keeps only its
    last auto snapshot (hour 18); day 10 onwards keeps all four. The manual one stays."""
    now = [0]
    ledger = Ledger(tmp_path, clock=lambda: now[0])

    def add(hour, kind="auto"):
        return ledger.add_snapshot("L", hour, kind, stash={"orb": [1, 1.0]}, positions={}, known={}, bridge=100.0,
                                   stash_value=1.0, positions_value=0.0, t=hour)

    manual = add(DAY + 2 * HOUR, "manual")
    for day in range(40):
        for h in (0, 6, 12, 18):
            add(day * DAY + h * HOUR)
    assert len(ledger.snapshots("L")) == 161, "nothing is older than 30 days while it is being taken"
    now[0] = 40 * DAY
    add(40 * DAY)  # the next auto snapshot thins the old ones
    rows = ledger.snapshots("L")
    old = [r for r in rows if r["hour"] < 10 * DAY and r["kind"] == "auto"]
    assert [r["hour"] for r in old] == [d * DAY + 18 * HOUR for d in range(10)]
    assert sum(r["hour"] >= 10 * DAY for r in rows) == 30 * 4 + 1
    assert any(r["id"] == manual for r in rows) and len(rows) == 10 + 121 + 1
    assert ledger.snapshots("Other") == []


def test_no_snapshot_during_a_rehearsal_and_the_real_file_is_unchanged(archive_root, tmp_path):
    app = _app(archive_root, tmp_path)
    real = app.ledger
    real.set_stash(NEW, ITEM, 100)
    app.start_rehearsal({"league": NEW, "start_day": 1})
    app.ledger.set_stash(NEW, ITEM, 7)  # a practice stash: practice has no wealth to chart either
    digest = hashlib.sha256(real.path.read_bytes()).hexdigest()
    seen = {}

    def sync_side():
        app._thread.live = True  # what the sync loop does first: it sees the live market and the real ledger
        seen["ledger"] = app.ledger
        seen["taken"] = app._auto_snapshots()
        app._after_refresh()

    worker = threading.Thread(target=sync_side)
    worker.start()
    worker.join()
    assert seen == {"ledger": real, "taken": []}
    assert app._auto_snapshots() == []
    app.rehearsal.step(3)
    app.rehearsal_banner()  # a new practice hour runs the refresh hook in the request thread
    with pytest.raises(Exception, match="pause during a practice run"):
        app.api_post("/api/snapshot", {"league": NEW})
    assert app.api_snapshots({})["paused"] is True and app.api_snapshots({})["rows"] == []
    assert app.ledger.practice.snapshots(NEW) == []
    app.stop_rehearsal()
    assert hashlib.sha256(real.path.read_bytes()).hexdigest() == digest
    assert real.snapshots(NEW) == []
    assert len(app._auto_snapshots()) == 1, "once the practice stops, the desk takes them again"


def test_an_older_ledger_file_opens_and_takes_snapshots(desks, tmp_path):
    # A ledger file from before snapshots existed: today's schema without their table.
    old = _SCHEMA.split("CREATE TABLE IF NOT EXISTS snapshots")[0]
    db = sqlite3.connect(tmp_path / "desk.sqlite")
    db.executescript(old)
    db.execute("INSERT INTO stash VALUES (?, ?, 4, 1)", (NEW, ITEM))
    db.commit()
    db.close()
    ledger = Ledger(tmp_path)
    assert ledger.stash(NEW) == {ITEM: 4} and ledger.snapshots(NEW) == []
    new = wealth.take(ledger, desks[1], "auto")
    assert Ledger(tmp_path).snapshot(new)["stash"][ITEM][0] == 4


def test_snapshots_over_the_api(server):
    status, body = call(server + "/api/snapshot", {"league": NEW, "note": "x"})
    assert status == 400 and "nothing to picture" in body["error"]
    call(server + "/api/stash", {"league": NEW, "item": ITEM, "units": 100})
    status, body = call(server + "/api/snapshot", {"league": NEW, "note": "after a map"})
    assert status == 200 and body["snapshot"]
    status, got = call(server + f"/api/snapshots?league={NEW}")
    assert status == 200 and got["auto"] is True and got["paused"] is False and got["source"] == "stash"
    [row] = got["rows"]
    assert row["note"] == "after a map" and row["kind"] == "manual" and row["value"] > 0 and row["bridge"] > 100
    assert "stash" not in row and "positions" not in row, "the chart rows carry no per-item lists"
    status, mine = call(server + f"/api/snapshots?league={NEW}&source=ledger")
    assert mine["rows"][0]["value"] == 0
    assert call(server + f"/api/snapshots?league={NEW}&source=nope")[0] == 400
    status, _ = call(server + "/api/snapshot", {"delete": row["id"]})
    assert status == 200 and call(server + f"/api/snapshots?league={NEW}")[1]["rows"] == []
