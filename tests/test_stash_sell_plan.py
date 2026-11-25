"""The stash's sell plan, against `flat_root`: the New league with the Divine steady at 100 Chaos.

Known answers there, at the last hour (the flip plans' own levels, printed by `Desk.flips`):
- The test orb's Chaos market sells at 9.93 Chaos at patience 0.25 and 9.81 at 0.35; at 0.35 the order
  is "1 for 10 Chaos". Its Divine market sells at 10.80 Chaos at 0.35, posted as "28 for 3 Divine"
  (10.71 each), so 100 orbs post 3 lots, 84 orbs for 9 Divines (900 Chaos), and 16 are left over.
- The Chaos market trades 1,000 orbs an hour, the Divine market 4,000. At the default 10% share and
  patience 0.35, selling takes 0.1 x 0.35 x 1,000 = 35 orbs an hour in the Chaos market: 100 orbs in
  about 2.86 hours.
- The test ember's Divine market sells 26 embers for 1 Divine; its Chaos market 1 for 4 Chaos.
- The test card (1.5 Chaos) and the test shard (2 Chaos) trade in the Chaos market only.
"""

from __future__ import annotations

import pytest

from conftest import CARD, EMBER, ITEM, NEW, OTHER, call
from vaalstreet import check
from vaalstreet.desk import PATIENCE, Desk
from vaalstreet.ingest import CHAOS

SELLS_IN = {"chaos": "chaos", "divine": "divine", "cross": "divine"}  # the market each kind of flip sells in


@pytest.fixture(scope="module")
def desk(flat_root):
    from vaalstreet.db import connect

    con = connect(flat_root)
    yield Desk.build(con, NEW, root=flat_root)
    con.close()


def test_the_level_is_the_flip_plans_sell_level(desk):
    plans = [p for p in desk.flips(min_margin=0.0) if p["id"] == ITEM]
    assert {p["kind"] for p in plans} == {"chaos", "divine", "cross"}
    for plan in plans:
        out = check.stash_plan(desk, {ITEM: 100}, reach=plan["reach"])
        market = out["rows"][0][SELLS_IN[plan["kind"]]]
        assert market["level"] == pytest.approx(plan["sell_level"], rel=1e-5), plan["kind"]
    chaos = next(p for p in plans if p["kind"] == "chaos")
    assert chaos["reach"] == 0.25 and chaos["sell_level"] == pytest.approx(9.93, abs=0.01)


def test_more_patience_asks_more(desk):
    levels = [check.stash_plan(desk, {ITEM: 100}, reach=r)["rows"][0]["chaos"]["level"] for r in sorted(PATIENCE)]
    assert levels == sorted(levels, reverse=True), "a lower reach is a price fewer trades reached: higher"


def test_the_recommended_market_and_whole_lots(desk):
    row = check.stash_plan(desk, {ITEM: 100})["rows"][0]
    chaos, divine = row["chaos"], row["divine"]
    assert chaos["order"] == {"currency": "Chaos", "currency_units": 10, "item_units": 1, "price": 10.0}
    assert (chaos["lots"], chaos["units"], chaos["left"], chaos["value"]) == (100, 100, 0, 1000)
    assert chaos["hours"] == pytest.approx(100 / 35, rel=1e-3)
    assert (divine["order"]["item_units"], divine["order"]["currency_units"]) == (28, 3)
    assert (divine["lots"], divine["units"], divine["left"], divine["currency_total"]) == (3, 84, 16, 9)
    assert divine["value"] == pytest.approx(900)
    # The Divine market pays more for each orb, but the lots it takes leave 16 unsold: Chaos brings in more.
    assert row["best"] == "chaos" and row["other"] == "divine"
    # 280 orbs make ten whole Divine lots: 30 Divines (3,000 Chaos) against 2,800 in the Chaos market.
    row = check.stash_plan(desk, {ITEM: 280})["rows"][0]
    assert row["best"] == "divine" and row["divine"]["left"] == 0
    assert row["best_by"] == pytest.approx(3000 / 2800 - 1, abs=1e-3)


def test_a_holding_too_small_for_one_lot_says_so(desk):
    row = check.stash_plan(desk, {EMBER: 3})["rows"][0]
    assert "order" not in row["divine"]
    assert row["divine"]["why"] == "You hold 3; the Divine market's order at this price sells 26 at a time."
    assert row["best"] == "chaos" and row["chaos"]["units"] == 3


def test_lines_that_cannot_be_planned_say_why(desk):
    out = check.stash_plan(desk, {CHAOS: 50, CARD: 10, "Metadata/Items/Currency/CurrencyNotTraded": 4})
    rows = {r["id"]: r for r in out["rows"]}
    assert "nothing to sell it for" in rows[CHAOS]["why"]
    assert "No price in New" in rows["Metadata/Items/Currency/CurrencyNotTraded"]["why"]
    # The card sells in its Chaos market; its Divine market never trades, and the line says so.
    assert rows[CARD]["best"] == "chaos" and rows[CARD]["divine"]["why"] == "The Divine market does not trade steadily."
    assert out["rows"][0]["id"] == CARD, "planned lines come first"


def test_the_minimum_skips_lines_and_says_what_it_skipped(desk):
    # The card line is worth 10 x 1.5 = 15 Chaos and the shards 7 x 2 = 14: both under 20.
    out = check.stash_plan(desk, {ITEM: 100, CARD: 10, OTHER: 7}, min_value=20)
    assert [r["id"] for r in out["rows"]] == [ITEM]
    assert out["skipped"]["lines"] == 2 and out["skipped"]["value"] == pytest.approx(29, abs=0.2)
    t = out["total"]
    assert t["plan"] == pytest.approx(1000) and t["planned_worth"] == pytest.approx(out["rows"][0]["worth"])
    assert t["worth"] == pytest.approx(out["rows"][0]["worth"] + 29, abs=0.2), "the whole stash, skipped lines too"


