"""A small synthetic exchange archive with known structure, written the way the real one is."""

from __future__ import annotations

import gzip
import json
import math
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from vaalstreet import items
from vaalstreet.archive import FIRST_HOUR, hour_path
from vaalstreet.ingest import CHAOS, DIVINE, ingest

HOUR = 3600
H0 = FIRST_HOUR["poe1"]
ITEM = "Metadata/Items/Currency/CurrencyTestOrb"
CARD = "Metadata/Items/DivinationCards/DivinationCardTestCard"
OTHER = "Metadata/Items/Currency/CurrencyTestShard"
EMBER = "Metadata/Items/Currency/CurrencyTestEmber"
EMBER_PREMIUM = 0.95  # unlike the orb, the ember's Divine market is 5% cheaper than its Chaos market
OLD, NEW = "Old", "New"
OLD_HOURS, NEW_START, NEW_HOURS = 20 * 24, 30 * 24, 8 * 24
PREMIUM = 1.10  # the Divine market pays 10% more than the Chaos market

# Side leagues for the league-start analytics. Known answers:
# - Flash and Blitz start on hours 200 and 210, while Old (hours 0-480) runs, and end before it: both are
#   events (family "event"), so Old stays New's only precedent. Each lasts 11 days; Flash is Blitz's precedent.
# - Hardcore New and Standard trade over New's hours. The test orb is 20% dearer in Hardcore New every
#   hour (a structural gap); the ember is 10% dearer on even days and 10% cheaper on odd ones (noise).
#   In Standard the orb is at half its New price. Divine and Chaos prices match New's.
# - The Test Lure (3 Chaos) first trades at league hour 5 in every league. In the events its price climbs
#   3 * (1 + h/72), so it rises first and is up about 67% at hour 72 from its first-day value. In New its
#   traded units grow with the league day, (1 + h/24) times the start, while Old trades it flat: by day 7
#   it is a meta-shift candidate, growing about 3x against Old's 1x.
# - In the events the Divine costs 100 + 5 * |day - 2.5| Chaos: cheapest on league day 2.
LURE = "Metadata/Items/Currency/CurrencyTestLure"
FLASH, BLITZ, HC_NEW, STANDARD = "Flash", "Blitz", "Hardcore New", "Standard"
EVENTS = ((FLASH, 200), (BLITZ, 210))
EVENT_HOURS = 11 * 24
HC_GAP = 1.20
STANDARD_GAP = 0.5
LURE_WAKE = 5


def item_price(league: str, h: int) -> float:
    """Chaos-market price of the test orb at league hour h."""
    swing = 1 + 0.05 * math.sin(2 * math.pi * h / 12)
    trend = 1 + (h / 24) / 10 if league == OLD else 1.0
    return 10 * trend * swing


def divine_price(h: int) -> float:
    return 100 + 0.25 * h


def market(league, a, b, vol_a, vol_b, stock_a=500, stock_b=500):
    if vol_a and vol_b:
        lo, hi = sorted([(vol_a, vol_b), (max(1, int(vol_a * 1.02)), vol_b)], key=lambda r: r[0] / r[1])
    else:
        lo = hi = (0, 0)
    return {
        "league": league, "market_id": f"{a}|{b}",
        "volume_traded": {a: vol_a, b: vol_b},
        "lowest_stock": {a: stock_a, b: stock_b}, "highest_stock": {a: stock_a * 2, b: stock_b * 2},
        "lowest_ratio": {a: lo[0], b: lo[1]}, "highest_ratio": {a: hi[0], b: hi[1]},
    }


def lure_units(league: str, h: int) -> int:
    if h < LURE_WAKE:
        return 0
    return round(200 * (1 + h / 24)) if league == NEW else 200


def event_divine(h: int) -> float:
    return 100 + 5 * abs(h / 24 - 2.5)


