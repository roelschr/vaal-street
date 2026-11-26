"""The Gold fee table, against a local stand-in for poedb's page.

The page here is written for the test: a few named items with known numbers, padded with made-up
ones to the size the reader expects. Nothing here calls the real poedb.
"""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import ITEM, NEW
from vaalstreet import fees
from vaalstreet.paths import fees_file, fees_page

UA_CONTACT = "+https://github.com/roelschr/vaal-street"
HOUR = 3600

# Known answers: a Chaos Orb costs 15 Gold, a Divine Orb 250, the archive's Test Orb 40, lifeforce an
# eighth of one Gold. "Twice Listed" stands on the page with two numbers, so it has no fee.
KNOWN = [("Chaos Orb", "15"), ("Divine Orb", "250"), ("Test Orb", "40"), ("Wild Crystallised Lifeforce", "1/8"),
         ("Mirror of Kalandra", "25,000"), ("Maven&#039;s Chisel", "200"), ("Twice Listed", "75"), ("Twice Listed", "250")]


def entry(name: str, gold: str) -> str:
    return (f'<div class="col"><a class="item_currency" href="{name.replace(" ", "_")}">{name}</a>'
            f"<span>{gold}</span></div>")


def page(extra: int = 12, rows=KNOWN) -> str:
    filler = [(f"Filler Item {n}", str(5 * n + 5)) for n in range(extra)]
    return "<html><body><h5>Currency</h5>" + "".join(entry(*row) for row in list(rows) + filler) + "</body></html>"


class FakePoedb:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.body = page()
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(dict(self.headers))
                self.send_response(fake.status)
                self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if fake.status == 200:
                    self.wfile.write(gzip.compress(fake.body.encode("utf-8")))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/us/Currency_Exchange"


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = FakePoedb()
    yield server
    server.httpd.shutdown()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def get(root, fake, clock, league="New", **kw):
    return fees.refresh(root, "poe1", league, url=fake.url, min_items=10, clock=clock, **kw)


def test_the_page_reads_as_names_and_gold():
    table = fees.parse(page(), min_items=10)
    assert table["Chaos Orb"] == 15 and table["Divine Orb"] == 250 and table["Mirror of Kalandra"] == 25000
    assert table["Wild Crystallised Lifeforce"] == 0.125, "less than one Gold an item is real"
    assert table["Maven's Chisel"] == 200, "names are unescaped"
    assert "Twice Listed" not in table, "two different numbers for one name: unknown, not the first"


def test_a_page_that_is_not_the_table_is_refused():
    with pytest.raises(fees.FeesError, match="Divine Orb"):
        fees.parse(page(rows=[("Chaos Orb", "15")]), min_items=10)
    with pytest.raises(fees.FeesError, match="2 items"):
        fees.parse(page(extra=0, rows=KNOWN[:2]), min_items=10)
    with pytest.raises(fees.FeesError):
        fees.parse("<html>Access denied</html>")


def test_one_fetch_per_league_with_the_desks_user_agent(tmp_path, fake):
    clock = Clock()
    assert not len(fees.load(tmp_path)) and fees.due(tmp_path, "poe1", "New", clock())
    table = get(tmp_path, fake, clock)
    assert table.get("chaos orb") == 15 and table.league == "New" and table.fetched == clock.now and not table.stale
    assert len(fake.requests) == 1 and UA_CONTACT in fake.requests[0]["User-Agent"]
    # The page is kept as served, next to the table read from it.
    assert gzip.decompress(fees_page(tmp_path, "poe1").read_bytes()).decode() == fake.body

    clock.now += 60 * 24 * HOUR
    get(tmp_path, fake, clock)
    assert len(fake.requests) == 1, "same league: fees do not change mid-league, so nothing is asked"
    assert fees.load(tmp_path, "poe1", "Next").stale, "a table from another league is marked stale"
    get(tmp_path, fake, clock, league="Next")
    assert len(fake.requests) == 2 and fees.load(tmp_path, "poe1", "Next").league == "Next"
    get(tmp_path, fake, clock, league="Next", force=True)
    assert len(fake.requests) == 3, "asking by hand always fetches"


