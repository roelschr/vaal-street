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


def digest(hour: int) -> dict:
    markets = []
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
            market("Dead (PL1)", ITEM, CHAOS, 0, 0),
        ]
    return {"next_change_id": hour + HOUR, "markets": markets}


def all_hours() -> list[int]:
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
