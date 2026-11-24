"""How long an order has stood, and what its re-posts cost, measured with the ledger's own clock."""

from __future__ import annotations

import pytest

from conftest import ITEM, NEW
from vaalstreet.ledger import Ledger

HOUR = 3600
T0 = 1_700_000_000


@pytest.fixture()
def clock():
    return [T0]


@pytest.fixture()
def app(archive_root, tmp_path, clock):
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path, clock=lambda: clock[0])
    return app


def _rows(app) -> dict[int, dict]:
    return {o["id"]: o for o in app.api_ledger({"league": NEW})["orders"]}


def test_a_fresh_order_has_an_age_and_no_history(app, clock):
    """Posted at T0, looked at 5 hours later: it has stood 5 hours, and nothing came before it."""
    oid = app.ledger.add_order(NEW, ITEM, "buy", "chaos", 9, 1, 10, gold=50, t=T0)
    clock[0] = T0 + 5 * HOUR
    row = _rows(app)[oid]
    assert row["age"] == 5 * HOUR and row["listed"] is None and row["earlier"] == []
    assert row["chain_gold"] == 50 and row["chain_gold_known"]
    clock[0] = T0 - 60  # a clock behind the order (a rehearsal restarted): never a negative age
    assert _rows(app)[oid]["age"] == 0


def test_a_chain_of_three_counts_from_its_first_order(app, clock):
    """9 for 4 posted at T0 (Gold 400), re-posted at 7 for 3 two hours later (Gold 500), then at 2 for 1
    three hours after that (Gold 300). Looked at 10 hours after T0: the last order has stood 5 hours, the
    listing 10; its earlier ratios are 9 for 4, then 7 for 3; posting cost 400 + 500 + 300 = 1,200 Gold."""
    first = app.ledger.add_order(NEW, ITEM, "sell", "chaos", 4, 9, 18, gold=400, t=T0)
    second = app.ledger.repost_order(first, 3, 7, 14, gold=500, t=T0 + 2 * HOUR)
    third = app.ledger.repost_order(second, 1, 2, 14, gold=300, t=T0 + 5 * HOUR)
    assert [o["id"] for o in app.ledger.order_chain(third)] == [first, second]
    assert [o["id"] for o in app.ledger.order_chain(second)] == [first]
    assert app.ledger.order_chain(first) == []
    clock[0] = T0 + 10 * HOUR
    rows = _rows(app)
    assert list(rows) == [third], "the orders it replaced are cancelled"
    row = rows[third]
    assert row["age"] == 5 * HOUR and row["listed"] == 10 * HOUR
    assert row["earlier"] == [{"item_units": 9, "currency_units": 4}, {"item_units": 7, "currency_units": 3}]
    assert row["chain_gold"] == 1200 and row["chain_gold_known"]


def test_gold_on_posting_is_a_floor_when_some_is_missing(app, clock):
    """The same chain with no Gold recorded for the middle order: 400 + 300 = 700, at least."""
    first = app.ledger.add_order(NEW, ITEM, "sell", "chaos", 4, 9, 18, gold=400, t=T0)
    second = app.ledger.repost_order(first, 3, 7, 14, t=T0 + HOUR)
    third = app.ledger.repost_order(second, 1, 2, 14, gold=300, t=T0 + 2 * HOUR)
    row = _rows(app)[third]
    assert row["chain_gold"] == 700 and not row["chain_gold_known"]
    # And with no Gold anywhere in the chain, there is no sum to show.
    bare = app.ledger.repost_order(third, 1, 2, 14, t=T0 + 3 * HOUR)
    app.ledger._run("UPDATE orders SET gold = NULL")
    row = _rows(app)[bare]
    assert row["chain_gold"] is None and not row["chain_gold_known"]


def test_the_chain_stops_at_a_loop_and_at_a_deleted_order(tmp_path):
    ledger = Ledger(tmp_path)
    a = ledger.add_order(NEW, ITEM, "buy", "chaos", 9, 1, 10, t=T0)
    b = ledger.repost_order(a, 10, 1, 10, t=T0 + 1)
    c = ledger.repost_order(b, 11, 1, 10, t=T0 + 2)
    # A file edited by hand so that the links go round: a -> c -> b -> a.
    ledger._run("UPDATE orders SET reposted_from = ? WHERE id = ?", (c, a))
    assert [o["id"] for o in ledger.order_chain(c)] == [a, b]
    assert [o["id"] for o in ledger.order_chain(a)] == [b, c]
    # Deleting the middle order cuts the chain there.
    ledger._run("UPDATE orders SET reposted_from = NULL WHERE id = ?", (a,))
    ledger.delete_order(b)
    assert ledger.order_chain(c) == []
    assert ledger.order_chain(999) == []


def test_ages_in_a_rehearsal_use_practice_time(archive_root, tmp_path):
    """Posted at the rehearsal's hour and stepped 6 hours on: 6 hours old, whatever the wall clock says."""
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    app.start_rehearsal({"league": NEW, "start_day": 1})
    try:
        app.api_post("/api/order", {"league": NEW, "item": ITEM, "side": "buy", "market": "chaos",
                                    "currency_units": 9, "item_units": 1, "units": 10})
        app.rehearsal.step(6)
        [row] = app.api_ledger({"league": NEW})["orders"]
        assert row["age"] == 6 * HOUR
    finally:
        app.stop_rehearsal()
