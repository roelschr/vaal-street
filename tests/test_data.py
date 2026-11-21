"""Archive, ingest and the views."""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import CARD, H0, HOUR, ITEM, NEW, OLD, PREMIUM, digest, divine_price, item_price, write_raw
from vaalstreet import archive, items
from vaalstreet.ingest import CHAOS, DIVINE, ingest


@pytest.fixture()
def exchange(monkeypatch):
    """A local stand-in for the exchange endpoint: serves the first 30 hours, 404s the rest."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(archive.time, "sleep", lambda seconds: None)  # no real backoff in tests
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hour = int(self.path.rsplit("/", 1)[-1])
            if hour >= H0 + 30 * HOUR:
                self.send_error(404)
                return
            body = gzip.compress(json.dumps(digest(hour)).encode())
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_archive_fetches_only_missing_hours_newest_first(tmp_path, exchange):
    write_raw(tmp_path, [H0 + 5 * HOUR])
    todo = archive.missing(tmp_path, "poe1", H0 - 10 * HOUR, H0 + 9 * HOUR)
    assert todo[0] == H0 + 9 * HOUR and todo[-1] == H0, "clamped to the first published hour, newest first"
    assert H0 + 5 * HOUR not in todo
    saved = archive.archive_realm(tmp_path, "poe1", start=H0, end=H0 + 9 * HOUR, base_url=exchange, delay=0, log=None)
    assert saved == 9
    assert archive.missing(tmp_path, "poe1", H0, H0 + 9 * HOUR) == []
    raw = gzip.decompress(archive.hour_path(tmp_path, "poe1", H0).read_bytes())
    assert json.loads(raw) == digest(H0), "stored bytes are the served digest"


def test_archive_skips_hours_the_exchange_has_not_published(tmp_path, exchange):
    hours = [H0 + 29 * HOUR, H0 + 30 * HOUR, H0 + 31 * HOUR]
    assert archive.archive_hours(tmp_path, "poe1", hours, base_url=exchange, delay=0, log=None) == 1
    assert archive.have(tmp_path, "poe1") == {H0 + 29 * HOUR}


def test_latest_published_waits_for_the_hour_to_close():
    assert archive.latest_published(H0 + HOUR + archive.PUBLISH_LAG) == H0
    assert archive.latest_published(H0 + HOUR + archive.PUBLISH_LAG - 1) == H0 - HOUR


def test_prices_combine_both_markets_and_convert_the_divine_leg(con):
    row = con.execute(
        "SELECT price, r_price, b_price, bridge, volume, value, r_volume, b_volume FROM prices "
        "WHERE league = ? AND item = ? AND hour = to_timestamp(?)", [NEW, ITEM, H0 + (30 * 24 + 3) * HOUR],
    ).fetchall()[0]
    price, r_price, b_price, bridge, volume, value, r_volume, b_volume = row
    p, div = item_price(NEW, 3), divine_price(3)
    assert bridge == pytest.approx(round(div * 40) / 40)
    assert r_price == pytest.approx(p, rel=1e-3)
    assert b_price == pytest.approx(p * PREMIUM, rel=5e-3), "Divine-market price is expressed in Chaos"
    assert (r_volume, b_volume, volume) == (1000, 4000, 5000)
    assert price == pytest.approx(value / volume)
    assert r_price < price < b_price


def test_a_joke_trade_in_one_market_does_not_move_the_headline_price(tmp_path):
    from conftest import market
    from vaalstreet.archive import hour_path
    from vaalstreet.db import connect

    body = {"next_change_id": H0 + HOUR, "markets": [
        market("L", CHAOS, DIVINE, 4000, 40),
        market("L", ITEM, CHAOS, 500, 250),  # 0.5 Chaos each, 500 units
        market("L", ITEM, DIVINE, 6, 60),  # six units "sold" at 10 Divine = 1000 Chaos each
    ]}
    path = hour_path(tmp_path, "poe1", H0)
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(json.dumps(body).encode()))
    ingest(tmp_path, log=None)
    price, volume, r_price, b_price = connect(tmp_path).execute(
        "SELECT price, volume, r_price, b_price FROM prices WHERE item = ?", [ITEM]).fetchall()[0]
    assert (price, volume) == (0.5, 500), "the market that moved the units sets the price"
    assert r_price == 0.5 and b_price == 1000, "both markets stay visible as published"


def test_divine_is_priced_and_dead_markets_are_dropped(con):
    assert con.execute("SELECT count(*) FROM prices WHERE item = ?", [DIVINE]).fetchall()[0][0] > 0
    assert con.execute("SELECT count(*) FROM prices WHERE item = ?", [CHAOS]).fetchall()[0][0] == 0
    assert con.execute("SELECT count(*) FROM prices WHERE league LIKE 'Dead%'").fetchall()[0][0] == 0
    assert con.execute("SELECT count(*) FROM markets WHERE league LIKE 'Dead%'").fetchall()[0][0] > 0, "kept as published"


def test_leagues_and_league_days(con):
    leagues = {r[0]: r for r in con.execute("SELECT league, kind, hours FROM leagues").fetchall()}
    assert leagues[OLD][1] == "challenge" and leagues[OLD][2] == 20 * 24
    assert leagues[NEW][2] == 8 * 24
    day = con.execute(
        "SELECT DISTINCT league_day FROM league_prices WHERE league = ? AND hour = to_timestamp(?)",
        [NEW, H0 + (30 * 24 + 49) * HOUR],
    ).fetchall()
    assert day == [(2,)]
    assert con.execute("SELECT name, category FROM items WHERE item = ?", [CARD]).fetchall() == [("Test Card", "Divination Cards")]


def test_ingest_is_incremental_and_idempotent(tmp_path):
    hours = [H0 + h * HOUR for h in range(6)]
    write_raw(tmp_path, hours[:4])
    assert ingest(tmp_path, log=None) == {"poe1": 4}
    assert ingest(tmp_path, log=None) == {"poe1": 0}
    write_raw(tmp_path, hours[4:])
    assert ingest(tmp_path, log=None) == {"poe1": 2}
    from vaalstreet.db import connect

    con = connect(tmp_path)
    assert con.execute("SELECT count(DISTINCT hour), count(*) FROM markets").fetchall()[0] == (6, 6 * 7)
    assert con.execute("SELECT count(*) FROM prices WHERE item = ?", [ITEM]).fetchall()[0][0] == 6


def test_item_metadata_reads_sets_and_categories():
    meta = items.build({
        "Metadata/Items/DivinationCards/DivinationCardTheSephirot": {
            "name": "The Sephirot", "item_class": "DivinationCard", "tags": [],
            "properties": {"stack_size": 11, "description": "<currencyitem>{10x Divine Orb}"}},
        "Metadata/Items/Currency/CurrencyMirrorShard": {"name": "Mirror Shard", "item_class": "StackableCurrency",
                                                         "tags": ["currency_shard"], "properties": {}},
        "Metadata/Items/Currency/CurrencyDuplicate": {"name": "Mirror of Kalandra", "item_class": "StackableCurrency",
                                                       "tags": [], "properties": {}},
        "Metadata/Items/Currency/CurrencyEssenceWoe7": {"name": "Deafening Essence of Woe", "tags": ["essence"],
                                                         "item_class": "StackableCurrency", "properties": {}},
        "Metadata/Items/Currency/Hidden": {"name": "Hidden", "release_state": "unreleased"},
    })
    assert meta["Metadata/Items/DivinationCards/DivinationCardTheSephirot"]["set"] == {"need": 11, "gives": 10, "reward": "Divine Orb"}
    assert meta["Metadata/Items/Currency/CurrencyMirrorShard"]["set"] == {"need": 20, "gives": 1, "reward": "Mirror of Kalandra"}
    assert meta["Metadata/Items/Currency/CurrencyEssenceWoe7"]["category"] == "Essences"
    assert "Metadata/Items/Currency/Hidden" not in meta
    assert items.fallback_name("Metadata/Items/Currency/CurrencyRerollRare") == "Reroll Rare"


def test_snapshot_restores_the_archive_without_raw_files(archive_root, tmp_path, monkeypatch):
    from vaalstreet import cli
    from vaalstreet.db import connect

    parts, restored = tmp_path / "parts", tmp_path / "restored"
    monkeypatch.setenv("VAAL_DATA_DIR", str(archive_root))
    cli.main(["snapshot", str(parts), "--part-mb", "1"])
    assert list(parts.glob("vaal-markets.tar.000"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(restored))
    cli.main(["restore", str(parts)])
    assert not (restored / "raw").exists()
    old, new = connect(archive_root), connect(restored)
    query = "SELECT count(*), round(sum(price), 6) FROM prices"
    assert new.execute(query).fetchall() == old.execute(query).fetchall(), "prices re-derived identically"
    # Restored hours count as on disk, so a sync only fetches what came after them.
    assert archive.missing(restored, "poe1", H0, H0 + 5 * HOUR) == []
    assert archive.have(restored, "poe1") == archive.have(archive_root, "poe1")


def test_ingest_bounds_duckdb_memory(monkeypatch, tmp_path):
    """DuckDB reserves JSON buffers per thread; ingest must cap both, or many-core machines run out of memory."""
    import duckdb

    from vaalstreet import ingest as ingest_mod

    seen = {}
    real_connect = duckdb.connect

    def spy(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        seen["threads"] = con.execute("SELECT current_setting('threads')").fetchone()[0]
        return con

    monkeypatch.setattr(ingest_mod.duckdb, "connect", spy)
    ingest(tmp_path, log=None)
    assert 1 <= seen["threads"] <= 4
    assert "maximum_object_size = 268435456" not in ingest_mod._PARSE
    assert ingest_mod._MAX_DIGEST_BYTES * 2 * seen["threads"] <= 512 * 1024 * 1024
