"""The league report page: its sections against the synthetic archive's known answers, what it must never
contain, and causality (a day's page is the same whether or not later hours exist)."""

from __future__ import annotations

import re

import pytest

from tests.conftest import BLITZ, H0, HOUR, NEW, NEW_START, OLD, all_hours, write_raw
from vaalstreet import items, report
from vaalstreet.ingest import ingest


@pytest.fixture(scope="module")
def new_day7(archive_root):
    data = report.build(archive_root, NEW, 7)
    return data, report.render(data)


def names(rows):
    return [r["name"] for r in rows]


def test_divine_against_past_leagues(new_day7):
    data, page = new_day7
    d = data["divine"]
    # The Divine costs 100 + 0.25 * league hour in both Old and New, so day 7 costs the same in both.
    assert [r["league"] for r in d["past"]] == [OLD]
    assert d["past"][0]["price"] == pytest.approx(d["own"][7][1], rel=1e-3)
    assert d["own"][7][1] == pytest.approx(100 + 0.25 * (7 * 24 + 11.5), rel=1e-3)
    assert d["past"][0]["change"] == pytest.approx(6 / d["own"][6][1], rel=1e-2)
    assert "The Divine Orb" in page and "One league is not a pattern" in page


def test_movers_and_most_traded(archive_root, new_day7):
    data, page = new_day7
    # In New every price but the Divine is flat, and the Divine is left out of movers: no movers section.
    assert data["risers"] == [] and data["fallers"] == []
    assert "Biggest movers" not in page
    # The ember trades 2,000 units at 4 Chaos every hour (plus its Divine market): the most traded item.
    assert names(data["traded"])[:2] == ["Test Ember", "Test Orb"]
    assert "Divine Orb" not in names(data["traded"])  # it has its own section
    orb = data["traded"][1]
    # Both markets, kept apart: the orb's Divine market pays 10% more.
    assert orb["divine"] / orb["chaos"] == pytest.approx(1.10, rel=0.02)
    # In Old the orb trends up 10% of its start price a day: on day 7 it is up about 1.8 / 1.7 - 1.
    old = report.build(archive_root, OLD, 7)
    assert names(old["risers"]) == ["Test Orb"]
    assert old["risers"][0]["change_24h"] == pytest.approx(1.8 / 1.7 - 1, abs=0.01)


def test_playbook_standing_meta_and_gaps(new_day7):
    data, page = new_day7
    pb = data["playbook"]
    assert pb["precedents"] == [OLD]
    # Old's orb kept rising after day 7 in Divines as well: it is a "rose next" row, 1 of 1 league.
    assert names(pb["up"]) == ["Test Orb"] and pb["up"][0]["agree_k"] == 1 and pb["up"][0]["n"] == 1
    assert "1 of 1" in page
    # New's orb is flat while Old's had risen 70% by day 7: cheaper than usual, never dearer.
    assert names(data["standing"]["cheap"]) == ["Test Orb"] and data["standing"]["dear"] == []
    # The lure's demand grows (1 + h/24) times in New and stays flat in Old.
    assert names(data["meta"]) == ["Test Lure"]
    # The orb is 20% dearer in Hardcore New and half price in Standard; the ember's flipping gap is noise.
    assert list(data["gaps"]) == ["Hardcore New", "Standard"]  # Hardcore first
    gaps = {(o, r["market"]): r["gap"] for o, rows in data["gaps"].items() for r in rows if r["name"] == "Test Orb"}
    assert gaps[("Hardcore New", "chaos")] == pytest.approx(0.20, abs=0.01)
    assert gaps[("Standard", "chaos")] == pytest.approx(-0.50, abs=0.01)
    assert all("Test Ember" not in names(rows) for rows in data["gaps"].values())
    for title in ("What past leagues did next", "Cheap or dear against past leagues", "Meta shifts",
                  "Steady gaps against Hardcore and Standard", "Most traded"):
        assert title in page


