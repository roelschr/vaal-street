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
        screen.parse_screen(window(["Eldrch Exlte 0rb", "1:2", "Chaos Orb"]), NAMES)
    assert err.value.text == "Eldrch Exlte 0rb"     # not "Kir", the minimap text right under the label
    r = screen.parse_screen(window(["Eldrch Exlte 0rb", "1:2", "Chaos Orb"]), NAMES,
                            {"eldrchexlte0rb": "Eldritch Exalted Orb"})
    assert r.item == "Eldritch Exalted Orb"


def test_reading_order_puts_one_row_left_to_right():
    # (left, top, bottom, text). The two names sit 4 pixels apart in height; the old grid of 12
    # pixels split them into two rows and read the right one first.
    boxes = [(1081, 310, 334, "Chaos Orb"), (415, 306, 336, "EldritchExaltedOrb"), (815, 309, 331, "1:2"),
             (462, 252, 276, "I WANT"), (1133, 253, 277, "I HAVE"), (815, 252, 274, "1:2"),
             (778, 222, 246, "MARKET RATIO"), (765, 418, 440, "PLACE ORDER")]
    assert screen.reading_order(boxes) == ["MARKET RATIO", "I WANT", "1:2", "I HAVE", "EldritchExaltedOrb", "1:2",
                                           "Chaos Orb", "PLACE ORDER"]


# A press with amounts typed, as recognised in game: 13 Stacked Deck wanted for 43 Chaos, the
# market at 1 : 3.42, a fee of 455 Gold, and minimap text ("Hel", "Tujen") in between.
TYPED = ["MARKETRATIO", "IWANT", "1:3.42", "I HAVE", "Kir", "a", "Hel", "StackedDeck", "13", "1:3.31", "43",
         "Chaos Orb", "Tujen", "455", "EXPLORINGKINGSMARCH (OPTIONAL)", "PLACEORDER", "0/10"]
DECK = ["Stacked Deck"]


def test_typed_amounts_are_the_offer_judged_and_the_gold_is_read():
    r = screen.parse_screen(TYPED, DECK)
    # Known answer: your own order, 13 for 43, not the market's 1 : 3.42.
    assert (r.item, r.side, r.item_units, r.currency_units) == ("Stacked Deck", "buy", 13, 43)
    assert (r.want_amount, r.have_amount, r.gold) == (13, 43, 455)


def test_gold_with_a_thousands_mark_and_none_without_place_order():
    lines = ["MARKETRATIO", "IWANT", "140:1", "I HAVE", "Chaos Orb", "140", "140:1", "1", "Divine 0rb", "Tujen",
             "2.100", "Life2.830/2.830", "EXPLORINGKINGSMARCH (OPTIONAL)", "PLACEORDER"]
    r = screen.parse_screen(lines, DECK)
    assert (r.item, r.side, r.gold) == ("Divine Orb", "sell", 2100)
    assert screen.parse_screen(lines[:-1], DECK).gold is None


class RecordingDesk:
    corrections: dict = {}

    def __init__(self):
        self.orders, self.reports, self.said = [], [], []

    def names(self):
        return "Covens", DECK

    def check(self, reading, league):
        return {"verdict": "go", "reasons": []}

    def event(self, *a):
        pass

    def reading(self, report):
        self.reports.append(report)

    def order(self, reading, league):
        self.orders.append((league, reading.item, reading.side, reading.want_amount, reading.have_amount, reading.gold))


def test_the_record_key_writes_one_order_and_the_check_key_none():
    desk, recent = RecordingDesk(), {}
    screen.handle_press(lambda: "image", lambda image: TYPED, desk, desk.said.append)
    assert desk.orders == [] and desk.reports[-1]["recorded"] is False
    screen.handle_press(lambda: "image", lambda image: TYPED, desk, desk.said.append, record=True, recent=recent)
    assert desk.orders == [("Covens", "Stacked Deck", "buy", 13, 43, 455)] and desk.reports[-1]["recorded"] is True
    assert any("Recorded in your Ledger: buying 13 Stacked Deck for 43 Chaos, 455 Gold" in s for s in desk.said)
    # The same press repeated does not write a second order.
    screen.handle_press(lambda: "image", lambda image: TYPED, desk, desk.said.append, record=True, recent=recent)
    assert len(desk.orders) == 1 and "Not recorded again" in desk.said[-3] + desk.said[-2] + desk.said[-1]


def test_the_record_key_needs_both_amounts():
    desk = RecordingDesk()
    lines = [ln for ln in TYPED if ln != "13"]
    screen.handle_press(lambda: "image", lambda image: lines, desk, desk.said.append, record=True, recent={})
    assert desk.orders == [] and any("Not recorded" in s for s in desk.said)


def test_the_desk_records_an_order_sent_by_name_with_its_gold(archive_root, tmp_path):
    from conftest import ITEM
    from vaalstreet.ledger import Ledger
    from vaalstreet.server import App

    app = App(archive_root, sync=False)
    app.ledger = Ledger(tmp_path)  # the session's archive is shared: keep this test's order out of it
    desk = app.desk(None)
    app.api_post("/api/order", {"league": desk.league, "item": desk._name(ITEM), "side": "buy", "market": "chaos",
                                "item_units": 13, "currency_units": 43, "units": 13, "gold": 455})
    (order,) = app.ledger.orders(desk.league)
    assert (order["item"], order["units"], order["gold"]) == (ITEM, 13, 455)
    # A reading's Gold reaches the "Posted this order…" form.
    app.api_reading({"kind": "screen", "league": desk.league, "lines": [], "recorded": True, "reading": {
        "item": desk._name(ITEM), "market": "chaos", "side": "buy", "item_units": 13, "currency_units": 43,
        "want_amount": 13, "have_amount": 43, "gold": 455}})
    row = app.api_readings({})["readings"][0]
    assert row["recorded"] is True and row["order"]["gold"] == 455


def test_no_stock_says_so_and_amounts_still_read():
    # Seen in game: a pair nobody offers shows "No Stock" where the market's ratio stands.
    head = ["MARKETRATIO", "I WANT", "No Stock", "I HAVE", "Kir", "a", "Hel"]
    with pytest.raises(screen.ScreenError, match="No Stock"):
        screen.parse_screen(head + ["EldritchExaltedOrb", "Divine Orb", "Tujen", "250", "PLACE ORDER"], NAMES)
    r = screen.parse_screen(head + ["EldritchExaltedOrb", "90", "90:1", "1", "Divine Orb", "250", "PLACE ORDER"], NAMES)
    assert (r.item, r.market, r.side, r.item_units, r.currency_units, r.gold) == ("Eldritch Exalted Orb", "divine", "buy", 90, 1, 250)
