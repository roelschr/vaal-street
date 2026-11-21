"""The ledger and the local server."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from conftest import ITEM, NEW, call
from vaalstreet.ledger import Ledger


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


def test_api_serves_every_desk_page(server):
    status, body = call(f"{server}/api/status")
    assert status == 200 and body["ready"] and body["league"] == NEW, "the live challenge league is the default"
    status, desk = call(f"{server}/api/desk")
    assert status == 200 and desk["summary"]["league_day"] == 7 and len(desk["market"]) == 4
    assert call(f"{server}/api/flips?min_margin=0.01")[1]["flips"]
    arb = call(f"{server}/api/arbitrage")[1]
    assert arb["conversions"][0]["reward"] == "Test Orb" and arb["loops"] and not arb["chaos_only"]
    poor = call(f"{server}/api/arbitrage?bankroll=30")[1]
    assert poor["chaos_only"], "a purse smaller than one Divine stays in the Chaos markets"
    small = call(f"{server}/api/flips?min_margin=0.01&bankroll=30")[1]["flips"]
    assert small and {p["kind"] for p in small} == {"chaos"} and all(p["lot_cost"] <= 30 for p in small)
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
    status, real = call(f"{server}/api/backtest", {"strategy": "swing", "capital": 2000, "whole_lots": True,
                                                    "gold_per_unit": 1, "gold_budget": 5000, "schedule": "mon-fri 17-23"})
    assert status == 200 and 0 < real["summary"]["gold_spent"] <= 5000
    assert call(f"{server}/api/backtest", {"strategy": "swing", "schedule": "whenever"})[0] == 400

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


def test_api_orders_sessions_and_recipe_checks(server):
    assert call(f"{server}/api/trade", {"item": ITEM, "side": "buy", "units": 10, "price": 9, "gold": 40})[0] == 200
    assert call(f"{server}/api/order", {"item": ITEM, "side": "buy", "market": "divine", "currency_units": 1,
                                        "item_units": 5, "units": 10, "gold": 60})[0] == 200
    ledger = call(f"{server}/api/ledger")[1]
    order = ledger["orders"][0]
    assert order["advice"] in {"keep", "re-price", "cancel"} and order["reason"] and order["name"]
    assert ledger["gold_fees"][0]["gold_per_unit"] == 5 and ledger["gold_fees"][0]["trades"] == 2
    assert call(f"{server}/api/order", {"fill": order["id"], "units": 10})[0] == 200
    ledger = call(f"{server}/api/ledger")[1]
    assert ledger["orders"][0]["advice"] == "collect" and ledger["positions"][0]["units"] == 20
    assert call(f"{server}/api/order", {"id": order["id"], "status": "closed"})[0] == 200
    assert call(f"{server}/api/order", {"item": ITEM, "side": "up", "market": "chaos", "currency_units": 1,
                                        "item_units": 1, "units": 1})[0] == 400

    assert call(f"{server}/api/session", {"expected_per_hour": 12.5, "plan": {"flips": 3}})[0] == 200
    assert call(f"{server}/api/session", {})[0] == 400, "one session at a time"
    session = call(f"{server}/api/ledger")[1]["sessions"][0]
    assert session["running"] and session["expected_per_hour"] == 12.5
    assert call(f"{server}/api/session", {"stop": session["id"], "gold_earned": 1000})[0] == 200
    assert not call(f"{server}/api/ledger")[1]["sessions"][0]["running"]

    recipes = call(f"{server}/api/ledger")[1]["recipes"]
    assert recipes and all(r["check"] is None for r in recipes)
    key = next(r["key"] for r in recipes if r["key"].startswith("set:"))
    assert call(f"{server}/api/recipe_check", {"key": key, "status": "confirmed", "note": "ok"})[0] == 200
    after = call(f"{server}/api/ledger")[1]
    assert next(r for r in after["recipes"] if r["key"] == key)["check"]["status"] == "confirmed"
    assert after["recipe_checks"][0]["label"] != key


def test_api_gold_fields_settings_and_session_plan(server):
    flips = call(f"{server}/api/flips?min_margin=0.01")[1]
    assert flips["settings"] == {"assumed_gold_fee": None, "minutes_per_order": 1.0}
    assert all(p["gold"] is None and p["minutes"] == 2 for p in flips["flips"]), "no fee until you give one"
    assert "session" not in flips, "no bankroll, no session plan"

    assert call(f"{server}/api/settings", {"assumed_gold_fee": 3, "minutes_per_order": 2})[0] == 200
    assert call(f"{server}/api/settings", {"league": "x"})[0] == 400, "only the desk's own settings"
    assert call(f"{server}/api/settings", {"minutes_per_order": -1})[0] == 400
    assert call(f"{server}/api/trade", {"item": ITEM, "side": "buy", "units": 10, "price": 9, "gold": 50})[0] == 200
    flips = call(f"{server}/api/flips?min_margin=0.01&bankroll=5000&gold_budget=1000000")[1]
    mine = next(p for p in flips["flips"] if p["id"] == ITEM)
    assert mine["gold"] == 10 and mine["gold_sources"][0]["source"] == "your trades", "a learned fee beats the assumption"
    assert all(p["gold"] == 6 for p in flips["flips"] if p["id"] != ITEM) and mine["minutes"] == 4
    session = flips["session"]
    assert session["gold_budget"] == 1000000 and session["gold_unknown"] == []
    assert all(p["chaos_per_1000_gold"] for p in session["flips"])

    arb = call(f"{server}/api/arbitrage")[1]
    assert arb["conversions"][0]["gold"] is not None and arb["loops"][0]["chaos_per_1000_gold"] is not None
    assert call(f"{server}/api/settings", {"assumed_gold_fee": ""})[0] == 200
    assert call(f"{server}/api/flips")[1]["settings"]["assumed_gold_fee"] is None
