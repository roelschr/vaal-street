"""League families and the league-start analytics, on the synthetic archive's known answers (see conftest)."""

from __future__ import annotations

from conftest import H0, HOUR, NEW_START, OLD_HOURS, BLITZ, EMBER, FLASH, HC_NEW, ITEM, LURE, NEW, OLD, STANDARD, call

import shutil

from conftest import write_raw

from vaalstreet.db import connect
from vaalstreet.ingest import ingest
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


def test_cross_league_tells_structural_gaps_from_noise(con):
    out = LeagueBook(con).cross_league(NEW)
    assert out["others"] == [HC_NEW, STANDARD]
    gaps = {(r["other"], r["id"], r["market"]): r for r in out["items"]}
    # The orb is 20% dearer in Hardcore New every hour, in both markets, and half price in Standard.
    for market in ("chaos", "divine"):
        hc = gaps[(HC_NEW, ITEM, market)]
        assert hc["verdict"] == "structural" and abs(hc["gap"] - 0.20) < 0.005 and hc["same_sign"] == 1
        assert gaps[(STANDARD, ITEM, market)]["verdict"] == "structural"
        assert abs(gaps[(STANDARD, ITEM, market)]["gap"] + 0.5) < 0.005
    # The ember is 10% dearer one day and 10% cheaper the next: noise.
    assert gaps[(HC_NEW, EMBER, "chaos")]["verdict"] == "noise"
    assert gaps[(STANDARD, EMBER, "chaos")]["verdict"] == "none"
    assert {r["other"]: r["verdict"] for r in out["divine"]} == {HC_NEW: "none", STANDARD: "none"}
    assert out["items"][0]["verdict"] == "structural"


def test_cross_league_endpoint_and_command(server, archive_root, monkeypatch, capsys):
    status, body = call(f"{server}/api/cross_league?league={NEW}&days=5")
    assert status == 200 and body["days"] == 5 and body["others"] == [HC_NEW, STANDARD]
    assert any(r["family"] == "event" for r in call(f"{server}/api/status")[1]["leagues"])

    from vaalstreet import cli

    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))  # read-only command: nothing is written
    cli.main(["cross-league", "--league", NEW])
    out = capsys.readouterr().out
    assert "Structural gaps" in out and "Test Orb" in out and "+20%" in out and "Test Ember" not in out


def test_launch_playbook_on_the_events(con):
    book = LeagueBook(con)
    out = book.launch(family="event")
    assert out["precedents"] == [BLITZ, FLASH] and out["shortfall"] is None
    lure = next(r for r in out["items"] if r["id"] == LURE)
    # Wakes at hour 5 and climbs 3 * (1 + h/72). Its first-day value is 3 * (1 + 14/72), the average of hours 5-23.
    assert lure["n"] == 2 and lure["wake"] == 5 and lure["first"] == "rise" and lure["agree"] == 1
    day0 = 3 * (1 + 14 / 72)
    assert abs(lure["moves"]["72"] - (3 * (1 + 71 / 72) / day0 - 1)) < 0.002  # hours 70-72 average 71
    assert abs(lure["moves"]["24"] - (3 * (1 + 23 / 72) / day0 - 1)) < 0.002  # hours 22-24 average 23
    assert lure["path"][4] is None and len(lure["path"]) == 73


def test_launch_playbook_names_the_shortfall(con):
    book = LeagueBook(con)
    assert book.launch(NEW)["shortfall"].startswith("Only 1 past challenge league has its first 72 hours")
    out = book.launch(BLITZ)
    assert out["precedents"] == [FLASH] and "Only 1 past event league" in out["shortfall"]
    assert book.launch(FLASH)["items"] == [] and "Only 0 past event leagues have" in book.launch(FLASH)["shortfall"]


