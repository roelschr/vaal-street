"""Rehearsal: the desk as it stood at a past hour, and the practice session in the server.

The key property: a desk built "as of" hour h from the full archive equals a desk built from an
archive that physically ends at hour h. The cut archives are written and ingested here, from the
same synthetic digests as the full one, so any look-ahead shows up as a difference.
"""

from __future__ import annotations

import json
import shutil

import pytest

from conftest import BLITZ, H0, HOUR, ITEM, NEW, NEW_START, OLD, all_hours, call, write_raw
from vaalstreet.db import connect
from vaalstreet.desk import Desk
from vaalstreet.ingest import ingest
from vaalstreet.leagues import LeagueBook

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
