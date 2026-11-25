"""Search strings for the game's search box: each finds the items it claims and no other.

Known answers, against the real names below plus the synthetic archive's:
- "Orb of Alchemy" is `.*Alchemy` (9 characters); "Chaos Orb" is `Chaos.*`, since `.*Orb` would
  also find Divine Orb, Test Orb and the rest.
- With both Greed essences known, "Deafening Essence of Greed" cannot be `.*Greed` (it would find
  the Shrieking one) nor `Deafening.*` (Deafening Essence of Hatred): it is the full name with dots.
- "Mirror Shard" needs its full name, `Mirror.Shard`: `.*Shard` would also find Test Shard, and
  `Mirror.*` Mirror of Kalandra. "Mirror of Kalandra" is `.*Kalandra`.
- "The Doctor" is `.*Doctor`; `The.*` would find The Nurse and The Test Blade.
"""

from __future__ import annotations

import re

import pytest

from conftest import ITEM, NEW, OTHER, call
from vaalstreet import searchstring

REAL = [
    "Chaos Orb", "Divine Orb", "Exalted Orb", "Orb of Alteration", "Orb of Augmentation", "Orb of Alchemy",
    "Orb of Fusing", "Orb of Scouring", "Orb of Regret", "Gemcutter's Prism", "Glassblower's Bauble",
    "Deafening Essence of Greed", "Shrieking Essence of Greed", "Deafening Essence of Hatred",
    "Screaming Essence of Greed", "Mirror Shard", "Mirror of Kalandra", "The Doctor", "The Nurse",
    "The Apothecary", "Blessed Orb", "Vaal Orb", "Awakened Sextant", "Tainted Armourer's Scrap",
    "Fracturing Orb", "Splinter of Xoph", "Splinter of Tul", "Blessing of Xoph", "Maven's Orb",
]
# The synthetic archive's names (tests/conftest.py).
SYNTHETIC = ["Test Orb", "Test Shard", "Test Ember", "Test Card", "Test Lure", "The Test Blade", "Test Scrap"]
KNOWN = REAL + SYNTHETIC


def assert_exact(result: dict, known: list[str]) -> None:
    """Every string matches all the names it claims and none of the others, whole name, any case."""
    for s in result["strings"]:
        assert len(s["text"]) == s["length"] <= result["limit"]
        rx = re.compile(s["text"], re.IGNORECASE)
        for name in known:
            assert bool(rx.fullmatch(name)) == (name in s["items"]), (s["text"], name)


@pytest.mark.parametrize("name, pattern", [
    ("Orb of Alchemy", ".*Alchemy"), ("Chaos Orb", "Chaos.*"), ("Mirror of Kalandra", ".*Kalandra"),
    ("Mirror Shard", "Mirror.Shard"), ("The Doctor", ".*Doctor"), ("Gemcutter's Prism", ".*Prism"),
    ("Deafening Essence of Greed", "Deafening.Essence.of.Greed"), ("Deafening Essence of Hatred", ".*Hatred"),
    ("Splinter of Xoph", "Splinter.of.Xoph"),  # ".*Xoph" finds the Blessing, "Splinter.*" the Splinter of Tul
    ("Tainted Armourer's Scrap", "Tainted.*"),  # ".*Scrap" also finds the Test Scrap
    ("Maven's Orb", "Maven.s.*"),
])
def test_shortest_unique_pattern(name, pattern):
    assert searchstring.pattern_for(name, KNOWN) == pattern


def test_greed_only_when_one_essence_is_unique():
    # Only one Greed essence known: then ".*Greed" finds it alone and is the shortest.
    assert searchstring.pattern_for("Deafening Essence of Greed", ["Deafening Essence of Greed", "Chaos Orb"]) == ".*Greed"
    # Both known, and only one wanted: the string spells it out.
    r = searchstring.build(["Deafening Essence of Greed"], KNOWN, 50)
    assert r["strings"][0]["text"] == "^(Deafening.Essence.of.Greed)$"
    assert_exact(r, KNOWN)


def test_every_string_is_exact_and_nothing_is_dropped():
    wanted = ["Chaos Orb", "Orb of Alchemy", "Orb of Fusing", "Deafening Essence of Greed", "Mirror Shard",
              "The Doctor", "Gemcutter's Prism", "Test Orb", "Splinter of Xoph", "Vaal Orb", "Awakened Sextant"]
    for limit in (20, 30, 50, 200):
        r = searchstring.build(wanted, KNOWN, limit)
        assert_exact(r, KNOWN)
        covered = [n for s in r["strings"] for n in s["items"]] + [t["name"] for t in r["too_long"]]
        assert sorted(covered) == sorted(wanted)  # each once


def test_split_at_the_limit():
    # .*Alchemy (9), .*Fusing (8), .*Doctor (8): "^(.*Alchemy|.*Fusing|.*Doctor)$" is 31 characters.
    r = searchstring.build(["Orb of Alchemy", "Orb of Fusing", "The Doctor"], KNOWN, 31)
    assert [s["text"] for s in r["strings"]] == ["^(.*Alchemy|.*Fusing|.*Doctor)$"]
    r = searchstring.build(["Orb of Alchemy", "Orb of Fusing", "The Doctor"], KNOWN, 30)
    assert len(r["strings"]) == 2 and all(s["length"] <= 30 for s in r["strings"])
    # The first string holds the first item asked for; items keep the order asked within a string.
    assert r["strings"][0]["items"][0] == "Orb of Alchemy"
    assert_exact(r, KNOWN)


def test_an_item_that_does_not_fit_alone_is_reported():
    # "^(Deafening.Essence.of.Greed)$" is 30 characters.
    r = searchstring.build(["Deafening Essence of Greed", "Orb of Alchemy"], KNOWN, 20)
    assert r["too_long"] == [{"name": "Deafening Essence of Greed", "pattern": "Deafening.Essence.of.Greed", "length": 30}]
    assert [s["items"] for s in r["strings"]] == [["Orb of Alchemy"]]


def test_odd_characters_become_dots():
    assert searchstring.candidates("Gemcutter's Prism") == [".*Prism", "Gemcutter.s.*", "Gemcutter.s.Prism"]
    assert searchstring.pattern_for("Ritual Splinter (Blighted)", ["Ritual Splinter (Blighted)", "Ritual Vessel"]) \
        == ".*.Blighted."


@pytest.mark.parametrize("limit", [0, 4, -1, 2.5, True])
def test_bad_limit(limit):
    with pytest.raises(ValueError):
        searchstring.build(["Chaos Orb"], KNOWN, limit)


def test_api(server):
    status, r = call(f"{server}/api/search_string?league={NEW}&items={ITEM},{OTHER}&limit=50")
    # In the synthetic league ".*Orb" would find Divine Orb and "Test.*" every test item, so the orb
    # needs its full name; ".*Shard" finds the shard alone.
    assert status == 200 and r["too_long"] == []
    assert r["strings"] == [{"text": "^(Test.Orb|.*Shard)$", "length": 20, "items": ["Test Orb", "Test Shard"]}]
    rx = re.compile(r["strings"][0]["text"], re.I)
    for other in ("Divine Orb", "Chaos Orb", "Test Ember", "Test Card"):
        assert not rx.fullmatch(other)
    for query in (f"league={NEW}", f"league={NEW}&items=Nope", f"league={NEW}&items={ITEM}&limit=x",
                  f"league={NEW}&items={ITEM}&limit=3"):
        status, r = call(f"{server}/api/search_string?{query}")
        assert status == 400 and r["error"], query
