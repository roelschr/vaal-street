"""The ratio helper: a price per item in, the whole-number ratio to type out.

Known answers, worked by hand from `pricing.lot_ratio` (the smallest ratio within 2%, ties of
Python's round() going to the even number):
- 9.5 Chaos each is "19 Chaos for 2", exactly. The only rounder ratio is "10 for 1" (+5.3%).
- 7/3 Chaos each is "7 for 3", exactly; the rounder "5 for 2" is +7.1%.
- 2.3 Chaos each is "7 for 3" (+1.4%); the rounder one is "5 for 2" (+8.7%), the closer one
  "23 for 10" (exact; "16 for 7" is -0.62%, just outside 0.5%).
- 1.15 Chaos each is "7 for 6" (+1.4%); the rounder one is "6 for 5" (+4.3%), the closer ones
  "15 for 13" (+0.33%, within 0.5%) and "23 for 20" (exact, within 0.1%).
- Under 1 the currency is the smaller side: 0.25 is "1 for 4", 0.05 Divine "1 Divine for 20".
- Holding 7 items, 2.3 still posts "7 for 3" (2 lots), but the closest ratio within 0.1% that fits
  is "16 for 7" (one lot), so the closer choices collapse into one. Holding 2, the closest that
  fits is "5 for 2" (+8.7%). Paying at most 4 Chaos, it is "2 for 1" (-13%).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import call
from vaalstreet import check, pricing


def pairs(answer: dict) -> list[tuple[int, int]]:
    return [(c["currency_units"], c["item_units"]) for c in answer["alternatives"]]


def test_nine_and_a_half_is_nineteen_for_two():
    r = check.ratio_helper(9.5)
    assert (r["ratio"]["currency_units"], r["ratio"]["item_units"]) == (19, 2)
    assert r["ratio"]["price"] == 9.5 and r["ratio"]["error"] == 0
    assert r["ratio"]["currency"] == "Chaos" and r["ratio"]["kind"] == "posted"
    assert pairs(r) == [(10, 1)]  # nothing is closer than exact
    assert r["alternatives"][0]["error"] == pytest.approx(10 / 9.5 - 1)


def test_seven_thirds_is_seven_for_three():
    r = check.ratio_helper(7 / 3)
    assert (r["ratio"]["currency_units"], r["ratio"]["item_units"]) == (7, 3)
    assert r["ratio"]["error"] == pytest.approx(0, abs=1e-9)
    assert pairs(r) == [(5, 2)]


def test_matches_what_flip_plans_post():
    # The posted choice is lot_ratio at the plans' tolerance, so a price from a plan comes back as its order.
    for price in (1.15, 2.3, 9.5, 31.7, 147.75, 0.3, 0.013):
        assert (lambda c: (c["currency_units"], c["item_units"]))(check.ratio_helper(price)["ratio"]) == \
            pricing.lot_ratio(price)


@pytest.mark.parametrize("price, currency, pair", [
    (0.25, "chaos", (1, 4)),
    (0.05, "divine", (1, 20)),
    (0.1, "chaos", (1, 10)),
])
def test_under_one_puts_the_currency_on_the_small_side(price, currency, pair):
    r = check.ratio_helper(price, currency)
    assert (r["ratio"]["currency_units"], r["ratio"]["item_units"]) == pair
    assert r["ratio"]["currency"] == currency.title()
    assert r["alternatives"] == []  # one unit of currency has no smaller lot, and it is exact


def test_alternatives_are_ordered_by_lot_with_errors_to_match():
    assert pairs(check.ratio_helper(2.3)) == [(5, 2), (23, 10)]
    r = check.ratio_helper(1.15)
    assert (r["ratio"]["currency_units"], r["ratio"]["item_units"]) == (7, 6)
    assert pairs(r) == [(6, 5), (15, 13), (23, 20)]
    assert [c["kind"] for c in r["alternatives"]] == ["rounder", "closer", "closer"]
    errors = [abs(c["error"]) for c in r["alternatives"]]
    assert errors[0] > abs(r["ratio"]["error"]) > errors[1] > errors[2] == 0
    lots = [min(c["currency_units"], c["item_units"]) for c in r["alternatives"]]
    assert lots == sorted(lots)


def test_caps_cut_the_ratio_and_count_the_lots():
    held = check.ratio_helper(2.3, max_items=7)
    assert (held["ratio"]["currency_units"], held["ratio"]["item_units"]) == (7, 3)
    assert held["max_lots"] == 2 and held["ratio"]["max_lots"] == 2
    assert pairs(held) == [(5, 2), (16, 7)]  # "23 for 10" needs 10 items
    assert [c["max_lots"] for c in held["alternatives"]] == [3, 1]

    two = check.ratio_helper(2.3, max_items=2)
    assert (two["ratio"]["currency_units"], two["ratio"]["item_units"]) == (5, 2)
    assert two["ratio"]["error"] == pytest.approx(2.5 / 2.3 - 1)

    purse = check.ratio_helper(2.3, max_currency=4)
    assert (purse["ratio"]["currency_units"], purse["ratio"]["item_units"]) == (2, 1)
    assert purse["max_lots"] == 2

    assert check.ratio_helper(2.3)["max_lots"] is None  # no caps, no limit


def test_caps_too_small_answer_with_a_note():
    r = check.ratio_helper(0.25, max_items=3)  # "1 for 4" needs four items
    assert r["ratio"] is None and r["alternatives"] == [] and r["note"]


@pytest.mark.parametrize("price, currency, extra", [
    ("abc", "chaos", {}), (0, "chaos", {}), (-1, "chaos", {}), ("nan", "chaos", {}), ("inf", "chaos", {}),
    (None, "chaos", {}), (9.5, "exalted", {}), (9.5, "chaos", {"max_items": -2}), (9.5, "chaos", {"max_currency": "x"}),
])
def test_bad_input_is_refused(price, currency, extra):
    with pytest.raises(ValueError):
        check.ratio_helper(price, currency, **extra)


def test_api(server):
    status, r = call(f"{server}/api/ratio?price=9.5&currency=chaos")
    assert status == 200 and (r["ratio"]["currency_units"], r["ratio"]["item_units"]) == (19, 2)
    status, r = call(f"{server}/api/ratio?price=2.3&max_items=7")
    assert status == 200 and r["max_lots"] == 2 and len(r["alternatives"]) == 2
    status, r = call(f"{server}/api/ratio?price=0.05&currency=divine")
    assert status == 200 and r["ratio"]["currency"] == "Divine" and r["ratio"]["item_units"] == 20
    status, r = call(f"{server}/api/ratio?price=0.25&max_items=3")
    assert status == 200 and r["ratio"] is None and r["note"]
    for query in ("", "price=", "price=abc", "price=-1", "price=0", "price=9.5&currency=exalted", "price=9.5&max_items=0"):
        status, r = call(f"{server}/api/ratio?{query}")
        assert status == 400 and r["error"], query


ROOT = Path(__file__).resolve().parents[1]  # run the package in this checkout, not an installed one


def test_cli():
    out = subprocess.run([sys.executable, "-m", "vaalstreet", "ratio", "9.5"], capture_output=True, text=True,
                         check=True, cwd=ROOT)
    assert "19 Chaos for 2" in out.stdout and "2 for 19 Chaos" in out.stdout
    bad = subprocess.run([sys.executable, "-m", "vaalstreet", "ratio", "-1"], capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode != 0 and "more than zero" in bad.stderr
