"""The bankroll ladder and the compounding plan, on the synthetic archive and on injected rates."""

from __future__ import annotations

import shutil

import pytest

from conftest import NEW, call
from vaalstreet import ladder


@pytest.fixture()
def desk(con, archive_root):
    from vaalstreet.desk import Desk

    return Desk.build(con, NEW, root=archive_root)


def test_rungs_are_sorted_and_divines_convert_at_the_current_price():
    steps = ladder.rungs(150.0)
    assert [s["chaos"] for s in steps] == [25, 50, 100, 150, 200, 300, 400, 750, 1500]
    assert steps[3]["label"] == "1 Divine"
    # A Divine worth 100 Chaos lands on the 100 Chaos rung; the two are merged.
    assert [s["chaos"] for s in ladder.rungs(100.0)].count(100) == 1


def test_ladder_grows_and_says_what_opens(desk):
    rows = ladder.ladder(desk)
    by_label = {r["label"]: r for r in rows}
    # What a purse can run never shrinks as it grows.
    for lower, upper in zip(rows, rows[1:]):
        assert upper["can_run"] >= lower["can_run"]
    # Known answers on the synthetic archive (Divine about 148 Chaos):
    # the Chaos-market flip of the test orb first fits at 25 Chaos (one lot costs 19),
    assert "Chaos-market flips" in by_label["25 Chaos"]["opens_groups"]
    # buying for Chaos and selling for Divines needs a 135 Chaos lot, so it opens at 1 Divine,
    assert by_label["100 Chaos"]["counts"]["flips"] == 1
    assert "Chaos-to-Divine flips" in by_label["1 Divine"]["opens_groups"]
    # and the Divine-market flip needs a 285 Chaos lot, so it opens at 2 Divines, not before.
    assert "Divine-market flips" in by_label["2 Divines"]["opens_groups"]
    assert not any(o["group"] == "Divine-market flips" for r in rows if r["chaos"] < 285 for o in r["opens"])
    # Every option opens exactly once.
    keys = [o["key"] for r in rows for o in r["opens"]]
    assert len(keys) == len(set(keys))
    # The replayed rate sits next to the promised one, and the cautious rate is never above either.
    for r in rows:
        s = r["session"]
        assert s["rate"] <= s["expected_per_hour"] + 1e-9
        assert all(p["replayed_per_hour"] is not None for p in s["picks"])
        if r["next"] is not None and s["rate"] > 0:
            assert r["hours_to_next"] == pytest.approx((r["next"] - r["chaos"]) / s["rate"], rel=1e-3)


def test_compounding_switches_at_the_right_rung():
    # 10 Chaos an hour below 100, 50 an hour from 100: 25 -> 50 takes 2.5 h, 50 -> 100 takes 5 h,
    # then 100 -> 200 takes 2 h. A 9 hour session ends 1.5 h into the third step, at 175 Chaos.
    rate = lambda purse: (10.0, [{"label": "A"}]) if purse < 100 else (50.0, [{"label": "B"}])  # noqa: E731
    segs = ladder.compound(25, [25, 50, 100, 200, 400], rate, hours=9)
    assert [(s["from_hour"], s["to_hour"], s["switch_at"]) for s in segs] == [(0, 2.5, 50), (2.5, 7.5, 100), (7.5, 9, None)]
    assert segs[-1]["end_purse"] == 175 and segs[-1]["hours_to_next"] == 0.5
    assert [s["plans"][0]["label"] for s in segs] == ["A", "A", "B"]
    text = ladder.describe(segs)
    assert text[1].startswith("2.5–7.5 h: run A at 50 Chaos") and "switch plans" in text[1]
    assert "keep the same plans" in text[0]
    # A start between rungs is evaluated at the actual purse; a target stops the plan.
    segs = ladder.compound(30, [25, 50, 100], rate, target=60)
    assert [(s["purse"], s["end_purse"], s["reason"]) for s in segs] == [(30, 50, "next rung"), (50, 60, "target reached")]
    # Nothing earns: say so, never divide by zero.
    segs = ladder.compound(5, [25, 50], lambda p: (0.0, []), hours=6)
    assert segs[0]["to_hour"] is None and "nothing to compound" in ladder.describe(segs)[0]


def test_plan_end_to_end_uses_replayed_rates(desk):
    out = ladder.plan(desk, 25, target=50)
    assert out["schedule"][0]["rate"] == out["ladder"][0]["session"]["rate"] > 0
    assert out["schedule"][-1]["reason"] == "target reached"
    assert "last week" in out["note"]


def test_api_ladder(server):
    status, body = call(f"{server}/api/ladder?bankroll=25&hours=6")
    assert status == 200
    assert body["ladder"][0]["label"] == "25 Chaos" and body["schedule"] and body["schedule_text"]


def test_cli_ladder(archive_root, tmp_path, monkeypatch, capsys):
    from vaalstreet import cli

    root = tmp_path / "data"
    shutil.copytree(archive_root, root, ignore=shutil.ignore_patterns("desk.sqlite*"))
    monkeypatch.setenv("VAAL_DATA_DIR", str(root))
    cli.main(["ladder", "--league", NEW, "--from", "25", "--hours", "6"])
    out = capsys.readouterr().out
    assert "Hours to next" in out and "2 Divines (first Divine-market flips): " in out and "From 25 Chaos" in out and "not a promise" in out
