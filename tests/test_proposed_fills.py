"""Proposed fills: how much of an open order has probably filled, from the flow that went through its price.

Known market: in league New the test orb's Chaos market trades 1,000 orbs every hour at about 10 Chaos
(within 5%). A buy at 1,000 Chaos each is above every hour's price, so all 1,000 units an hour went
through it; a buy at 1 Chaos each is below every hour's price, so nothing did.
"""

from __future__ import annotations

import pytest

from conftest import ITEM, NEW
from vaalstreet import calibrate
from vaalstreet.db import connect
from vaalstreet.desk import Desk
from vaalstreet.ledger import Ledger

HOUR = 3600


@pytest.fixture()
def desk(con, archive_root):
    return Desk.build(con, NEW, root=archive_root)


def _order(created, currency_units, item_units, units, filled=0.0, side="buy"):
    return {"id": 1, "item": ITEM, "side": side, "market": "chaos", "currency_units": currency_units,
            "item_units": item_units, "units": units, "filled": filled, "created": created, "status": "open"}


def test_an_order_the_market_never_reached_proposes_nothing(desk):
    t0 = int(desk.panel.hours[10])
    guess = calibrate.likely_filled(_order(t0, 1, 1, 500), desk.panel, 0.1, t0 + 10 * HOUR)
    assert guess == {"through": 0.0, "likely": 0.0, "more": 0.0}
    # A sell asking 1,000 Chaos each is above every hour too: nothing traded through it either.
    assert calibrate.likely_filled(_order(t0, 1000, 1, 500, side="sell"), desk.panel, 0.1, t0 + 10 * HOUR)["more"] == 0


