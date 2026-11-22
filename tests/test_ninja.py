"""The poe.ninja client and the card sets it prices, against a local stand-in for poe.ninja.

The fixtures follow the response shapes in poe.ninja's API reference (https://poe.ninja/docs/api):
the leagues list, and stash item overview `lines` with name, chaosValue, links, corrupted,
gemLevel, gemQuality, variant and listingCount. Nothing here calls the real poe.ninja.
"""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import BLADE_CARD, NEW
from vaalstreet import archive, items, ninja

UA_CONTACT = "+https://github.com/roelschr/vaal-street"

LEAGUES = [{"id": "New", "name": "New"}, {"id": "Standard", "name": "Standard"}]
# Known answer: the plain Test Blade lists at 30 Chaos (12 listings); its six-link copy at 100 must not win.
UNIQUE_WEAPONS = {"lines": [
    {"id": 1, "name": "Test Blade", "baseType": "Rusted Sword", "itemClass": 3, "chaosValue": 30.0,
     "divineValue": 0.3, "links": None, "corrupted": False, "count": 20, "listingCount": 12},
    {"id": 2, "name": "Test Blade", "baseType": "Rusted Sword", "itemClass": 3, "chaosValue": 100.0,
     "divineValue": 1.0, "links": 6, "corrupted": False, "count": 3, "listingCount": 2},
]}
EMPTY = {"lines": []}


