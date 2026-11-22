"""The ledger and the local server."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from conftest import CARD, ITEM, NEW, OTHER, call
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
    assert status == 200 and desk["summary"]["league_day"] == 7 and len(desk["market"]) == 7  # with the Test Lure and the Test Blade card
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



def test_api_records_an_item_you_made_so_it_can_be_sold(server):
    # 6 cards bought at 5 make 2 orbs that cost 15 each; one sold at 20 realises 5.
    assert call(f"{server}/api/trade", {"item": CARD, "side": "buy", "units": 6, "price": 5})[0] == 200
    status, answer = call(f"{server}/api/trade", {"item": ITEM, "side": "sell", "units": 1, "price": 20})
    assert status == 400 and "Record items you made" in answer["error"]
    status, answer = call(f"{server}/api/made", {"inputs": [["Test Card", 7]], "output": "Test Orb"})
    assert status == 400 and "Cannot use 7 Test Card: the ledger holds 6" in answer["error"]
    assert call(f"{server}/api/made", {"inputs": [["No Such Thing", 1]], "output": ITEM})[0] == 400
    assert call(f"{server}/api/made", {"inputs": [["test card", 6]], "output": ITEM, "gives": 2, "note": "set"})[0] == 200
    assert call(f"{server}/api/trade", {"item": ITEM, "side": "sell", "units": 1, "price": 20})[0] == 200
    ledger = call(f"{server}/api/ledger")[1]
    [made] = ledger["made"]
    assert made["name"] == "Test Orb" and made["gives"] == 2 and made["note"] == "set"
    assert made["inputs"] == [{"id": CARD, "name": "Test Card", "units": 6}]
    orb = next(p for p in ledger["positions"] if p["id"] == ITEM)
    assert orb["units"] == 1 and orb["average"] == pytest.approx(15) and orb["realized"] == pytest.approx(5)
    assert ledger["totals"]["cost"] == pytest.approx(15) and ledger["totals"]["realized"] == pytest.approx(5)
    assert call(f"{server}/api/made", {"delete": made["id"]})[0] == 200
    assert call(f"{server}/api/ledger")[1]["made"] == []
    # An item the ledger holds can be used even when neither the market nor the item export knows its id.
    gone = "Metadata/Items/Currency/CurrencyNoLongerTraded"
    assert call(f"{server}/api/trade", {"item": gone, "side": "buy", "units": 3, "price": 2})[0] == 200
    assert call(f"{server}/api/made", {"inputs": [[gone, 3]], "output": ITEM})[0] == 200
    assert call(f"{server}/api/ledger")[1]["made"][0]["inputs"][0]["id"] == gone


def test_api_edits_a_trade(server):
    assert call(f"{server}/api/trade", {"item": ITEM, "side": "buy", "units": 10, "price": 9})[0] == 200
    assert call(f"{server}/api/trade", {"item": ITEM, "side": "buy", "units": 4, "price": 11})[0] == 200
    wrong = call(f"{server}/api/ledger")[1]["trades"][0]  # newest first
    assert call(f"{server}/api/trade", {"edit": wrong["id"], "side": "sell"})[0] == 200
    ledger = call(f"{server}/api/ledger")[1]
    assert ledger["trades"][0]["side"] == "sell" and ledger["positions"][0]["units"] == 6
    assert ledger["positions"][0]["realized"] == pytest.approx(4 * (11 - 9))
    status, answer = call(f"{server}/api/trade", {"edit": wrong["id"], "units": 99})
    assert status == 400 and "more than the ledger held" in answer["error"]

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


def test_api_flips_filters_by_market_before_it_cuts_the_list(server):
    """The Flips page re-ranks and searches the list it gets, so the server must not cut it to the most
    profitable plans first, and a market filter must apply before any cut a caller asks for."""
    every = call(f"{server}/api/flips?min_margin=0.01")[1]["flips"]
    kinds = [p["kind"] for p in every]
    assert len(set(kinds)) > 1 and kinds[0] != "divine", "the archive ranks another kind of plan first"
    divine = call(f"{server}/api/flips?min_margin=0.01&kind=divine&limit=1")[1]["flips"]
    assert [p["kind"] for p in divine] == ["divine"]
    assert len(call(f"{server}/api/flips?min_margin=0.01&limit=1")[1]["flips"]) == 1


def test_api_gold_fields_settings_and_session_plan(flat_server):
    server = flat_server  # a flat Divine, so some flip beats holding Divines and makes the session plan
    flips = call(f"{server}/api/flips?min_margin=0.01")[1]
    assert flips["settings"] == {"assumed_gold_fee": None, "minutes_per_order": 1.0, "ninja_prices": 1.0}
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
    assert session["flips"] and all(p["chaos_per_1000_gold"] for p in session["flips"])
    tight = call(f"{server}/api/flips?min_margin=0.01&bankroll=5000&gold_budget=500")[1]["session"]
    assert tight["gold_used"] <= 500 + 1e-9 and tight["expected_per_hour"] < session["expected_per_hour"]

    arb = call(f"{server}/api/arbitrage")[1]
    assert arb["conversions"][0]["gold"] is not None and arb["loops"][0]["chaos_per_1000_gold"] is not None
    assert call(f"{server}/api/settings", {"assumed_gold_fee": ""})[0] == 200
    assert call(f"{server}/api/flips")[1]["settings"]["assumed_gold_fee"] is None


def test_api_your_recipes_and_vendor_prices_with_their_checks(server):
    from vaalstreet.ingest import CHAOS

    arb = call(f"{server}/api/arbitrage")[1]
    assert arb["fixed"] == [] and arb["swaps"] == 3
    assert all(row["check"] == "unchecked" for row in arb["conversions"] + arb["loops"])
    # Made up: 2 cards and 1 shard make 1 orb; a vendor sells an orb for 5 Chaos.
    assert call(f"{server}/api/recipe", {"kind": "recipe", "inputs": [[CARD, 2], [OTHER, 1]], "output": ITEM,
                                         "note": "try once"})[0] == 200
    assert call(f"{server}/api/recipe", {"kind": "vendor", "inputs": [[CHAOS, 5]], "output": ITEM})[0] == 200
    assert call(f"{server}/api/recipe", {"kind": "vendor", "inputs": [], "output": ITEM})[0] == 400
    arb = call(f"{server}/api/arbitrage")[1]
    assert [f["kind"] for f in arb["fixed"]] == ["recipe", "vendor"]
    assert arb["fixed"][0]["inputs"][0]["name"] == "Test Card" and arb["fixed"][0]["check"] == "unchecked"
    yours = next(r for r in arb["conversions"] if r["how"] == "yours")
    assert yours["check"] == "unchecked" and yours["note"] == "try once"
    vendor_loop = next(r for r in arb["loops"] if r["via"] == ["vendor"])
    assert vendor_loop["vendor_checks"] == {f"vendor:5*{CHAOS}>1*{ITEM}": "unchecked"}

    assert call(f"{server}/api/recipe_check", {"key": yours["recipe_key"], "status": "confirmed"})[0] == 200
    assert call(f"{server}/api/recipe_check", {"key": vendor_loop["recipe_key"], "status": "failed",
                                               "note": "vendor was out"})[0] == 200
    arb = call(f"{server}/api/arbitrage")[1]
    assert next(r for r in arb["conversions"] if r["how"] == "yours")["check"] == "confirmed"
    assert arb["fixed"][0]["check"] == "confirmed"
    failed = next(r for r in arb["loops"] if r["via"] == ["vendor"])
    assert failed["check"] == "failed" and failed["check_note"] == "vendor was out"
    labels = {r["key"]: r["label"] for r in call(f"{server}/api/ledger")[1]["recipes"]}
    assert labels[f"vendor:5*{CHAOS}>1*{ITEM}"] == "5 Chaos Orb buys 1 Test Orb"

    for f in arb["fixed"]:
        assert call(f"{server}/api/recipe", {"delete": f["id"]})[0] == 200
    assert call(f"{server}/api/arbitrage")[1]["fixed"] == []
    # The checks outlive the recipes; they are named from their keys, never shown as raw keys.
    checks = {c["key"]: c["label"] for c in call(f"{server}/api/ledger")[1]["recipe_checks"]}
    assert checks[yours["recipe_key"]] == "2 Test Card + 1 Test Shard → 1 Test Orb"
    assert all(":" not in label for label in checks.values())


def test_api_recipe_takes_names_or_ids(server):
    # The desk's form sends names; Chaos Orb never appears in the market list but the item export knows it.
    assert call(f"{server}/api/recipe", {"kind": "vendor", "inputs": [["chaos orb", 12]], "output": "Test Orb"})[0] == 200
    assert call(f"{server}/api/recipe", {"kind": "recipe", "inputs": [[CARD, 5]], "output": ITEM, "gives": 1})[0] == 200
    status, body = call(f"{server}/api/recipe", {"kind": "recipe", "inputs": [["No Such Thing", 1]], "output": "Test Orb"})
    assert status == 400 and "No item called" in body["error"]
    fixed = call(f"{server}/api/arbitrage")[1]["fixed"]
    assert [f["output"] for f in fixed] == [ITEM, ITEM]
    assert fixed[0]["inputs"][0]["name"] == "Chaos Orb" and fixed[0]["check"] == "unchecked"


def test_api_strategy_export_matches_the_cli_file(server):
    from vaalstreet import sharing

    body = {"strategy": "swing", "params": {"reach": 0.3}, "options": {"capital": 500, "whole_lots": True},
            "results": [{"league": NEW, "return": 0.1}]}
    status, doc = call(f"{server}/api/strategy_export", body)
    assert status == 200 and doc["format"] == sharing.FORMAT and doc["params"] == {"reach": 0.3}
    assert doc == sharing.document("swing", body["params"], body["options"], body["results"])
    assert call(f"{server}/api/strategy_export", {"strategy": "my.py:Mine"})[0] == 400


def test_market_change_is_blank_when_the_window_starts_before_the_item_traded(con, archive_root):
    # New runs 8 days (192 hours), so the 7-day change compares with hours 21-23 and the 24-hour
    # change with hours 165-167. A change needs its window to start SETTLE_HOURS (24) after the
    # item's first trade: the orb trades from hour 0, so 21 < 24 already blanks the 7-day change
    # (the Covens day-7 case), while the 24-hour change stays.
    from vaalstreet.desk import Desk

    def row(desk):
        return next(r for r in desk.market() if r["id"] == ITEM)

    from vaalstreet.desk import SETTLE_HOURS

    desk = Desk.build(con, NEW, root=archive_root)
    T = len(desk.panel.hours)
    assert T - 3 - 7 * 24 == 21 < SETTLE_HOURS == 24
    fresh = row(desk)
    assert fresh["change_7d"] is None and fresh["change_24h"] is not None
    # First trade at hour 142: the 24-hour window (from hour 165) starts 23 hours later, too soon.
    desk = Desk.build(con, NEW, root=archive_root)
    desk.panel["price"][:142, desk.panel.index(ITEM)] = float("nan")
    assert row(desk)["change_24h"] is None
    # First trade at hour 141: exactly 24 hours before the window, so the change is shown.
    desk = Desk.build(con, NEW, root=archive_root)
    desk.panel["price"][:141, desk.panel.index(ITEM)] = float("nan")
    assert row(desk)["change_24h"] is not None


def test_the_sync_thread_warms_the_slow_answers(flat_root, tmp_path):
    """After a rebuild the flip replays and the ladder's replays at the stake are already computed:
    the pages' first requests add nothing to the desk's replay cache."""
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(flat_root, sync=False)
    app.ledger = Ledger(tmp_path)
    app._warm()
    desk = app.desk()
    warmed = set(desk._replays)
    assert (0.1, 7) in warmed and any(k[0] == "at" for k in warmed if isinstance(k, tuple) and k)
    app.api_flips({})
    app.api_ladder({})
    assert set(app.desk()._replays) == warmed and app.desk() is desk
