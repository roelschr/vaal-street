import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vaalstreet import archive
from vaalstreet.db import connect
from vaalstreet.ingest import ingest, parse_digest

CUR = "Metadata/Items/Currency/"
CHAOS, DIVINE, MIRROR, FUSING = (CUR + n for n in ("CurrencyRerollRare", "CurrencyModValues", "CurrencyDuplicate", "CurrencyRerollSocketLinks"))
HOUR0 = 1722024000  # 2024-07-26 20:00 UTC


def market(league, a, b, vol, lo, hi, stock_lo=(0, 0), stock_hi=(0, 0)):
    pair = lambda v: {a: v[0], b: v[1]}
    return {
        "league": league, "market_id": f"{a}|{b}", "market_pair": [a, b],
        "volume_traded": pair(vol), "lowest_ratio": pair(lo), "highest_ratio": pair(hi),
        "lowest_stock": pair(stock_lo), "highest_stock": pair(stock_hi),
    }


def digest(next_id, markets):
    return json.dumps({"next_change_id": next_id, "markets": markets}).encode()


@pytest.fixture
def data(tmp_path):
    hours = {
        HOUR0: [
            market("Test", CHAOS, DIVINE, (2000, 10), (180, 1), (220, 1), (5, 1), (9, 3)),
            market("Test", DIVINE, MIRROR, (300, 1), (290, 1), (310, 1)),  # only tradable for divines
            market("Test", FUSING, CHAOS, (40, 10), (3, 1), (5, 1)),
            market("Test", CHAOS, MIRROR, (0, 0), (0, 0), (0, 0), (10, 0), (10, 0)),  # stock only, no trades
            market("Mine (PL123)", CHAOS, DIVINE, (190, 1), (190, 1), (190, 1)),
        ],
        HOUR0 + 30 * 3600: [market("Test", CHAOS, DIVINE, (2100, 10), (200, 1), (215, 1))],
    }
    for hour, markets in hours.items():
        month = tmp_path / "raw" / "poe1" / "2024-07"
        month.mkdir(parents=True, exist_ok=True)
        (month / f"{hour}.json.gz").write_bytes(gzip.compress(digest(hour + 3600, markets)))
    assert ingest(tmp_path) == {"poe1": 1}
    return tmp_path


def price_row(con, item, hour=HOUR0):
    return con.execute(
        "SELECT price, price_lo, price_hi, volume, volume_ref, via FROM prices "
        "WHERE league = 'Test' AND item = ? AND epoch(hour) = ?", [item, hour]
    ).fetchone()


def test_parse_keeps_exact_large_integers():
    big = 12345678901234567890
    rows = parse_digest(digest(HOUR0 + 3600, [market("L", CHAOS, DIVINE, (big, 1), (1, 1), (1, 1))]), HOUR0)
    assert rows[0]["vol_a"] == big


def test_direct_price_and_band_in_either_orientation(data):
    con = connect(data)
    # Published as chaos|divine: 180..220 chaos per divine, 2000 chaos for 10 divine.
    assert price_row(con, DIVINE) == (200.0, 180.0, 220.0, 10, 2000.0, CHAOS)
    # Published as fusing|chaos with ratios 3:1 and 5:1, so a fusing costs 1/5..1/3 chaos.
    price, low, high, volume, volume_ref, via = price_row(con, FUSING)
    assert (price, volume, volume_ref, via) == (0.25, 40, 10.0, CHAOS)
    assert (low, high) == pytest.approx((0.2, 1 / 3))


def test_item_without_reference_market_is_priced_through_divine(data):
    price, low, high, volume, volume_ref, via = price_row(connect(data), MIRROR)
    assert (price, low, high) == (300 * 200.0, 290 * 200.0, 310 * 200.0)
    assert (volume, volume_ref, via) == (1, 60000.0, DIVINE)


def test_stock_only_markets_do_not_create_prices(data):
    con = connect(data)
    assert con.execute("SELECT count(*) FROM markets WHERE vol_a = 0").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM prices WHERE via = ? AND item = ?", [CHAOS, MIRROR]).fetchone()[0] == 0


def test_leagues_and_league_day(data):
    con = connect(data)
    assert con.execute("SELECT league, is_private, hours FROM leagues ORDER BY league").fetchall() == [
        ("Mine (PL123)", True, 1), ("Test", False, 2)]
    assert con.execute(
        "SELECT league_hour, league_day FROM league_prices WHERE league = 'Test' AND item = ? ORDER BY hour", [DIVINE]
    ).fetchall() == [(0, 0), (30, 1)]


def test_names_use_export_then_fall_back_to_id(data):
    (data / "items").mkdir()
    (data / "items" / "poe1.json").write_text(json.dumps({DIVINE: {"name": "Divine Orb", "class": "StackableCurrency"}}))
    names = dict(connect(data).execute("SELECT DISTINCT item, name FROM prices").fetchall())
    assert names[DIVINE] == "Divine Orb"
    assert names[FUSING] == "Reroll Socket Links"


def test_ingest_only_rebuilds_changed_months(data):
    assert ingest(data) == {"poe1": 0}


def test_connect_without_data_explains_what_to_run(tmp_path):
    with pytest.raises(FileNotFoundError, match="ingest"):
        connect(tmp_path)


@pytest.fixture
def exchange_server():
    """Mimics the endpoint: 3 published hours, one 429 on the way."""
    tip = HOUR0 + 3 * 3600
    state = {"throttled": False, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            tail = self.path.rsplit("/", 1)[-1]
            hour = int(tail) if tail.isdigit() else HOUR0
            if hour == HOUR0 + 3600 and not state["throttled"]:
                state["throttled"] = True
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.end_headers()
                return
            body = digest(hour if hour >= tip else hour + 3600, [] if hour >= tip else [market("L", CHAOS, DIVINE, (1, 1), (1, 1), (1, 1))])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/api/currency-exchange", state
    server.shutdown()


def test_archive_walks_to_the_tip_survives_throttling_and_resumes(tmp_path, exchange_server):
    url, state = exchange_server
    assert archive.archive_realm(tmp_path, "poe2", base_url=url, delay=0) == 3
    saved = sorted(p.name for p in (tmp_path / "raw" / "poe2").rglob("*.json.gz"))
    assert saved == [f"{HOUR0 + i * 3600}.json.gz" for i in range(3)]
    assert state["requests"][0].endswith("/poe2")  # first call asks for the first published hour
    assert archive.archive_realm(tmp_path, "poe2", base_url=url, delay=0) == 0