def test_share_times_the_flow_rounded_down_to_whole_lots(desk):
    """Ten hours of 1,000 orbs through a buy at 7,000 Chaos for 7: 10,000 units. At a 1.23% share that is
    123 orbs, which is 17 lots of 7 and a bit: 119 orbs, rounded down."""
    t0 = int(desk.panel.hours[10])
    guess = calibrate.likely_filled(_order(t0, 7000, 7, 5000), desk.panel, 0.0123, t0 + 10 * HOUR)
    assert guess["through"] == pytest.approx(10_000)
    assert guess["likely"] == 119 and guess["more"] == 119
    # Half an hour more counts half of that hour: 10,500 units, 129.15 orbs, 18 lots: 126.
    assert calibrate.likely_filled(_order(t0, 7000, 7, 5000), desk.panel, 0.0123, t0 + 10 * HOUR + HOUR // 2)["likely"] == 126


def test_recorded_fills_are_subtracted_and_never_below_zero(desk):
    t0 = int(desk.panel.hours[10])
    # 119 likely, 50 recorded: about 69 more.
    assert calibrate.likely_filled(_order(t0, 7000, 7, 5000, filled=50), desk.panel, 0.0123, t0 + 10 * HOUR)["more"] == 69
    # Recorded more than the desk guesses: it proposes nothing, never a negative fill.
    assert calibrate.likely_filled(_order(t0, 7000, 7, 5000, filled=200), desk.panel, 0.0123, t0 + 10 * HOUR)["more"] == 0


def test_the_proposal_is_capped_at_the_order(desk):
    """10% of 10,000 is 1,000 orbs, but the order is for 100: capped at 100, then whole lots of 7, 98."""
    t0 = int(desk.panel.hours[10])
    guess = calibrate.likely_filled(_order(t0, 7000, 7, 100), desk.panel, 0.1, t0 + 10 * HOUR)
    assert guess["likely"] == 98 and guess["more"] == 98
    assert calibrate.likely_filled(_order(t0, 7000, 7, 100, filled=98), desk.panel, 0.1, t0 + 10 * HOUR)["more"] == 0


def test_an_order_posted_after_the_last_published_hour_proposes_nothing(desk):
    end = desk.as_of + HOUR
    assert calibrate.likely_filled(_order(end + 60, 1000, 1, 500), desk.panel, 0.1, end)["more"] == 0


def test_the_proposal_does_not_look_past_the_desks_hour(con, archive_root, desk):
    """Worked out as of hour t, the proposal is the same whether later hours exist or not.

    A desk built from an archive cut at t (`connect(until=t)`) has no hours after t; the live desk has
    a week more of them. Posted 10 hours before t, an order has seen 11 hours (10 before t and t itself)
    of 1,000 orbs: 11,000 units, 1,100 at 10%, under its 5,000. With the later hours it would have seen far
    more, so a look-ahead would show up as a different, bigger answer."""
    t = int(desk.panel.hours[60])
    order = _order(t - 10 * HOUR, 1000, 1, 5000)
    cut = connect(archive_root, until=t)
    try:
        then = Desk.build(cut, NEW, root=archive_root)
        assert then.as_of == t and then.panel.hours[-1] == t
        before = calibrate.likely_filled(order, then.panel, 0.1, then.as_of + HOUR)
    finally:
        cut.close()
    bounded = calibrate.likely_filled(order, desk.panel, 0.1, t + HOUR)
    assert before == bounded and before["likely"] == 1100
    later = calibrate.likely_filled(order, desk.panel, 0.1, desk.as_of + HOUR)
    assert later["likely"] > before["likely"], "the later hours carry more flow: the test can tell"


def test_a_rehearsal_proposes_from_practice_hours_only(archive_root, tmp_path, desk):
    """A practice order posted 10 hours before the rehearsal's hour has seen 11 hours: 11,000 orbs.
    It is still open, so it teaches no share (only settled orders do, or an order with fills not yet
    recorded would shrink its own proposal): the share is the default 10%, and 1,100 orbs are proposed.
    The live desk's week of later hours is not used."""
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    app.start_rehearsal({"league": NEW, "start_day": 1})
    try:
        hour = app.rehearsal.hour
        app.ledger.add_order(NEW, ITEM, "buy", "chaos", 1000, 1, 5000, t=hour - 10 * HOUR)
        [row] = app.api_ledger({"league": NEW})["orders"]
    finally:
        app.stop_rehearsal()
    assert row["traded_through"] == pytest.approx(11_000)
    assert row["fill_share"] == pytest.approx(0.1) and row["fill_share_orders"] == 0
    assert row["likely_filled"] == 1100 and row["proposed_fill"] == 1100
    later = calibrate.likely_filled(dict(row, created=hour - 10 * HOUR), desk.panel, 0.1, desk.as_of + HOUR)
    assert later["likely"] > 1100


def test_the_ledger_page_carries_the_proposal(archive_root, tmp_path, desk):
    """On the live desk: a buy at 1,000 Chaos each posted 10 hours before the end of the last hour has seen
    10,000 orbs, and 300 are recorded. A second buy, of 10, filled completely after 5 hours (5,000 orbs).
    Only the settled one teaches the share: 10 filled of 5,000 is 0.2% from one order, so with the page's
    share at 20% the share used is (1 x 0.2% + 3 x 20%) / 4 = 15.05%: 1,505 orbs, and 1,205 more than
    recorded. The open order, with 300 recorded so far, is not counted: its record is not final. The
    filled order proposes nothing."""
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)
    end = app.desk(NEW).as_of + HOUR
    oid = app.ledger.add_order(NEW, ITEM, "buy", "chaos", 1000, 1, 5000, t=end - 10 * HOUR)
    app.ledger.fill_order(oid, 300, t=end - HOUR, record_trade=False)
    done = app.ledger.add_order(NEW, ITEM, "buy", "chaos", 1000, 1, 10, t=end - 10 * HOUR)
    app.ledger.fill_order(done, 10, t=end - 5 * HOUR, record_trade=False)
    rows = {o["id"]: o for o in app.api_ledger({"league": NEW, "share": "0.2"})["orders"]}
    row = rows[oid]
    assert row["traded_through"] == pytest.approx(10_000)
    assert row["fill_share"] == pytest.approx(0.1505) and row["fill_share_orders"] == 1
    assert row["likely_filled"] == 1505 and row["proposed_fill"] == 1205
    # The Ledger's own table of measured shares still counts every order, as the flip plans do.
    [measured] = app.api_ledger({"league": NEW, "share": "0.2"})["fill_calibration"]
    assert measured["orders"] == 2
    assert rows[done]["proposed_fill"] is None
    assert app.ledger.order(oid)["filled"] == 300, "a proposal records nothing"