def test_a_failed_fetch_keeps_the_old_table_and_waits(tmp_path, fake):
    clock = Clock()
    get(tmp_path, fake, clock)
    fake.body = "<html>Access denied</html>"
    clock.now += HOUR
    assert len(get(tmp_path, fake, clock, league="Next")) and len(fake.requests) == 1, "a day between fetches, whatever the league"
    clock.now += fees.MIN_GAP
    table = get(tmp_path, fake, clock, league="Next")
    # Known answer: the old numbers stand, marked stale, with the reason.
    assert table.get("Divine Orb") == 250 and table.stale and table.league == "New" and "fee table" in table.error
    assert len(fake.requests) == 2
    get(tmp_path, fake, clock, league="Next")
    assert len(fake.requests) == 2, "no second try right after a failure"
    fake.status = 503
    clock.now += fees.RETRY_AFTER + 1
    assert get(tmp_path, fake, clock, league="Next").get("Divine Orb") == 250 and len(fake.requests) == 3
    fake.status, fake.body = 200, page()
    clock.now += fees.RETRY_AFTER + 1
    table = get(tmp_path, fake, clock, league="Next")
    assert table.league == "Next" and table.error is None and not table.stale

    # With no table at all, a failure leaves an empty one and never raises.
    empty = tmp_path / "empty"
    fake.status = 500
    assert not len(get(empty, fake, clock)) and fees.load(empty).error


def test_a_disk_that_cannot_be_written_does_not_make_the_desk_ask_again(archive_root, tmp_path, fake, monkeypatch):
    from vaalstreet.server import App

    monkeypatch.setattr(fees, "MIN_ITEMS", 10)

    def full(path, data):
        raise OSError("No space left on device")

    monkeypatch.setattr(fees, "_write", full)
    assert not len(get(tmp_path, fake, Clock())), "the fetch never raises, whatever the disk does"
    app = App(archive_root, sync=False, fees_url=fake.url)
    monkeypatch.setattr(app, "root", tmp_path)
    monkeypatch.setattr(app, "live_league", lambda: NEW)
    before = len(fake.requests)
    for _ in range(3):
        app.refresh_fees()
    assert len(fake.requests) == before + 1, "the desk keeps its own wait when the failed try could not be stored"


def test_a_newer_reader_reads_the_kept_page_again_without_the_network(tmp_path, fake, monkeypatch):
    clock = Clock()
    get(tmp_path, fake, clock)
    path = fees_file(tmp_path, "poe1")
    stored = json.loads(path.read_text())
    stored["fees"] = {"Chaos Orb": 99.0}  # what an older reader understood
    path.write_text(json.dumps(stored))
    monkeypatch.setattr(fees, "VERSION", fees.VERSION + 1)
    monkeypatch.setattr(fees, "MIN_ITEMS", 10)
    fees._read.cache_clear()
    assert fees.load(tmp_path).get("Chaos Orb") == 15 and len(fake.requests) == 1


def test_fees_by_item_id_come_from_the_export_names(archive_root, tmp_path, fake):
    import shutil

    shutil.copytree(archive_root / "items", tmp_path / "items")
    get(tmp_path, fake, Clock())
    by_item = fees.by_item(tmp_path, "poe1")
    assert by_item[ITEM] == 40, "the archive's Test Orb, by its id"
    assert all(isinstance(v, float) for v in by_item.values())
    assert fees.by_item(tmp_path / "nothing") == {}


def test_the_desk_fetches_once_and_reports_it(archive_root, tmp_path, fake, monkeypatch):
    from vaalstreet.server import App

    monkeypatch.setattr(fees, "MIN_ITEMS", 10)
    app = App(archive_root, sync=False, fees_url=fake.url)
    monkeypatch.setattr(app, "root", tmp_path)  # keep the shared archive free of a fee table
    monkeypatch.setattr(app, "live_league", lambda: NEW)
    assert app.fees_status()["items"] == 0
    app.refresh_fees()
    app.refresh_fees()
    assert len(fake.requests) == 1
    status = app.fees_status()
    assert status["items"] == len(KNOWN) - 2 + 12 and status["league"] == NEW and not status["stale"]
    assert status["credit"] == "Gold fees from poedb.tw"

    # Plans are costed from the table, and a fee your own order taught wins over it.
    import shutil

    from vaalstreet.ledger import Ledger

    shutil.copytree(archive_root / "items", tmp_path / "items")
    app.ledger = Ledger(tmp_path / "ledger")
    assert app.costs(NEW).fee(ITEM) == (40.0, {"source": "fee table"})
    app.ledger.add_order(NEW, ITEM, "buy", "chaos", 10, 1, 4, gold=168)  # 42 Gold each
    assert app.costs(NEW).fee(ITEM) == (42.0, {"source": "your trades", "trades": 1})


def test_the_live_league_ignores_the_league_you_chose(archive_root, tmp_path):
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    live = app.live_league()
    app.ledger.set("league", "Standard")
    assert app.default_league() == "Standard" and app.live_league() == live == NEW
