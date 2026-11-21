"""League families and the league-start analytics, on the synthetic archive's known answers (see conftest)."""

from __future__ import annotations

from conftest import BLITZ, FLASH, HC_NEW, NEW, OLD, STANDARD

from vaalstreet.leagues import DAY, LeagueBook, classify


def test_families_on_the_archive(con):
    rows = dict(con.execute("SELECT league, family FROM leagues").fetchall())
    assert rows[OLD] == rows[NEW] == "challenge"
    assert rows[FLASH] == rows[BLITZ] == "event", "both started mid-way through Old and ended before it"
    assert rows[HC_NEW] == "challenge_hardcore" and rows[STANDARD] == "standard"
    assert con.execute(f"SELECT base FROM leagues WHERE league = '{HC_NEW}'").fetchone()[0] == NEW


def test_family_rules_from_names_and_dates():
    def league(name, first_day, last_day):
        return {"league": name, "first_hour": first_day * DAY, "last_hour": last_day * DAY}

    rows = [
        league("Settlers", 0, 120), league("Phrecia", 40, 75), league("Hardcore Phrecia", 40, 75),
        league("HC Ruthless Settlers", 0, 118), league("Mercenaries", 121, 130),  # live, 9 days old
        league("Ruthless", 0, 130), league("Hardcore", 0, 130), league("Mine (PL123)", 3, 4),
        league("Ruthless Mine (PL9)", 3, 4), league("Keepers Event", 125, 130),
    ]
    got = {k: f for k, (f, _base) in classify(rows).items()}
    assert got == {
        "Settlers": "challenge", "Phrecia": "event", "Hardcore Phrecia": "event_hardcore",
        "HC Ruthless Settlers": "challenge_hc_ruthless", "Mercenaries": "challenge",
        "Ruthless": "standard_ruthless", "Hardcore": "standard_hardcore", "Mine (PL123)": "private",
        "Ruthless Mine (PL9)": "private", "Keepers Event": "event",
    }


def test_precedents_stay_in_the_family_and_in_the_past(con):
    book = LeagueBook(con)
    by_name = {r["league"]: r for r in book.leagues()}
    assert by_name[FLASH]["family"] == "event" and by_name[NEW]["family"] == "challenge"
    assert book.precedents(NEW) == [OLD], "events never stand in for challenge leagues"
    assert book.precedents(OLD) == [], "nothing came before Old"
    assert book.precedents(BLITZ) == [FLASH], "an event learns from earlier events that have ended"
    assert book.precedents(FLASH) == []
    assert NEW not in book.precedents(NEW)
