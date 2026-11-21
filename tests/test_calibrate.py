"""Fill calibration: the share of the flow learned from your own orders."""

from __future__ import annotations

import pytest

from conftest import ITEM, NEW, call
from vaalstreet import calibrate
from vaalstreet.desk import Desk
from vaalstreet.ledger import Ledger

HOUR = 3600


@pytest.fixture()
def desk(con, archive_root):
    return Desk.build(con, NEW, root=archive_root)


def _order(ledger, desk):
    """Buy the test orb at 1000 Chaos each: far above its ~10 Chaos price, so every unit of the
    Chaos market's 1000 units an hour trades through the order's price. The order stands ten
    hours, fills 2000 units five hours in, and is cancelled at hour ten.

    Known answer: eligible = 10 h x 1000 = 10,000 units, measured share = 2000 / 10,000 = 20%;
    with one order against the default 10% weighted as three orders, used = (0.2 + 3 x 0.1) / 4 = 12.5%.
    """
    t0 = int(desk.panel.hours[10])
    oid = ledger.add_order(NEW, ITEM, "buy", "chaos", 1000, 1, 5000, t=t0)
    ledger.fill_order(oid, 2000, t=t0 + 5 * HOUR)
    ledger.set_order_status(oid, "cancelled", t=t0 + 10 * HOUR)
    return t0


def test_measured_share_from_a_cancelled_order(tmp_path, desk):
    ledger = Ledger(tmp_path)
    _order(ledger, desk)
    [row] = calibrate.from_ledger(ledger, NEW, desk.panel, desk.as_of + HOUR)
    assert (row["item"], row["market"], row["orders"]) == (ITEM, "chaos", 1)
    assert row["eligible"] == pytest.approx(10_000)
    assert row["measured"] == pytest.approx(0.2)
    assert row["share"] == pytest.approx(0.125)
    assert "faster" in row["reading"]


def test_calibration_uses_only_what_was_known_at_the_time(tmp_path, desk):
    ledger = Ledger(tmp_path)
    t0 = _order(ledger, desk)
    # Three hours in, the fill at hour five has not happened: 3000 units went through, none filled.
    [row] = calibrate.from_ledger(ledger, NEW, desk.panel, t0 + 3 * HOUR)
    assert row["eligible"] == pytest.approx(3000)
    assert row["measured"] == 0
    assert row["share"] == pytest.approx(0.075)  # (0 + 3 x 0.1) / 4
    # Before the order was posted, it does not exist.
    assert calibrate.from_ledger(ledger, NEW, desk.panel, t0) == []
    # A fresh open order with nothing filled says too little to count.
    assert calibrate.from_ledger(ledger, NEW, desk.panel, t0 + HOUR) == []


def test_complete_fill_ends_at_its_last_fill(tmp_path, desk):
    ledger = Ledger(tmp_path)
    t0 = int(desk.panel.hours[20])
    oid = ledger.add_order(NEW, ITEM, "buy", "chaos", 1000, 1, 600, t=t0)
    ledger.fill_order(oid, 600, t=t0 + 4 * HOUR)
    ledger.set_order_status(oid, "closed", t=t0 + 30 * HOUR)  # collected much later: not standing time
    [row] = calibrate.from_ledger(ledger, NEW, desk.panel, desk.as_of + HOUR)
    assert row["eligible"] == pytest.approx(4000)  # four hours of 1000 units
    assert row["measured"] == pytest.approx(0.15) and row["complete"] == 1


def test_flips_use_the_learned_share_per_item(desk):
    base = {p["kind"]: p for p in desk.flips() if p["id"] == ITEM}
    learned = {ITEM: {"chaos": [0.125, 1]}}
    plans = {p["kind"]: p for p in desk.flips(share_by_item=learned) if p["id"] == ITEM}
    assert "chaos" in plans and "divine" in plans
    chaos, divine = plans["chaos"], plans["divine"]
    assert chaos["share_source"] == "your fills, 1 order" and chaos["share"] == 0.125
    assert chaos["units_per_hour"] == pytest.approx(base["chaos"]["units_per_hour"] * 1.25, rel=1e-3)
    assert divine["share_source"] == "default 10%"
    assert divine["units_per_hour"] == base["divine"]["units_per_hour"]
    # The memo keys on the new argument: asking without it again gives the default plans.
    assert {p["kind"]: p for p in desk.flips() if p["id"] == ITEM}["chaos"]["share"] == 0.1


def test_api_ledger_reports_fill_calibration(server):
    # Orders posted through the API are stamped now, after the archive's last hour, so none count yet.
    assert call(f"{server}/api/order", {"item": ITEM, "side": "buy", "market": "chaos", "currency_units": 11,
                                        "item_units": 1, "units": 10})[0] == 200
    assert call(f"{server}/api/ledger?league={NEW}")[1]["fill_calibration"] == []
    flips = call(f"{server}/api/flips?league={NEW}")[1]["flips"]
    assert flips and all(p["share_source"] == "default 10%" for p in flips)


def test_cross_plan_names_the_share_it_uses(desk):
    # Chaos side measured faster than the default: the Divine side's default 10% sets the pace.
    fast = {p["kind"]: p for p in desk.flips(share_by_item={ITEM: {"chaos": [0.3, 2]}}) if p["id"] == ITEM}
    assert fast["cross"]["share"] == 0.1
    assert fast["cross"]["share_source"] == "default 10% on the Divine side; your fills on the Chaos side are faster"
    # Chaos side measured slower: your fills set the pace.
    slow = {p["kind"]: p for p in desk.flips(share_by_item={ITEM: {"chaos": [0.05, 2]}}) if p["id"] == ITEM}
    assert slow["cross"]["share"] == 0.05 and slow["cross"]["share_source"] == "your fills, 2 orders"
