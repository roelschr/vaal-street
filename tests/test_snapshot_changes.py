"""What changed between two snapshots, item by item, and the change in worth split in two honest parts."""

from __future__ import annotations

import pytest

from conftest import CARD, HOUR, ITEM, NEW, OTHER, call
from vaalstreet import wealth
from vaalstreet.desk import Desk
from vaalstreet.ledger import Ledger


def snap(sid, t, stash, known=None, positions=None, league="L"):
    """A stored snapshot as `Ledger.snapshot` reads it back, with the stash worth the Stash panel would show."""
    positions = positions or {}
    worth = lambda held: sum(u * p for u, p in held.values() if p is not None)
    return {"id": sid, "league": league, "t": t, "hour": t - HOUR, "kind": "manual", "note": "", "bridge": 100.0,
            "stash": stash, "positions": positions, "known": known or {},
            "stash_value": worth(stash), "positions_value": worth(positions),
            "stash_unpriced": sum(p is None for _, p in stash.values()), "positions_unpriced": 0}


# Then: 10 orbs at 2, 4 cards at 5, 3 shards with no price. Six hours later: 15 orbs at 2.5, 2 essences at
# 10, the same 3 shards now priced at 1, and the cards sold, still priced at 6 (kept in `known`).
THEN = snap(1, 0, {"orb": [10, 2.0], "card": [4, 5.0], "shard": [3, None]})
NOW = snap(2, 6 * HOUR, {"orb": [15, 2.5], "ess": [2, 10.0], "shard": [3, 1.0]}, known={"card": 6.0})


def test_the_two_parts_add_up_to_the_change_in_worth():
    """Known answers. Orb: +5 held at 2.5 = 12.5, prices moved 10 x 0.5 = 5. Card: -4 held at 6 = -24, prices
    moved 4 x 1 = 4. Essence: +2 at 10 = 20, no earlier units so prices moved nothing. Shard: no price then,
    set apart. Worth then 20 + 20 = 40, now 37.5 + 20 = 57.5: a change of 17.5 = 8.5 held + 9 moved."""
    out = wealth.compare(THEN, NOW)
    assert out["worth_then"] == pytest.approx(40) and out["worth_now"] == pytest.approx(57.5)
    assert out["held_more"] == pytest.approx(8.5) and out["prices_moved"] == pytest.approx(9)
    assert out["held_more"] + out["prices_moved"] == pytest.approx(out["change"]) == pytest.approx(17.5)
    assert [(r["id"], r["change"], r["worth"]) for r in out["gained"]] == [("ess", 2, 20), ("orb", 5, 12.5)]
    assert [(r["id"], r["change"], r["worth"]) for r in out["lost"]] == [("card", -4, -24)]
    assert out["gained_total"] == pytest.approx(32.5) and out["lost_total"] == pytest.approx(-24)
    assert out["gained_total"] + out["lost_total"] == pytest.approx(out["held_more"])
    # The shard is listed apart, not counted as zero. Against the stored worths (40 then, 60.5 now) the
    # difference is the 17.5 counted plus the shard's 3 at its later price, which has no earlier one.
    assert out["no_price"] == [{"id": "shard", "units_then": 3, "units_now": 3, "change": 0, "missing": "then"}]
    assert NOW["stash_value"] - THEN["stash_value"] == pytest.approx(out["change"] + 3 * 1.0)
    assert out["hours"] == 6 and out["per_hour"] == pytest.approx(17.5 / 6)


def test_the_order_of_the_two_does_not_matter_and_bad_pairs_are_refused():
    assert wealth.compare(NOW, THEN) == wealth.compare(THEN, NOW)
    with pytest.raises(ValueError, match="two different"):
        wealth.compare(THEN, THEN)
    with pytest.raises(ValueError, match="different leagues"):
        wealth.compare(THEN, snap(3, HOUR, {}, league="M"))
    with pytest.raises(ValueError, match="source"):
        wealth.compare(THEN, NOW, "nope")


def test_an_item_sold_with_no_later_price_is_set_apart():
    """The cards were sold and the later snapshot has no price for them: 'no price now', not a loss of 20."""
    out = wealth.compare(THEN, snap(2, 6 * HOUR, {"orb": [10, 2.0]}))
    assert [r["id"] for r in out["no_price"]] == ["card", "shard"] and out["no_price"][0]["missing"] == "now"
    assert out["change"] == 0 and out["lost"] == []