def test_your_measured_share_sets_the_time(desk):
    out = check.stash_plan(desk, {ITEM: 100}, share_by_item={ITEM: {"chaos": [0.2, 4]}})
    chaos = out["rows"][0]["chaos"]
    assert chaos["units_per_hour"] == pytest.approx(0.2 * 0.35 * 1000) and chaos["share_source"] == "your fills, 4 orders"
    assert out["rows"][0]["divine"]["share_source"] == "default 10%", "the Chaos market's share is not the Divine's"


def test_bad_settings_are_refused(desk):
    with pytest.raises(ValueError, match="reach"):
        check.stash_plan(desk, {ITEM: 1}, reach=0.3)
    with pytest.raises(ValueError, match="minimum"):
        check.stash_plan(desk, {ITEM: 1}, min_value=-5)


def test_the_api_plans_the_stash_and_the_ledger(flat_server):
    assert call(f"{flat_server}/api/stash", {"league": NEW, "item": "Test Orb", "units": 100})[0] == 200
    assert call(f"{flat_server}/api/stash", {"league": NEW, "item": "Test Card", "units": 10})[0] == 200
    status, out = call(f"{flat_server}/api/stash_plan?league={NEW}&reach=0.35&min_value=20")
    assert status == 200 and out["source"] == "stash" and out["reaches"] == list(PATIENCE)
    assert [r["id"] for r in out["rows"]] == [ITEM] and out["skipped"]["lines"] == 1
    assert out["rows"][0]["chaos"]["order"]["item_units"] == 1 and out["orders"] == []
    # A posted sell order is listed, so the line can say it is posted.
    assert call(f"{flat_server}/api/order", {"league": NEW, "item": ITEM, "side": "sell", "market": "chaos",
                                             "currency_units": 10, "item_units": 1, "units": 100})[0] == 200
    out = call(f"{flat_server}/api/stash_plan?league={NEW}")[1]
    assert out["reach"] == check.DEFAULT_REACH and len(out["orders"]) == 1 and out["orders"][0]["side"] == "sell"
    status, out = call(f"{flat_server}/api/stash_plan?league={NEW}&source=ledger")
    assert status == 200 and out["source"] == "ledger" and out["rows"] == []
    status, out = call(f"{flat_server}/api/stash_plan?league={NEW}&reach=0.3")
    assert status == 400 and "reach" in out["error"]
    status, out = call(f"{flat_server}/api/stash_plan?league={NEW}&min_value=-1")
    assert status == 400 and "minimum" in out["error"]


def test_cli_stash_plan(flat_root, tmp_path, monkeypatch, capsys):
    import shutil

    from vaalstreet import cli

    root = tmp_path / "data"
    shutil.copytree(flat_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    cli.main(["stash", "set", "Test Orb", "100", "--league", NEW])
    cli.main(["stash", "set", "Test Card", "10", "--league", NEW])
    capsys.readouterr()
    cli.main(["stash", "--plan", "--min-value", "20", "--league", NEW])
    out = capsys.readouterr().out
    assert "1 for 10 Chaos" in out and "Test Orb" in out
    assert "1 line(s) worth 15 Chaos in all are under your minimum." in out


def test_a_ratio_for_few_items_never_asks_more_than_the_price():
    """`lot_ratio_at_most` rounds the price down. Known answers: at 2.47 each, one item states "2 for 1"
    (19% under) and five state "12 for 5" (2.4, under 3% below); at 0.7236 each, three items state "2 for 3"
    (0.667), and one item cannot state a price under 1 at all."""
    from vaalstreet import pricing

    assert pricing.lot_ratio_at_most(2.47, max_items=1) == (2, 1)
    assert pricing.lot_ratio_at_most(2.47, max_items=5) == (12, 5)
    assert pricing.lot_ratio_at_most(0.7236, max_items=3) == (2, 3)
    assert pricing.lot_ratio_at_most(0.7236, max_items=1) is None
    assert pricing.lot_ratio_at_most(9.5, max_items=2) == (19, 2), "an exact ratio is taken as it is"
    for price in (0.013, 0.7236, 2.47, 9.5, 77.06, 4661.9):
        for cap in (1, 2, 3, 7, 50):
            pair = pricing.lot_ratio_at_most(price, max_items=cap)
            assert pair is None or (pair[0] / pair[1] <= price * (1 + 1e-9) and pair[1] <= cap)


def test_a_small_holding_never_asks_more_than_its_level(desk):
    """With one to seven items the nearest whole-number ratio can sit well above the level, and an order
    above its level does not sell at the pace the plan states. Known answer for every line and market that
    gets an order: it asks no more than the level beyond the usual 2% rounding, and gives away at most 10%;
    a market where that is not possible says why instead."""
    for units in range(1, 8):
        plan = check.stash_plan(desk, {item: units for item in desk.panel.items})
        for row in plan["rows"]:
            for market in ("chaos", "divine"):
                m = row.get(market)
                if not m:
                    continue
                if "order" in m:
                    assert -check.MAX_UNDER - 1e-9 <= m["off_level"] <= 0.02 + 1e-9, (row["name"], units, market, m)
                    assert m["units"] <= units
                else:
                    assert m["why"]
