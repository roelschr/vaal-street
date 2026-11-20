"""The ledger and the local server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import ITEM, NEW
from vaalstreet.ledger import Ledger
from vaalstreet.server import App, make_handler


def test_ledger_average_cost_and_realised_profit(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.add_trade("L", "orb", "buy", 10, 2.0, t=1)
    ledger.add_trade("L", "orb", "buy", 10, 4.0, t=2)
    ledger.add_trade("L", "orb", "sell", 5, 5.0, t=3)
    pos = ledger.positions("L")["orb"]
    assert pos["units"] == 15 and pos["cost"] == pytest.approx(45.0) and pos["realized"] == pytest.approx(10.0)
    with pytest.raises(ValueError):
        ledger.add_trade("L", "orb", "sell", 16, 5.0)
    with pytest.raises(ValueError):
        ledger.add_trade("L", "orb", "buy", -1, 5.0)
    assert ledger.positions("Other") == {}
    trade_id = ledger.trades("L")[-1]["id"]
    ledger.delete_trade(trade_id)
    assert ledger.positions("L")["orb"]["units"] == 20
    assert Ledger(tmp_path).positions("L")["orb"]["units"] == 20, "survives a restart"


def test_alerts_fire_once_per_crossing_and_rearm(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.add_alert("L", "orb", "price", "above", 10)
    row = lambda price: {"orb": {"name": "Orb", "price": price}}
    assert ledger.evaluate_alerts("L", row(9), 1) == []
    assert len(ledger.evaluate_alerts("L", row(11), 2)) == 1
    assert ledger.evaluate_alerts("L", row(12), 3) == [], "still above: no repeat"
    assert ledger.evaluate_alerts("L", row(9), 4) == []
    assert len(ledger.evaluate_alerts("L", row(11), 5)) == 1, "re-armed after coming back"
    assert len(ledger.events("L")) == 2
    with pytest.raises(ValueError):
        ledger.add_alert("L", "orb", "nonsense", "above", 1)
    ledger.watch("L", "orb", True)
    ledger.watch("L", "orb", True)
    assert ledger.watchlist("L") == ["orb"]
    ledger.watch("L", "orb", False)
    assert ledger.watchlist("L") == []
    ledger.set("league", "L")
    assert ledger.get("league") == "L" and ledger.get("missing", 3) == 3


@pytest.fixture()
def server(archive_root, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
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


def test_api_serves_every_desk_page(server):
    status, body = call(f"{server}/api/status")
    assert status == 200 and body["ready"] and body["league"] == NEW, "the live challenge league is the default"
    status, desk = call(f"{server}/api/desk")
    assert status == 200 and desk["summary"]["league_day"] == 7 and len(desk["market"]) == 4
    assert call(f"{server}/api/flips?min_margin=0.01")[1]["flips"]
    arb = call(f"{server}/api/arbitrage")[1]
    assert arb["conversions"][0]["reward"] == "Test Orb" and arb["swaps"]
    league = call(f"{server}/api/league?unit=chaos&horizon=3")[1]
    assert league["precedents"] == ["Old"] and league["playbook"]["horizon"] == 3 and "New" in league["divine"]
    item = call(f"{server}/api/item?id=Test%20Orb&hours=24")[1]
    assert item["series"]["id"] == ITEM and {p["kind"] for p in item["flips"]} >= {"cross"}
    assert call(f"{server}/api/item?id=Nope")[0] == 404
    assert call(f"{server}/api/curve?id=Test%20Orb")[1]["curve"]["Old"]


def test_api_backtest_and_ledger_round_trip(server):
    status, result = call(f"{server}/api/backtest", {"strategy": "swing", "capital": 2000, "attention": 2})
    assert status == 200 and result["summary"]["return"] > 0 and len(result["t"]) == len(result["equity"])
    assert call(f"{server}/api/backtest", {"strategy": "/etc/passwd:X"})[0] == 400, "no arbitrary modules over HTTP"
    assert call(f"{server}/api/backtest", {"strategy": "swing", "params": {"bogus": 1}})[0] == 400

    assert call(f"{server}/api/trade", {"item": ITEM, "side": "buy", "units": 10, "price": 9})[0] == 200
    assert call(f"{server}/api/trade", {"item": ITEM, "side": "sell", "units": 99, "price": 9})[0] == 400
    assert call(f"{server}/api/watch", {"item": ITEM})[0] == 200
    assert call(f"{server}/api/alert", {"item": ITEM, "field": "price", "op": "above", "threshold": 1})[0] == 200
    ledger = call(f"{server}/api/ledger")[1]
    position = ledger["positions"][0]
    assert position["units"] == 10 and position["unrealized"] == pytest.approx((position["mark"] - 9) * 10, rel=1e-4)
    assert ledger["watchlist"] == [ITEM] and len(ledger["alerts"]) == 1
    assert next(r for r in call(f"{server}/api/desk")[1]["market"] if r["id"] == ITEM)["watched"]


def test_server_refuses_other_sites_and_serves_the_app(server):
    assert call(f"{server}/api/status", headers={"Origin": "https://evil.example"})[0] == 403
    assert call(f"{server}/api/trade", {"item": ITEM}, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(f"{server}/api/status", headers={"Host": "evil.example"})[0] == 403, "DNS rebinding"
    with urllib.request.urlopen(f"{server}/") as response:
        assert b"Vaal Street" in response.read()
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{server}/../pyproject.toml")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{server}/%2e%2e/%2e%2e/pyproject.toml")