def test_a_farming_session_in_one_data_hour():
    """Two snapshots of the stash taken an hour apart within the same prices: all of the change is loot you hold.
    Known answer: 20 more orbs at 2 = +40 in one hour."""
    before = snap(1, 10 * HOUR, {"orb": [10, 2.0]})
    after = snap(2, 11 * HOUR, {"orb": [30, 2.0]}) | {"hour": before["hour"]}
    out = wealth.compare(before, after)
    assert out["held_more"] == 40 and out["prices_moved"] == 0 and out["per_hour"] == 40 and out["data_hours"] == 0
    assert wealth.compare(before, after | {"t": before["t"]})["per_hour"] is None, "no time passed: no rate"
    # Under a quarter of an hour apart there is no rate either: 40 in four seconds is not 36,000 an hour.
    assert wealth.compare(before, after | {"t": before["t"] + 4})["per_hour"] is None
    assert wealth.compare(before, after | {"t": before["t"] + 900})["per_hour"] == pytest.approx(160)


def test_the_ledger_positions_compare_the_same_way():
    then = snap(1, 0, {}, positions={"orb": [10, 2.0]})
    now = snap(2, HOUR, {}, positions={"orb": [4, 3.0]})
    out = wealth.compare(then, now, "ledger")
    assert out["held_more"] == -18 and out["prices_moved"] == 10 and out["change"] == -8
    assert out["now"]["worth"] == 12 and out["then"]["worth"] == 20


@pytest.fixture(scope="module")
def desks(archive_root):
    from vaalstreet.db import connect

    con = connect(archive_root)
    late = Desk.build(con, NEW, root=archive_root)
    yield Desk.build(con, NEW, root=archive_root, as_of=late.as_of - 50 * HOUR), late
    con.close()


def test_two_stored_snapshots_add_up_to_their_stored_worths(desks, tmp_path):
    """Every item priced at both hours: the two parts add up to the change in the stored worths, which are the
    Stash panel's totals. Those and the stored prices are rounded to six significant figures: worths near
    1,000 to 1,500 Chaos are within 0.005 each, hence a tolerance of 0.02."""
    early, late = desks
    ledger = Ledger(tmp_path)
    ledger.set_stash_many(NEW, {ITEM: 100, CARD: 6})
    a = wealth.take(ledger, early, "manual")
    ledger.set_stash_many(NEW, {ITEM: 140, CARD: 0, OTHER: 5})
    b = wealth.take(ledger, late, "manual")
    sa, sb = ledger.snapshot(a), ledger.snapshot(b)
    out = wealth.compare(sa, sb)
    assert out["no_price"] == []
    assert out["held_more"] + out["prices_moved"] == pytest.approx(sb["stash_value"] - sa["stash_value"], abs=0.02)
    assert [r["id"] for r in out["lost"]] == [CARD] and {r["id"] for r in out["gained"]} == {ITEM, OTHER}
    assert out["lost"][0]["worth"] == pytest.approx(-6 * late.fair(CARD), rel=1e-5), "at the later hour's price"


def test_snapshot_changes_over_the_api(server):
    call(server + "/api/stash", {"league": NEW, "item": ITEM, "units": 100})
    a = call(server + "/api/snapshot", {"league": NEW})[1]["snapshot"]
    call(server + "/api/stash", {"league": NEW, "item": ITEM, "units": 130})
    b = call(server + "/api/snapshot", {"league": NEW, "note": "after a map"})[1]["snapshot"]
    status, out = call(server + f"/api/snapshot_diff?a={b}&b={a}")
    assert status == 200 and out["then"]["id"] == a and out["now"]["note"] == "after a map"
    [row] = out["gained"]
    assert row["name"] == "Test Orb" and row["change"] == 30 and out["prices_moved"] == 0
    assert call(server + f"/api/snapshot_diff?a={a}&b={a}")[0] == 400
    assert call(server + f"/api/snapshot_diff?a={a}&b=999")[0] == 404
    assert call(server + f"/api/snapshot_diff?a={a}")[0] == 400
