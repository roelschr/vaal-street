"""Rehearsal: the desk as it stood at a past hour, and the practice session in the server.

The key property: a desk built "as of" hour h from the full archive equals a desk built from an
archive that physically ends at hour h. The cut archives are written and ingested here, from the
same synthetic digests as the full one, so any look-ahead shows up as a difference.
"""

from __future__ import annotations

import json
import shutil

import pytest

from conftest import BLITZ, H0, HOUR, ITEM, NEW, NEW_HOURS, NEW_START, OLD, all_hours, call, write_raw
from vaalstreet.db import connect
from vaalstreet.desk import Desk
from vaalstreet.ingest import ingest
from vaalstreet.leagues import LeagueBook
from vaalstreet.rehearsal import Rehearsal

# (league, league hour) cuts. New at its first hours (0, 1), when the Lure wakes (5), and on day 3;
# Old at hour 205, when Flash has just started and is not yet an event, and Blitz while Flash still runs.
CUTS = [(NEW, 0), (NEW, 1), (NEW, 5), (NEW, 95), (OLD, 205), (BLITZ, 260)]
STARTS = {NEW: NEW_START, OLD: 0, BLITZ: 210}


def _hour(league: str, h: int) -> int:
    return H0 + (STARTS[league] + h) * HOUR


@pytest.fixture(scope="module")
def cuts(archive_root, tmp_path_factory):
    """One ingested archive per cut, ending at that hour."""
    out = {}
    for league, h in CUTS:
        end = _hour(league, h)
        root = tmp_path_factory.mktemp(f"cut_{league}_{h}")
        write_raw(root, [t for t in all_hours() if t <= end])
        shutil.copytree(archive_root / "items", root / "items")
        ingest(root, log=None)
        out[(league, h)] = root
    return out


def _same(a, b):
    """JSON round trip, so NaN/None and tuple/list differences do not hide or fake a mismatch."""
    assert json.loads(json.dumps(a, default=str)) == json.loads(json.dumps(b, default=str))


def _desk_views(desk: Desk) -> dict:
    return {
        "summary": desk.summary(), "market": desk.market(), "flips": desk.flips(min_margin=0.0),
        "conversions": desk.conversions(), "loops": desk.loops(min_edge=0.0), "replay": sorted(map(str, desk.replay().items())),
    }


@pytest.mark.parametrize("cut", CUTS, ids=[f"{l}-{h}" for l, h in CUTS])
def test_as_of_desk_equals_a_desk_from_an_archive_that_ends_there(cut, con, cuts, archive_root):
    league, h = cut
    end = _hour(league, h)
    cut_con = connect(cuts[cut])
    try:
        live = Desk.build(con, league, root=archive_root, as_of=end)
        assert live.as_of == end
        _same(_desk_views(live), _desk_views(Desk.build(cut_con, league, root=archive_root)))
    finally:
        cut_con.close()


def _book_views(book: LeagueBook, league: str) -> dict:
    out = {"leagues": book.leagues(), "precedents": book.precedents(league), "day": book.day_of(league),
           "family": book.family_of(league), "curve": book.curve(ITEM), "divine_curve": book.divine_curve(),
           "launch": book.launch(league), "divine_timing": book.divine_timing(league), "meta": book.meta(league),
           "cross_league": book.cross_league(league)}
    for horizon in (1, 7):
        out[f"playbook{horizon}"] = book.playbook(league, horizon, min_value=0)
    out["standing"] = book.standing(league, min_value=0)
    out["divine_outlook"] = book.divine_outlook(league)
    return out


@pytest.mark.parametrize("cut", CUTS, ids=[f"{l}-{h}" for l, h in CUTS])
def test_as_of_league_book_equals_a_book_from_an_archive_that_ends_there(cut, archive_root, cuts):
    league, h = cut
    bounded, cut_con = connect(archive_root, until=_hour(league, h)), connect(cuts[cut])
    try:
        _same(_book_views(LeagueBook(bounded), league), _book_views(LeagueBook(cut_con), league))
    finally:
        bounded.close()
        cut_con.close()


def test_as_of_book_does_not_know_later_endings(archive_root):
    """At Blitz's hour 50 Flash is still running, so it is not yet a precedent; in the full archive it is."""
    early = connect(archive_root, until=_hour(BLITZ, 50))
    full = connect(archive_root)
    try:
        assert LeagueBook(early).precedents(BLITZ) == []
        assert LeagueBook(full).precedents(BLITZ) == ["Flash"]
        flash = next(r for r in LeagueBook(early).leagues() if r["league"] == "Flash")
        assert not flash["ended"]
    finally:
        early.close()
        full.close()


def test_as_of_at_the_last_hour_is_the_live_desk(con, archive_root):
    live = Desk.build(con, NEW, root=archive_root)
    _same(_desk_views(live), _desk_views(Desk.build(con, NEW, root=archive_root, as_of=live.as_of)))
    with pytest.raises(LookupError):
        Desk.build(con, NEW, root=archive_root, as_of=_hour(NEW, 0) - HOUR)


# ------------------------------------------------------------------ server
def _delete(url):
    import urllib.request

    request = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())