def test_sections_without_data_are_left_out(archive_root):
    data = report.build(archive_root, OLD, 3)
    page = report.render(data)
    # Old has no past league, Hardcore or Standard: only its own market sections remain.
    for title in ("What past leagues did next", "Cheap or dear", "Meta shifts", "Steady gaps"):
        assert title not in page
    assert "Most traded" in page


def test_safe_to_publish(archive_root, tmp_path, new_day7):
    _data, page = new_day7
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", page))
    assert urls == {report.PROJECT_URL, report.API_URL, report.REPOE_URL}
    for banned in ("<script", "<img", "<link", "url(", "src=", "@import", "@font-face", "web.poecdn", "xmlns"):
        assert banned not in page
    assert "affiliated with or endorsed by Grinding Gear Games" in page
    # Nothing about the user or their machine: no data folder path, no ledger.
    assert str(archive_root) not in page
    for word in ("ledger", "stash", "watchlist", "alert", "position"):
        assert word not in page.lower()


def test_ledger_data_never_reaches_the_page(archive_root, tmp_path):
    from vaalstreet.ledger import Ledger

    root = tmp_path / "data"
    root.mkdir()
    for sub in ("parquet", "items"):
        (root / sub).symlink_to(archive_root / sub, target_is_directory=True)
    ledger = Ledger(root)
    marker = "Secret Fill Note 4711"
    ledger.add_trade(NEW, "Metadata/Items/Currency/CurrencyTestOrb", "buy", 4711, 9.5, note=marker)
    ledger.set_stash(NEW, "Metadata/Items/Currency/CurrencyTestOrb", 4711)
    page = report.render(report.build(root, NEW, 7))
    assert marker not in page and "4711" not in page and "desk.sqlite" not in page


def _truncated_archive(root, until: int):
    write_raw(root, [h for h in all_hours() if h <= until])
    target = root / "items" / "poe1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


@pytest.mark.parametrize("league, start, day", [(NEW, NEW_START, 3), (BLITZ, 210, 3)])
def test_a_day_does_not_see_later_hours(archive_root, tmp_path, league, start, day):
    """The page for day d is byte-identical from the full archive and from one that ends with day d.

    Blitz day 3 is the sharp case: Flash is still running then, so it must not count as a past league,
    although in the full archive it has ended.
    """
    until = H0 + (start + 24 * day + 23) * HOUR
    root = tmp_path / "cut"
    target = _truncated_archive(root, until)
    target.write_text((archive_root / "items" / "poe1.json").read_text())
    items.load.cache_clear()
    ingest(root, log=None)
    cut = report.render(report.build(root, league, day))
    full = report.render(report.build(archive_root, league, day))
    items.load.cache_clear()
    assert cut == full
    assert report.build(root, league)["day"] == day  # the cut archive really ends on that day


def test_write_all_days_and_index(archive_root, tmp_path, monkeypatch):
    out = tmp_path / "site"
    written = report.write(archive_root, out, NEW, all_days=True, log=None)
    pages = sorted(p.name for p in written)
    assert pages == sorted([f"new-day-{d}.html" for d in range(8)] + ["index.html"])
    index = (out / "index.html").read_text()
    assert all(f'href="new-day-{d}.html"' in index for d in range(8))
    # One more day refreshes the index without losing the others.
    report.write(archive_root, out, OLD, 2, log=None)
    index = (out / "index.html").read_text()
    assert 'href="old-day-2.html"' in index and 'href="new-day-0.html"' in index


def test_cli(archive_root, tmp_path, monkeypatch, capsys):
    from vaalstreet import cli

    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    out = tmp_path / "reports"
    cli.main(["report", "--league", NEW, "--day", "5", "--out", str(out)])
    assert (out / "new-day-5.html").exists() and (out / "index.html").exists()
    assert "Nothing was uploaded" in capsys.readouterr().err
