"""Alerts on plans, round trips, your orders and league precedents, the event feed, and the migration.

Known answers on the synthetic archive (league "New"): the test orb's best flip is the cross flip,
about 10% margin and 48.7 Chaos an hour; the shard -> orb round trip has a 78% edge with about
5,258 Chaos resting at its thinnest swap; the orb is 41% cheaper than the same day of league "Old",
and past leagues moved it a median +5% over the next 3 days.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import ITEM, OTHER, call
from vaalstreet.ledger import ALERT_COOLDOWN, TITLE_LENGTH, Ledger, describe_alert
from vaalstreet.paths import ledger_file
from vaalstreet.server import App

HOUR = 3600


@pytest.fixture
def app(archive_root, tmp_path):
    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    return app


def fire(app):
    desk = app.desk("New")
    rows = {row["id"]: row for row in desk.market()}
    return app.ledger.evaluate_alerts("New", rows, desk.as_of, app._alert_context(desk))


def titles(fired):
    return [f["title"] for f in fired]


def test_each_kind_fires_on_the_known_answer_and_not_past_it(app):
    ledger = app.ledger
    hits = [
        ledger.add_alert("New", kind="plan", params={"item": ITEM, "min_margin": 9, "min_profit": 40}),
        ledger.add_alert("New", kind="trip", params={"item": OTHER, "min_edge": 70, "min_resting": 5000}),
        ledger.add_alert("New", kind="precedent", params={"item": ITEM, "mode": "standing", "direction": "cheap",
                                                          "min_gap": 40}),
        ledger.add_alert("New", kind="precedent", params={"item": ITEM, "mode": "playbook", "horizon": 3,
                                                          "direction": "up", "min_move": 5}),
    ]
    misses = [
        # The orb's best plan buys at "19 Chaos for 2" and sells at "1 Divine for 14": 11.1% as posted
        # (10.1% at the levels those orders round).
        ledger.add_alert("New", kind="plan", params={"item": ITEM, "min_margin": 12}),
        ledger.add_alert("New", kind="plan", params={"min_margin": 2, "min_profit": 60}),
        ledger.add_alert("New", kind="trip", params={"min_edge": 80}),
        ledger.add_alert("New", kind="trip", params={"item": OTHER, "min_resting": 6000}),
        ledger.add_alert("New", kind="trip", params={"item": ITEM, "min_edge": 50}),  # the orb trip has 16%
        ledger.add_alert("New", kind="precedent", params={"item": ITEM, "mode": "standing", "direction": "dear",
                                                          "min_gap": 1}),
        ledger.add_alert("New", kind="precedent", params={"item": ITEM, "mode": "standing", "min_gap": 45}),
        ledger.add_alert("New", kind="precedent", params={"item": ITEM, "mode": "playbook", "horizon": 3,
                                                          "direction": "down", "min_move": 1}),
    ]
    fired = fire(app)
    assert sorted(f["alert"] for f in fired) == sorted(hits)
    assert not set(misses) & {f["alert"] for f in fired}
    assert "Test Orb" in " ".join(titles(fired))
    assert all(len(t) <= TITLE_LENGTH for t in titles(fired))
    assert fire(app) == [], "the same conditions an hour later do not ring again"


def test_order_alert_says_what_to_do_aloud(app):
    ledger = app.ledger
    ledger.add_alert("New", kind="order")
    order = ledger.add_order("New", ITEM, "buy", "chaos", 1, 1, 10)
    keep_or_not = {o["id"]: o["advice"] for o in app._advise(app.desk("New"))}
    assert keep_or_not[order] != "keep", "1 Chaos for an orb worth ~4 is far from the plan"
    assert len(fire(app)) == 1
    ledger.fill_order(order, 10, record_trade=False)
    fired = fire(app)
    assert titles(fired) == ["Test Orb filled, post the sell"], "a new advice is a new condition"


def test_no_repeat_until_cleared_and_not_within_the_cool_down(tmp_path):
    ledger = Ledger(tmp_path)
    alert = ledger.add_alert("L", kind="plan", params={"min_margin": 5})
    plan = {"id": "orb", "name": "Orb", "kind": "chaos", "margin": 0.06, "profit_per_hour": 10, "buy": 1, "sell": 1.1}
    on, off = {"flips": [plan]}, {"flips": []}
    assert len(ledger.evaluate_alerts("L", {}, 0, on)) == 1
    assert ledger.evaluate_alerts("L", {}, HOUR, on) == [], "still there: no repeat"
    assert ledger.evaluate_alerts("L", {}, 2 * HOUR, off) == []
    assert ledger.evaluate_alerts("L", {}, 3 * HOUR, on) == [], "came back inside the cool-down"
    assert ledger.evaluate_alerts("L", {}, 4 * HOUR, {}) == [], "no plans computed: state kept"
    assert ledger.alerts("L")[0]["state"]["hot"] == ["orb:chaos"]
    ledger.evaluate_alerts("L", {}, ALERT_COOLDOWN + HOUR, off)
    assert len(ledger.evaluate_alerts("L", {}, ALERT_COOLDOWN + 2 * HOUR, on)) == 1, "cleared, back, cool-down over"
    other = dict(plan, id="shard", name="Shard")
    fired = ledger.evaluate_alerts("L", {}, ALERT_COOLDOWN + 3 * HOUR, {"flips": [plan, other]})
    assert titles(fired) == ["Flip Shard, 6% margin"], "only the new item fires"
    assert alert == ledger.alerts("L")[0]["id"]


def test_alert_rules_are_checked_and_described_in_plain_words(tmp_path):
    ledger = Ledger(tmp_path)
    for kind, params, message in [
        ("nonsense", {}, "Unknown alert kind"),
        ("plan", {"min_margin": "lots"}, "margin must be a number"),
        ("trip", {"min_edge": -1}, "edge must be 0 or more"),
        ("precedent", {}, "needs an item"),
        ("precedent", {"item": "orb", "mode": "playbook", "horizon": 2.5}, "whole number"),
    ]:
        with pytest.raises(ValueError, match=message):
            ledger.add_alert("L", kind=kind, params=params)
    ledger.add_alert("L", kind="trip", params={"item": "orb", "min_edge": 4, "min_resting": 300})
    text = describe_alert(ledger.alerts("L")[0], {"orb": "Orb"})
    assert text == ("When a round trip starting with Orb shows an edge of 4% or more and its thinnest swap "
                    "has orders worth 300 Chaos or more waiting.")


def test_alert_api_and_event_feed(server):
    status, out = call(f"{server}/api/alert", {"league": "New", "kind": "plan", "params": {"min_margin": "x"}})
    assert status == 400 and "margin must be a number" in out["error"]
    assert call(f"{server}/api/alert", {"league": "New", "kind": "price", "item": ITEM})[0] == 400
    assert call(f"{server}/api/alert", {"league": "New", "kind": "order"})[0] == 200
    assert call(f"{server}/api/alert", {"league": "New", "item": ITEM, "field": "price", "op": "above",
                                        "threshold": 0})[0] == 200
    alerts = call(f"{server}/api/alerts?league=New")[1]["alerts"]
    assert [a["kind"] for a in alerts] == ["order", "price"]
    assert alerts[0]["description"].startswith("When one of your open orders")
    assert "Test Orb" in alerts[1]["description"]

    feed = call(f"{server}/api/events?league=New&since=0")[1]
    assert feed == {"events": [], "last": 0}
    assert call(f"{server}/api/events?since=abc")[0] == 400


def test_events_since_returns_only_new_events_oldest_first(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.add_alert("L", "orb", "price", "above", 1)
    ledger.add_alert("L", "shard", "price", "above", 1)
    rows = {"orb": {"name": "Orb", "price": 2}, "shard": {"name": "Shard", "price": 2}}
    ledger.evaluate_alerts("L", rows, 1)
    first, second = ledger.events_since("L", 0)
    assert first["id"] < second["id"] and first["title"] == "Orb price above 1"
    assert [e["id"] for e in ledger.events_since("L", first["id"])] == [second["id"]]
    assert ledger.events_since("L", second["id"]) == []


def test_alerts_table_from_the_current_schema_gains_kind_and_params(tmp_path):
    db = sqlite3.connect(ledger_file(tmp_path))
    db.executescript("""
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
    field TEXT NOT NULL, op TEXT NOT NULL CHECK(op IN ('above','below')), threshold REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, hot INTEGER NOT NULL DEFAULT 0);