def side_markets(hour: int) -> list[dict]:
    markets = []
    for league, start in EVENTS:
        h = (hour - H0) // HOUR - start
        if not 0 <= h < EVENT_HOURS:
            continue
        div, units = event_divine(h), lure_units(league, h)
        markets += [
            market(league, CHAOS, DIVINE, round(div * 40), 40),
            market(league, ITEM, CHAOS, 1000, 10000),
            market(league, LURE, CHAOS, units, round(units * 3 * (1 + h / 72))),
        ]
    h = (hour - H0) // HOUR - NEW_START
    if 0 <= h < NEW_HOURS:
        div, p = divine_price(h), item_price(NEW, h)
        ember = 1.1 if (h // 24) % 2 == 0 else 0.9
        for league, gap, ember_gap in ((HC_NEW, HC_GAP, ember), (STANDARD, STANDARD_GAP, 1.0)):
            markets += [
                market(league, CHAOS, DIVINE, round(div * 40), 40),
                market(league, ITEM, CHAOS, 1000, round(1000 * p * gap)),
                market(league, ITEM, DIVINE, 4000, round(4000 * p * gap * PREMIUM / div)),
                market(league, EMBER, CHAOS, 2000, round(8000 * ember_gap)),
            ]
    return markets


def digest(hour: int) -> dict:
    markets = side_markets(hour)
    for league, start, length in ((OLD, 0, OLD_HOURS), (NEW, NEW_START, NEW_HOURS)):
        h = (hour - H0) // HOUR - start
        if not 0 <= h < length:
            continue
        div, p = divine_price(h), item_price(league, h)
        markets += [
            market(league, CHAOS, DIVINE, round(div * 40), 40),
            market(league, ITEM, CHAOS, 1000, round(1000 * p)),
            market(league, ITEM, DIVINE, 4000, round(4000 * p * PREMIUM / div)),
            market(league, CARD, CHAOS, 600, 900),  # 1.5 Chaos each; five make one test orb
            market(league, ITEM, OTHER, 100, 300),  # side market: 3 shards per orb
            market(league, OTHER, CHAOS, 3000, 6000),  # ...but a shard is worth 2 Chaos
            # 4 Chaos each, 5% cheaper through Divines. Selling an orb for Divines (10% dear) and buying
            # embers with them (5% cheap) is a round trip through Divine: 1.10 / 0.95 - 1 = 15.8%.
            market(league, EMBER, CHAOS, 2000, 8000),
            market(league, EMBER, DIVINE, 20000, round(20000 * 4 * EMBER_PREMIUM / div)),
            market(league, LURE, CHAOS, lure_units(league, h), 3 * lure_units(league, h)),
            market("Dead (PL1)", ITEM, CHAOS, 0, 0),
        ]
    return {"next_change_id": hour + HOUR, "markets": markets}


def all_hours() -> list[int]:
    # Old's hours already cover both events.
    return [H0 + h * HOUR for h in list(range(OLD_HOURS)) + list(range(NEW_START, NEW_START + NEW_HOURS))]


def write_raw(root, hours) -> None:
    for hour in hours:
        path = hour_path(root, "poe1", hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(digest(hour)).encode()))


@pytest.fixture(scope="session")
def archive_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    write_raw(root, all_hours())
    meta = items.build({
        CHAOS: {"name": "Chaos Orb", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {"stack_size": 20}},
        DIVINE: {"name": "Divine Orb", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {"stack_size": 20}},
        ITEM: {"name": "Test Orb", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {"stack_size": 20}},
        OTHER: {"name": "Test Shard", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {}},
        LURE: {"name": "Test Lure", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {}},
        EMBER: {"name": "Test Ember", "item_class": "StackableCurrency", "tags": ["currency"], "properties": {}},
        CARD: {"name": "Test Card", "item_class": "DivinationCard", "tags": ["divination_card"],
               "properties": {"stack_size": 5, "description": "<currencyitem>{Test Orb}"}},
    })
    target = root / "items" / "poe1.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"version": items.VERSION, "items": meta}))
    items.load.cache_clear()
    ingest(root, log=None)
    return root


@pytest.fixture()
def con(archive_root):
    from vaalstreet.db import connect

    connection = connect(archive_root)
    yield connection
    connection.close()


@pytest.fixture()
def server(archive_root, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App, make_handler

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)  # keep the shared fixture clean
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")