def test_rehearsal_session_over_the_api(server):
    status, live = call(f"{server}/api/desk")
    assert status == 200 and "rehearsal" not in live and live["summary"]["league_day"] == 7
    assert call(f"{server}/api/rehearsal")[1] == {"running": False}

    status, started = call(f"{server}/api/rehearsal", {"league": NEW, "start_day": 2, "bankroll": 1000})
    assert status == 200 and started["running"] and started["league_day"] == 2 and started["league_hour"] == 48
    start = started["hour"]
    assert start == _hour(NEW, 48)

    desk = call(f"{server}/api/desk")[1]
    assert desk["rehearsal"]["hour"] == start and desk["rehearsal"]["league_day"] == 2
    assert desk["summary"]["as_of"] == start and desk["summary"]["league_day"] == 2
    # The league page lines up the same day, with precedents known at that hour.
    league = call(f"{server}/api/league")[1]
    assert league["day"] == 2 and league["rehearsal"]["league"] == NEW
    status_page = call(f"{server}/api/status")[1]
    assert status_page["league"] == NEW and status_page["rehearsal"]["hour"] == start

    stepped = call(f"{server}/api/rehearsal/step", {})[1]
    assert stepped["hour"] == start + HOUR
    assert call(f"{server}/api/desk")[1]["summary"]["as_of"] == start + HOUR
    assert call(f"{server}/api/rehearsal/step", {"hours": 23})[1]["league_day"] == 3

    status, ended = _delete(f"{server}/api/rehearsal")
    assert status == 200 and not ended["running"] and ended["result"]["hours"] == 24
    back = call(f"{server}/api/desk")[1]
    assert "rehearsal" not in back and back["summary"]["league_day"] == 7


def test_practice_trades_never_reach_the_real_ledger(archive_root, tmp_path):
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    real = app.ledger
    real.add_trade(NEW, ITEM, "buy", 1, 10.0)
    app.start_rehearsal({"league": NEW, "start_day": 1})
    assert app.ledger is not real and app.ledger.path == tmp_path / "rehearsal.sqlite"
    assert app.ledger.positions(NEW) == {}, "practice starts empty"
    app.api_post("/api/trade", {"league": NEW, "item": ITEM, "side": "buy", "units": 100, "price": 10.0})
    app.api_post("/api/alert", {"league": NEW, "item": ITEM, "field": "price", "op": "above", "threshold": 1})
    app.api_post("/api/session", {"league": NEW})
    [practice_trade] = app.ledger.trades(NEW)
    assert practice_trade["t"] == app.rehearsal.hour + HOUR - 1, "stamped at the rehearsal's hour"
    app.rehearsal.step(24)
    result = app.stop_rehearsal()["result"]
    assert result["trades"] == 1
    # The real ledger: only the trade made before, no alert, no session.
    assert [t["units"] for t in real.trades(NEW)] == [1] and real.alerts(NEW) == [] and real.sessions(NEW) == []
    assert Ledger(tmp_path).trades(NEW)[0]["units"] == 1
    # A new rehearsal starts from an empty practice ledger.
    app.start_rehearsal({"league": NEW})
    assert app.ledger.trades(NEW) == []
    app.stop_rehearsal()


def test_rehearsal_result_against_holding_divines(archive_root, tmp_path):
    """Buy 100 orbs at day 1 for 10 Chaos each; a day later the orb's fair value is about 10 (it only
    swings 5% around 10 in New) and the Divine went from 106 to 112 Chaos: holding 1000 Chaos of
    Divines made 1000 * (112 / 106 - 1) = 56.6."""
    from vaalstreet.desk import Desk

    first = _hour(NEW, 0)
    practice = Rehearsal(archive_root, NEW, first, _hour(NEW, NEW_HOURS - 1), _hour(NEW, 24),
                         ledger_path=tmp_path / "rehearsal.sqlite")
    practice.ledger.add_trade(NEW, ITEM, "buy", 100, 10.0)
    practice.step(24)
    out = practice.result()
    con = connect(archive_root)
    try:
        fair = Desk.build(con, NEW, root=archive_root, as_of=_hour(NEW, 48)).fair(ITEM)
    finally:
        con.close()
    assert out["hours"] == 24 and out["profit"] == pytest.approx(100 * (fair - 10.0), abs=0.01)
    assert out["divine_then"] == pytest.approx(106.0) and out["divine_now"] == pytest.approx(112.0)
    assert out["hold_divines"] == pytest.approx(1000 * (112 / 106 - 1), abs=0.01)
    assert out["vs_divines"] == pytest.approx(out["profit"] - out["hold_divines"], abs=0.02)
    practice.close()


def test_rehearsal_clock_runs_at_its_speed_and_stops_at_the_end(archive_root, tmp_path):
    now = [0.0]
    first, last = _hour(NEW, 0), _hour(NEW, NEW_HOURS - 1)
    practice = Rehearsal(archive_root, NEW, first, last, first, speed=2.0, wall=lambda: now[0],
                         ledger_path=tmp_path / "rehearsal.sqlite")
    now[0] = 90  # a minute and a half at two game hours a minute
    assert practice.hour == first + 3 * HOUR
    assert practice.desk().as_of == first + 3 * HOUR
    now[0] = 10**6
    assert practice.hour == last and practice.done
    with pytest.raises(ValueError):
        Rehearsal(archive_root, NEW, first, last, last + HOUR, ledger_path=tmp_path / "x.sqlite")
    practice.close()


def test_sync_thread_stays_live_while_requests_see_the_rehearsal(archive_root, tmp_path):
    import threading

    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    real, live_hour = app.ledger, app.desk(NEW).as_of
    app.start_rehearsal({"league": NEW, "start_day": 1})
    hour = app.rehearsal.hour
    # Backtests and searches read through the cursor: it ends at the rehearsal hour too.
    assert app.cursor().execute("SELECT epoch(max(hour))::BIGINT FROM prices").fetchone()[0] == hour
    seen = {}

    def sync_side():
        app._thread.live = True  # what the sync loop does first
        seen.update(desk=app.desk().as_of, ledger=app.ledger, league=app.default_league())

    worker = threading.Thread(target=sync_side)
    worker.start()
    worker.join()
    assert seen == {"desk": live_hour, "ledger": real, "league": NEW}
    assert app.desk().as_of == hour and app.ledger is not real
    app.stop_rehearsal()