""")  # copied verbatim from the schema before alert kinds
    db.execute("INSERT INTO alerts(league, item, field, op, threshold, hot) VALUES ('L','orb','price','above',5,1)")
    db.commit()
    db.close()
    Ledger(tmp_path)
    ledger = Ledger(tmp_path)  # twice: the migration runs again safely
    old, = ledger.alerts("L")
    assert old["kind"] == "price" and old["params"] == {} and old["hot"] == 1
    assert ledger.evaluate_alerts("L", {"orb": {"name": "Orb", "price": 6}}, 1) == [], "was already above"
    ledger.add_alert("L", kind="order")
    assert [a["kind"] for a in ledger.alerts("L")] == ["price", "order"]


def test_event_feed_over_http_returns_a_real_event(app):
    import threading
    from http.server import ThreadingHTTPServer

    from vaalstreet.server import make_handler

    order = app.ledger.add_order("New", ITEM, "buy", "chaos", 1, 1, 10)
    app.ledger.fill_order(order, 10, record_trade=False)
    app.ledger.add_alert("New", kind="order")
    fire(app)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/api/events?league=New"
        feed = call(f"{url}&since=0")[1]
        event, = feed["events"]
        assert event["title"] == "Test Orb filled, post the sell" and feed["last"] == event["id"]
        assert event["body"]
        assert call(f"{url}&since={event['id']}")[1] == {"events": [], "last": event["id"]}
    finally:
        httpd.shutdown()
