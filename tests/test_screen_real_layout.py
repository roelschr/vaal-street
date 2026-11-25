"""The exchange window as the game really shows it, from presses made in game on 25 November 2026.

The lines below are what text recognition returned for a whole 2560 x 1440 screen: the game's own
window, the minimap's names next to it ("Kir", "Tujen"), and the desk's page in a corner.
"""
import pytest

from vaalstreet import screen

NAMES = ["Eldritch Exalted Orb", "Eldritch Chaos Orb", "Exalted Orb", "Orb of Alteration", "Orb of Annulment",
         "Tattoo of the Tawhoa Warrior"]
HEAD = ["OHAN,THEKING'SHAND", "INVENTORY", "INVENTORY", "COSMETICS", "HORTICRAT", "Johan ()", "Faustus", "Lilly",
        "MARKET RATIO"]
TAIL = ["525", "PLACE ORDER", "0/10", "LILLY ROTH", "2.892", "ALERTS", "148 Chaos Orb:148 Chaos 8 min ago",
        "Eldritch Exalted Orb", "Buy 11 for 7", "Tattoo of the Tawhoa Warrior"]


def window(row, market="2"):
    return HEAD + ["IWANT", market, "IHAVE", "Kir", "a", "Xx"] + row + TAIL


def test_the_press_from_the_game_reads():
    # Known answer: the window showed Eldritch Exalted Orb wanted, Chaos Orb offered, at 1 : 2.
    # The ratio came back with a full-width colon, and the market's own ratio only as "2".
    r = screen.parse_screen(window(["EldritchExaltedOrb", "1：2", "Chaos Orb"]), NAMES)
    assert (r.item, r.market, r.side, r.item_units, r.currency_units) == ("Eldritch Exalted Orb", "chaos", "buy", 1, 2)
    assert (r.want_amount, r.have_amount) == (None, None)


def test_the_left_name_is_the_wanted_item():
    r = screen.parse_screen(window(["Chaos Orb", "4.97:1", "Orb of Alteration"], "4.97:1"), NAMES)
    assert (r.item, r.side, r.item_units, r.currency_units) == ("Orb of Alteration", "sell", 1, 4.97)


def test_divine_against_chaos():
    r = screen.parse_screen(window(["Divine Orb", "1:178", "Chaos Orb"], "1:178"), NAMES)
    assert (r.item, r.market, r.side, r.currency_units) == ("Divine Orb", "chaos", "buy", 178)


def test_amounts_on_the_row_between_the_names():
    # The amount fields stand on the names' row, either side of the ratio (seen in the screenshot).
    r = screen.parse_screen(window(["Orb of Alteration", "154", "4.97:1", "31", "Chaos Orb"], "4.97:1"), NAMES)
    assert (r.want_amount, r.have_amount) == (154, 31)
    # One amount alone is not enough to prefill an order.
    r = screen.parse_screen(window(["Orb of Alteration", "4.97:1", "31", "Chaos Orb"], "4.97:1"), NAMES)
    assert (r.want_amount, r.have_amount) == (None, None)


def test_names_further_down_the_screen_are_not_taken():
    # The desk's own page was on screen with item names in it; only the window's two names count.
    r = screen.parse_screen(window(["Orb of Annulment", "1:4.50", "Chaos Orb"], "1:4.50"), NAMES)
    assert r.item == "Orb of Annulment"


def test_an_empty_side_says_so():
    lines = HEAD + ["I WANT", "Select currency..", "I HAVE", "Kir", "ClicktoSelect", "ClicktoSelect", "PLACEORDER"]
    with pytest.raises(screen.ScreenError, match="no item yet"):
        screen.parse_screen(lines, NAMES)


def test_a_misread_name_is_offered_for_teaching_and_other_text_is_not():
    with pytest.raises(screen.ScreenError) as err:
        screen.parse_screen(window(["Eldrtch Exlted 0rb", "1:2", "Chaos Orb"]), NAMES[2:])
    assert err.value.text == "Eldrtch Exlted 0rb"     # not "Kir", the minimap text right under the label
    r = screen.parse_screen(window(["Eldrtch Exlted 0rb", "1:2", "Chaos Orb"]), NAMES,
                            {"eldrtchexlted0rb": "Eldritch Exalted Orb"})
    assert r.item == "Eldritch Exalted Orb"


def test_reading_order_puts_one_row_left_to_right():
    # (left, top, bottom, text). The two names sit 4 pixels apart in height; the old grid of 12
    # pixels split them into two rows and read the right one first.
    boxes = [(1081, 310, 334, "Chaos Orb"), (415, 306, 336, "EldritchExaltedOrb"), (815, 309, 331, "1:2"),
             (462, 252, 276, "I WANT"), (1133, 253, 277, "I HAVE"), (815, 252, 274, "1:2"),
             (778, 222, 246, "MARKET RATIO"), (765, 418, 440, "PLACE ORDER")]
    assert screen.reading_order(boxes) == ["MARKET RATIO", "I WANT", "1:2", "I HAVE", "EldritchExaltedOrb", "1:2",
                                           "Chaos Orb", "PLACE ORDER"]