class FakeNinja:
    """Serves the fixtures, answers If-None-Match with 304 and can be told to fail."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.etag = '"v1"'
        self.fail: list[tuple[int, str | None]] = []  # (status, Retry-After) to answer before succeeding
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append((self.path, dict(self.headers)))
                if fake.fail:
                    status, wait = fake.fail.pop(0)
                    self.send_response(status)
                    if wait:
                        self.send_header("Retry-After", wait)
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == fake.etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                body = fake.body(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = gzip.compress(json.dumps(body).encode())
                self.send_response(200)
                self.send_header("ETag", fake.etag)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    @staticmethod
    def body(path: str):
        if path.startswith(ninja.LEAGUES):
            return LEAGUES
        if path.startswith(ninja.STASH_ITEMS):
            return UNIQUE_WEAPONS if "type=UniqueWeapon" in path else EMPTY
        if path.startswith((ninja.EXCHANGE, ninja.STASH_CURRENCY)):
            return {"lines": [], "core": {}, "currencyDetails": []}
        return None

    def down(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("POE_USER_AGENT", raising=False)
    server = FakeNinja()
    yield server
    server.httpd.shutdown()


def client(tmp_path, fake, clock):
    return ninja.Ninja(tmp_path, fake.url, delay=0, clock=clock, sleep=clock.sleep)


def test_a_304_keeps_the_cached_answer(tmp_path, fake):
    clock = Clock()
    c = client(tmp_path, fake, clock)
    first = c.item_overview("New", "UniqueWeapon")
    assert first.body == UNIQUE_WEAPONS and not first.stale
    clock.now += ninja.MIN_INTERVAL
    second = c.item_overview("New", "UniqueWeapon")
    path, headers = fake.requests[-1]
    assert headers.get("If-None-Match") == '"v1"', "the second request is conditional"
    assert second.body == UNIQUE_WEAPONS and second.fetched == int(clock.now) and not second.stale


def test_each_list_is_asked_for_at_most_hourly(tmp_path, fake):
    clock = Clock()
    c = client(tmp_path, fake, clock)
    c.item_overview("New", "UniqueWeapon")
    clock.now += ninja.MIN_INTERVAL - 1
    c.item_overview("New", "UniqueWeapon")
    c.item_overview("New", "UniqueWeapon")
    assert len(fake.requests) == 1
    c.item_overview("New", "SkillGem")  # another category has its own hour
    assert len(fake.requests) == 2
    # The interval is kept on disk, so a restarted desk does not ask again either.
    again = ninja.Ninja(tmp_path, fake.url, delay=0, clock=clock, sleep=clock.sleep)
    assert again.item_overview("New", "UniqueWeapon").body == UNIQUE_WEAPONS
    assert len(fake.requests) == 2


def test_backs_off_on_429_and_honours_retry_after(tmp_path, fake):
    clock = Clock()
    c = client(tmp_path, fake, clock)
    fake.fail = [(429, "7"), (503, None)]
    got = c.item_overview("New", "UniqueWeapon")
    assert got.body == UNIQUE_WEAPONS
    assert clock.slept == [7.0, 10.0], "Retry-After first, then a growing pause for the 503"
    # A long Retry-After is not slept out: the next try simply waits for it, past the hourly interval.
    fake.fail = [(429, "7200")]
    clock.now += ninja.MIN_INTERVAL
    stale = c.item_overview("New", "UniqueWeapon")
    assert stale.body == UNIQUE_WEAPONS and stale.stale and stale.error == "HTTP 429"
    assert clock.slept == [7.0, 10.0]
    asked = len(fake.requests)
    clock.now += ninja.MIN_INTERVAL
    c.item_overview("New", "UniqueWeapon")
    assert len(fake.requests) == asked, "still inside the Retry-After"
    clock.now += ninja.MIN_INTERVAL
    assert not c.item_overview("New", "UniqueWeapon").stale


def test_a_rate_limit_on_one_list_holds_back_the_others(tmp_path, fake):
    clock = Clock()
    c = client(tmp_path, fake, clock)
    meta = {"card": {"set": {"need": 4, "gives": 1, "reward": "Test Blade", "kind": "unique"}}}
    c.leagues()
    fake.fail = [(429, "600")]
    summary = ninja.refresh(c, "New", meta)
    assert summary["errors"] == ["UniqueWeapon: HTTP 429"]
    assert sum("type=" in p for p, _ in fake.requests) == 1, "no other list is asked for during the wait"
    clock.now += 300
    ninja.refresh(c, "New", meta)
    assert sum("type=" in p for p, _ in fake.requests) == 1, "the pause covers every list"
    clock.now += ninja.HOST_PAUSE
    ninja.refresh(c, "New", meta)
    # The other five lists now; the one that was refused keeps its own hourly interval.
    assert sum("type=" in p for p, _ in fake.requests) == len(ninja.UNIQUE_TYPES)


def test_cache_reads_do_not_wait_for_a_request_in_flight(tmp_path, fake):
    c = client(tmp_path, fake, Clock())
    c.item_overview("New", "UniqueWeapon")
    with ninja._ONE_AT_A_TIME:  # as if the sync thread were mid-request
        assert c.item_overview("New", "UniqueWeapon", refresh=False).body == UNIQUE_WEAPONS


def test_unreachable_poe_ninja_leaves_the_last_answer_marked_stale(tmp_path, fake):
    clock = Clock()
    c = client(tmp_path, fake, clock)
    c.item_overview("New", "UniqueWeapon")
    fake.down()
    clock.now += ninja.MIN_INTERVAL
    got = c.item_overview("New", "UniqueWeapon")
    assert got.body == UNIQUE_WEAPONS and got.stale and got.error.startswith("no answer")
    assert clock.slept == [], "an unreachable host is not retried in a loop"
    # Never fetched at all: no body, stale.
    assert ninja.Ninja(tmp_path / "other", fake.url, delay=0, clock=clock).leagues().body is None


def test_every_outbound_request_names_the_project(tmp_path, fake, monkeypatch):
    clock = Clock()
    c = client(tmp_path, fake, clock)
    c.leagues()
    c.exchange_overview("New", "Currency")
    c.item_overview("New", "UniqueWeapon")
    c.currency_overview("New", "Currency")
    # The exchange digests, the item export and icons go out with the same string.
    for call in (lambda: archive.fetch_hour(fake.url, "poe1", 0),):
        with pytest.raises(Exception):
            call()
    monkeypatch.setitem(items.SOURCES, "poe1", fake.url + "/export")
    with pytest.raises(Exception):
        items.download(tmp_path, "poe1")
    from vaalstreet.server import App

    monkeypatch.setattr(items, "ICON_URL", fake.url + "/image/{art}.png")
    monkeypatch.setattr(items, "load", lambda root, realm: {"x": {"art": "Art/x"}})
    app = App(tmp_path, sync=False)
    assert app.icon("x") is None
    paths = [p for p, _ in fake.requests]
    assert any(p.startswith(ninja.LEAGUES) for p in paths) and any(p.startswith(ninja.EXCHANGE) for p in paths)
    assert any(p.startswith(ninja.STASH_ITEMS) for p in paths) and any(p.startswith(ninja.STASH_CURRENCY) for p in paths)
    assert any(p == "/0" for p in paths) and "/export" in paths and "/image/Art/x.png" in paths
    assert all(UA_CONTACT in headers["User-Agent"] for _, headers in fake.requests)


def test_ninja_league_comes_from_their_list_and_is_never_guessed(tmp_path, fake):
    c = client(tmp_path, fake, Clock())
    assert c.league_id("new") == "New"
    assert c.league_id("Settlers") is None


def test_card_set_with_a_unique_reward_is_priced_from_poe_ninja(archive_root, tmp_path, fake, monkeypatch):
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    app._ninja = client(tmp_path, fake, Clock())
    monkeypatch.setattr(app, "default_league", lambda: NEW)
    app.refresh_ninja()
    asked = sorted(p.split("type=")[1] for p, _ in fake.requests if "type=" in p)
    assert asked == sorted(ninja.UNIQUE_TYPES), "only the categories the card rewards need"
    assert app.ninja_state["league_id"] == "New" and not app.ninja_state["errors"]

    answer = app.api_arbitrage({"league": NEW})
    assert all(r["id"] != BLADE_CARD for r in answer["conversions"]), "kept apart from exchange-priced sets"
    row = next(r for r in answer["ninja_conversions"] if r["id"] == BLADE_CARD)
    assert answer["ninja_credit"] == "prices from poe.ninja"
    assert row["reward"] == "Test Blade" and row["reward_id"] is None
    assert row["need"] == 4 and row["cost"] == pytest.approx(20, rel=0.01)
    assert row["proceeds"] == 30 and row["margin"] == pytest.approx(0.5, rel=0.02), "the six-link copy is not used"
    assert row["reward_source"] == "poe.ninja" and row["credit"] == "prices from poe.ninja"
    assert row["reward_note"] == "reward priced by poe.ninja (stash listings, not the exchange)"
    assert row["reward_listings"] == 12 and row["reward_age_minutes"] is not None and not row["reward_stale"]
    assert row["margin_now"] is None, "no quick exit on the exchange for a unique"
    # The exchange-priced conversions are unchanged and poe.ninja never enters fair value.
    desk = app.desk(NEW)
    assert all(r["id"] != BLADE_CARD for r in desk.conversions())
    assert BLADE_CARD not in {step["id"] for r in desk.loops() for step in r["path"]}

    app.ledger.set("ninja_prices", 0.0)
    assert app.api_arbitrage({"league": NEW})["ninja_conversions"] == []
    asked = len(fake.requests)
    app.refresh_ninja()
    assert len(fake.requests) == asked, "turned off means no requests"


def test_card_text_reads_unique_gem_and_base_rewards():
    meta = items.build({
        "Card/Doctor": {"name": "The Doctor", "item_class": "DivinationCard",
                        "properties": {"stack_size": 8, "description": "<uniqueitem>{Headhunter}"}},
        "Card/Gem": {"name": "Gem Card", "item_class": "DivinationCard", "properties": {
            "stack_size": 5, "description": "<gemitem>{Level 21 Flame Link}\r\n<default>{Quality:} <augmented>{+20%}\r\n<corrupted>{Corrupted}"}},
        "Card/Base": {"name": "Base Card", "item_class": "DivinationCard",
                      "properties": {"stack_size": 6, "description": "<whiteitem>{Prismatic Ring}"}},
        "Card/Foul": {"name": "Foul Card", "item_class": "DivinationCard",
                      "properties": {"stack_size": 6, "description": "<uniqueitem>{Mageblood}\r\n<brequelmutated> {Foulborn}"}},
    })
    assert meta["Card/Doctor"]["set"] == {"need": 8, "gives": 1, "reward": "Headhunter", "kind": "unique", "corrupted": False}
    assert meta["Card/Gem"]["set"] == {"need": 5, "gives": 1, "reward": "Flame Link", "kind": "gem",
                                       "level": 21, "quality": 20, "corrupted": True}
    assert meta["Card/Base"]["set"]["kind"] == "base"
    assert "set" not in meta["Card/Foul"], "a mutated unique is a variant no listing pins down"
    assert ninja.needed_types(meta) == [*ninja.UNIQUE_TYPES, "SkillGem", "BaseType"]


def test_gem_reward_matches_level_quality_and_corruption():
    got = ninja.Cached(None, 1, False, None)
    lines = [
        {"name": "Flame Link", "gemLevel": 20, "gemQuality": 20, "corrupted": False, "chaosValue": 5, "listingCount": 90},
        {"name": "Flame Link", "gemLevel": 21, "gemQuality": 20, "corrupted": True, "chaosValue": 40, "listingCount": 4},
    ]
    reward = {"reward": "Flame Link", "kind": "gem", "level": 21, "quality": 20, "corrupted": True}
    assert ninja._pick(reward, [(l, got) for l in lines])["chaos"] == 40
    loose = ninja._pick(reward | {"level": 19}, [(l, got) for l in lines])
    assert loose["chaos"] == 40 and "no listing at level 19" in loose["variant"]


# Trimmed from one real poe.ninja sample (Covens, 2026-11-21): support gems are named "<card name> Support",
# gems absent from a field mean no quality and not corrupted, unique lines have no `corrupted` field, and
# foil uniques come with itemClass 10.
REAL_GEMS = [
    {"name": "Enhance Support", "variant": "4c", "gemLevel": 4, "corrupted": True, "itemClass": 4, "chaosValue": 1048, "listingCount": 109},
    {"name": "Enhance Support", "variant": "1", "gemLevel": 1, "itemClass": 4, "chaosValue": 2.0, "listingCount": 706},
    {"name": "Mirror Arrow", "variant": "1/20", "gemLevel": 1, "gemQuality": 20, "itemClass": 4, "chaosValue": 2.0, "listingCount": 1353},
    {"name": "Mirror Arrow", "variant": "1", "gemLevel": 1, "itemClass": 4, "chaosValue": 1.0, "listingCount": 276},
    {"name": "Mirror Arrow of Bombarding Clones", "variant": "1/20", "gemLevel": 1, "gemQuality": 20, "itemClass": 4,
     "chaosValue": 8.0, "listingCount": 48},
    {"name": "Vaal Molten Shell", "variant": "21c", "gemLevel": 21, "corrupted": True, "itemClass": 4, "chaosValue": 17.0, "listingCount": 30},
    {"name": "Vaal Molten Shell", "variant": "21/20c", "gemLevel": 21, "gemQuality": 20, "corrupted": True, "itemClass": 4,
     "chaosValue": 40.0, "listingCount": 60},
]
REAL_UNIQUES = [
    {"name": "Windripper", "baseType": "Imperial Bow", "itemClass": 3, "chaosValue": 5.0, "listingCount": 80},
    {"name": "Windripper", "baseType": "Imperial Bow", "itemClass": 3, "links": 6, "chaosValue": 60.0, "listingCount": 9},
    {"name": "Windripper", "baseType": "Imperial Bow", "itemClass": 10, "chaosValue": 900.0, "listingCount": 2},
]


@pytest.mark.parametrize("description, chaos", [
    ("<gemitem>{Level 4 Enhance}\r\n<corrupted>{Corrupted}", 1048),  # the card's "Enhance" is poe.ninja's "Enhance Support"
    ("<gemitem>{Mirror Arrow}\r\n<default>{Quality:} <augmented>{+20%}", 2.0),  # no level named: a level 1 gem
    ("<gemitem>{Level 21 Vaal Molten Shell}\r\n<corrupted>{Twice Corrupted}", 17.0),  # no quality named: none
])
def test_gem_rewards_match_real_poe_ninja_lines(description, chaos):
    reward = items.build({"c": {"name": "Card", "item_class": "DivinationCard",
                                "properties": {"stack_size": 3, "description": description}}})["c"]["set"]
    got = ninja.Cached(None, 1, False, None)
    picked = ninja._pick(reward, [(line, got) for line in REAL_GEMS])
    assert picked["chaos"] == chaos and picked["variant"] is None


def test_unique_reward_skips_foil_and_linked_copies():
    got = ninja.Cached(None, 1, False, None)
    plain = ninja._pick({"reward": "Windripper", "kind": "unique", "corrupted": False}, [(l, got) for l in REAL_UNIQUES])
    assert plain["chaos"] == 5.0 and plain["variant"] is None
    corrupt = ninja._pick({"reward": "Windripper", "kind": "unique", "corrupted": True}, [(l, got) for l in REAL_UNIQUES])
    assert corrupt["chaos"] == 5.0 and "priced as uncorrupted" in corrupt["variant"]


def test_status_falls_back_to_the_cache_after_a_restart(archive_root, tmp_path, fake, monkeypatch):
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    def app_with(base_url):
        app = App(archive_root, sync=False)
        app.ledger = Ledger(tmp_path)
        app._ninja = ninja.Ninja(tmp_path, base_url, delay=0, clock=clock, sleep=clock.sleep)
        monkeypatch.setattr(app, "default_league", lambda: NEW)
        return app

    clock = Clock()
    empty = app_with(fake.url).api_status({})["ninja"]
    assert empty["last_run"] is None and not empty["from_cache"], "nothing fetched, nothing to show"
    first = app_with(fake.url)
    first.refresh_ninja()
    assert first.api_status({})["ninja"]["from_cache"] is False
    # A restart: a new server whose in-memory state is empty, reading the same cache. The address is
    # unreachable on purpose: the status answer must come from the cache without asking poe.ninja.
    asked = len(fake.requests)
    after = app_with("http://127.0.0.1:9").api_status({})["ninja"]
    assert after["last_run"] == int(clock.now) and after["from_cache"]
    assert len(fake.requests) == asked