def test_launch_in_the_api_and_cli(server, archive_root, monkeypatch, capsys):
    league = call(f"{server}/api/league?league={NEW}")[1]
    assert league["family"] == "challenge" and league["launch"]["precedents"] == [OLD]
    assert league["divine_timing"]["n"] == 1 and league["meta"]["items"][0]["id"] == LURE

    from vaalstreet import cli

    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    cli.main(["playbook", "--launch", "--league", BLITZ, "--min-leagues", "1"])
    out = capsys.readouterr().out
    assert "Only 1 past event league" in out and "Test Lure" in out and "rise" in out


def test_divine_timing(con, archive_root, monkeypatch, capsys):
    book = LeagueBook(con)
    # In the events the Divine costs 100 + 5 * |day - 2.5| Chaos: cheapest on day 2 in both.
    events = book.divine_timing(family="event")
    assert events["window"] == {"from": 2, "to": 2, "leagues": 2, "days_checked": 10}
    assert events["advice"].startswith("In 2 of 2 past event leagues the Divine was cheapest in Chaos on day 2")
    assert events["curve"][2]["dearer"] == 0 and events["curve"][0]["dearer"] > 0
    # In Old it only climbs, so day 0; New rests on that one league and says so.
    new = book.divine_timing(NEW)
    assert new["n"] == 1 and new["by_league"][0] == {"league": OLD, "day": 0, "price": new["by_league"][0]["price"]}
    assert "One league is not a pattern" in new["advice"] and "Today is day 7, past that stretch" in new["advice"]
    assert book.divine_timing(FLASH)["advice"] == "No past event league is on disk to time the Divine with."

    from vaalstreet import cli

    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    cli.main(["divine-timing", "--league", BLITZ])
    assert "In the one past event league on disk (Flash), the Divine was cheapest in Chaos on day 2" in capsys.readouterr().out


def test_meta_shift(con, archive_root, monkeypatch, capsys):
    book = LeagueBook(con)
    out = book.meta(NEW)
    # New trades the lure at 200 * (1 + h/24) units an hour; Old flat. Hours 168-191 against 24-47:
    # (1 + 179.5/24) / (1 + 35.5/24) = 3.42 against Old's 1.0. Every other item grows 1x in both.
    assert out["hours"] == [168, 191] and out["precedents"] == [OLD]
    [lure] = out["items"]
    assert lure["id"] == LURE and abs(lure["growth"] - 3.42) < 0.01 and lure["past_high"] == 1 and lure["n"] == 1
    assert book.meta(OLD)["note"].startswith("No past league")

    from vaalstreet import cli

    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    cli.main(["meta", "--league", NEW])
    assert "Test Lure" in capsys.readouterr().out


def test_non_softcore_leagues_get_no_precedents(con):
    book = LeagueBook(con)
    assert book.precedents(STANDARD) == [] and book.precedents(HC_NEW) == []
    assert book.divine_timing(STANDARD)["advice"] == "No past standard league is on disk to time the Divine with."


def test_league_start_analytics_ignore_later_hours(con, archive_root, tmp_path):
    """Causality: the same archive cut at New's hour 95 gives the answers known at that hour."""
    write_raw(tmp_path, [H0 + h * HOUR for h in list(range(OLD_HOURS)) + list(range(NEW_START, NEW_START + 96))])
    shutil.copytree(archive_root / "items", tmp_path / "items")
    ingest(tmp_path, log=None)
    cut = connect(tmp_path)
    try:
        early, full = LeagueBook(cut), LeagueBook(con)
        # Lure units at hours 72-95 against 24-47: (1 + 83.5/24) / (1 + 35.5/24) = 1.807; Old stays 1.0.
        [lure] = early.meta(NEW)["items"]
        assert early.meta(NEW)["hours"] == [72, 95] and abs(lure["growth"] - 1.807) < 0.005
        assert early.launch(NEW)["items"] == full.launch(NEW)["items"]
        assert early.divine_timing(NEW)["by_league"] == full.divine_timing(NEW)["by_league"]
        assert early.divine_timing(NEW)["day"] == 3
    finally:
        cut.close()
