"""The stack check, without Windows, a clipboard or a game.

The item texts follow the layout the game copies, as two open-source tools parse it (Awakened PoE
Trade's parser and Exile UI's stash module): "Item Class:", "Rarity:", the name, then sections split
by a line of dashes, with "Stack Size: held/full stack" for stackable items. They were written from
those parsers, not captured from the game. Each case states its answer.

The clipboard is a stand-in that counts every read, because the rule under test is GGG's: at most
one read of the clipboard per manual key press, and none at all unless the game is in front and the
clipboard changed after the press.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import types
import urllib.request

import pytest

from conftest import NEW, call
from vaalstreet import clipboard, screen
from vaalstreet.clipboard import NotAnItem, Stack, Tally, describe_worth, handle_copy, parse_item, wait_changed
from vaalstreet.screen import PressGate

SCROLLS = ("Item Class: Stackable Currency\r\nRarity: Currency\r\nPortal Scroll\r\n--------\r\n"
           "Stack Size: 2,448/40\r\n--------\r\nCreates a portal to town\r\n--------\r\n"
           "Right click on this item to use it.\r\nShift click to unstack.\r\n")
CARDS = ("Item Class: Divination Cards\nRarity: Divination Card\nThe Doctor\n--------\nStack Size: 3/8\n"
         "--------\nHeadhunter\n--------\nA doctor a day keeps the apples away.\n")
RARE = ("Item Class: Belts\nRarity: Rare\nDoom Tether\nLeather Belt\n--------\nItem Level: 84\n--------\n"
        "+38 to maximum Life (implicit)\n")
ORBS = "Item Class: Stackable Currency\nRarity: Currency\nTest Orb\n--------\nStack Size: 100/20\n--------\n"

GOOD = [
    (SCROLLS, ("Portal Scroll", 2448)),  # a thousands comma, and Windows line ends
    (SCROLLS.replace("2,448", "2.448"), ("Portal Scroll", 2448)),  # the separator follows the Windows language
    (SCROLLS.replace("2,448", "2 448"), ("Portal Scroll", 2448)),
    (CARDS, ("The Doctor", 3)),
    (RARE, ("Doom Tether", 1)),  # no "Stack Size" line: one item; the desk will say it has no market for it
    ("Item Class: Stackable Currency\nRarity: Currency\nChaos Orb", ("Chaos Orb", 1)),  # nothing after the name
]


@pytest.mark.parametrize("text, expected", GOOD)
def test_parser_reads_copied_item_text(text, expected):
    stack = parse_item(text)
    assert (stack.item, stack.units) == expected


@pytest.mark.parametrize("text", [
    "", "hunter2", "my password is Item Class: Stackable Currency",  # item text only at the very start
    "Item Class: Stackable Currency\nRarity: Currency\n",  # no name
    SCROLLS.replace("2,448", "0"), SCROLLS.replace("2,448/40", "many"),  # a stack size that is no count
    "Класс предмета: Валюта\nРедкость: Валюта\nСвиток портала\n",  # not an English client
])
def test_parser_refuses_what_is_not_an_item(text):
    with pytest.raises(NotAnItem):
        parse_item(text)


def test_describe_says_the_count_with_its_separator():
    assert Stack("Portal Scroll", 2448).describe() == "2,448 Portal Scroll"


# ------------------------------------------------------------------ one press
class FakeClip:
    """A clipboard that counts reads. `writes_after` is how many looks at the change counter pass
    before the game has written (None: it never writes)."""

    def __init__(self, text=ORBS, in_front=True, writes_after=2):
        self.text, self.in_front, self.writes_after = text, in_front, writes_after
        self.reads = self.looks = 0

    def game_in_front(self):
        return self.in_front

    def sequence(self):
        self.looks += 1
        return 7 if self.writes_after is None or self.looks <= self.writes_after else 8

    def read(self):
        self.reads += 1
        return self.text


WORTH = {"league": "New", "bridge": 150.0, "name": "Test Orb", "units": 100, "value": 1000.0, "low": 960.0,
         "high": 1040.0, "chaos": {"price": 10.0, "value": 1000.0, "volume_per_hour": 5000.0, "hours": 0.02},
         "divine": {"price": 11.0, "value": 1100.0, "volume_per_hour": 4000.0, "hours": 0.025},
         "best": "divine", "best_by": 0.10, "hours": 0.025, "thin": False}


class FakeDesk:
    def __init__(self, worth=WORTH, stash=None):
        self.asked, self.events, self.kept, self._worth, self._stash = [], [], [], worth, stash or {}

    def set_stash(self, item, units, league):
        self.kept.append((item, units, league))
        if isinstance(self._stash, Exception):
            raise self._stash
        return {"rows": [1, 2], "total": {"value": 450.0}, "bridge": 150.0, "was": None} | self._stash

    def worth(self, item, units):
        self.asked.append((item, units))
        if isinstance(self._worth, Exception):
            raise self._worth
        return self._worth

    def event(self, title, body, league):
        self.events.append((title, body, league))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(clipboard, "TICK", 0.0)
    monkeypatch.setattr(clipboard, "WAIT", 0.05)


def test_one_press_is_one_read_one_valuation_one_event():
    clip, desk, said = FakeClip(), FakeDesk(), []
    assert handle_copy(clip, 7, desk, said.append)["value"] == 1000.0
    assert clip.reads == 1 and desk.asked == [("Test Orb", 100)]
    assert desk.kept == [], "without --stash nothing is written to the stash"
    (title, body, league), = desk.events
    assert title == "100 Test Orb: 6.67 Divine, 10% more for Divines" and league == "New"
    assert body.startswith("Read: 100 Test Orb. Worth 6.67 Divine (1,000 Chaos) at fair value")
    assert any("Read: 100 Test Orb" in line for line in said)
    assert any("The Divine market pays 10% more: 7.33 Divine (1,100 Chaos) against 6.67 Divine (1,000 Chaos)." in line
               for line in said)
    assert any("Selling it all there: under an hour of that market's volume." in line for line in said)


def test_nothing_is_read_unless_the_game_is_in_front():
    clip, desk, said = FakeClip(in_front=False), FakeDesk(), []
    assert handle_copy(clip, 7, desk, said.append) is None
    assert clip.reads == 0 and clip.looks == 0 and desk.asked == [] and desk.events == [] and said == []


def test_nothing_is_read_when_the_game_copied_nothing():
    # No item under the cursor: the clipboard never changes, so its old contents are never looked at.
    clip, desk, said = FakeClip(writes_after=None), FakeDesk(), []
    assert handle_copy(clip, 7, desk, said.append) is None
    assert clip.reads == 0 and clip.looks > 1 and desk.asked == [] and desk.events == []
    assert len(said) == 1 and "nothing was copied" in said[0]


def test_text_that_is_not_an_item_is_dropped_unseen():
    clip, desk, said = FakeClip(text="hunter2 is my password"), FakeDesk(), []
    assert handle_copy(clip, 7, desk, said.append) is None
    assert clip.reads == 1 and desk.asked == [] and desk.events == []
    assert len(said) == 1 and "not an item" in said[0] and "hunter2" not in said[0]


def test_an_item_the_desk_does_not_know_is_said_and_not_posted():
    clip, desk, said = FakeClip(text=RARE), FakeDesk(RuntimeError("No item called 'Doom Tether' traded in New.")), []
    assert handle_copy(clip, 7, desk, said.append) is None
    assert clip.reads == 1 and desk.asked == [("Doom Tether", 1)] and desk.events == []
    assert "Read: 1 Doom Tether" in said[0] and "No item called" in said[1]


def test_waiting_watches_the_counter_and_gives_up():
    clock = iter(range(100))
    looks = []
    assert not wait_changed(lambda: looks.append(1) or 7, 7, timeout=5, tick=0, clock=lambda: next(clock),
                            sleep=lambda s: None)
    assert 4 <= len(looks) <= 7
    seen = iter([7, 7, 8])
    assert wait_changed(lambda: next(seen), 7, timeout=5, tick=0, sleep=lambda s: None)
    assert wait_changed(lambda: 9, 7)  # already changed: no waiting


def test_worth_in_words():
    stack = Stack("Test Orb", 3)
    title, lines = describe_worth(stack, WORTH | {"value": 30.0, "low": 29.0, "high": 31.5, "hours": 9.4, "thin": True,
                                                   "chaos": {"value": 30.0}, "divine": {"value": 33.0}})
    assert title == "3 Test Orb: 30 Chaos, 10% more for Divines"
    assert lines[0] == "Worth 30 Chaos at fair value (29 Chaos to 31.5 Chaos)."
    assert lines[2].startswith("Thin market: this stack is about 9 hours of that market's volume")
    # Only one market trades steadily, and its volume is unknown.
    title, lines = describe_worth(stack, WORTH | {"best": "chaos", "best_by": None, "divine": None, "hours": None})
    assert title == "3 Test Orb: 6.67 Divine" and lines[1].startswith("Only its Chaos market trades steadily")
    assert "volume is unknown" in lines[2]
    # Neither market trades steadily; no price at all.
    title, lines = describe_worth(stack, WORTH | {"best": None, "chaos": None, "divine": None})
    assert "Neither of its markets trades steadily" in lines[1]
    title, lines = describe_worth(stack, {"league": "New", "value": None, "note": "No price in New."})
    assert title == "3 Test Orb: no price" and lines == ["No price in New."]
    # A long name gives way; the amount and the better market stay whole.
    title, _ = describe_worth(Stack("Awakened Unbound Ailments Support of the Long Name", 12345), WORTH)
    assert len(title) <= 60 and title.endswith("…: 6.67 Divine, 10% more for Divines") and title.startswith("12,345 Awakened")


# ------------------------------------------------------------------ the stash
def orbs(count):
    return ORBS.replace("100/20", f"{count:,}/20")


def test_tally_adds_different_counts_in_a_row_and_nothing_else():
    now = [0.0]
    tally = Tally(clock=lambda: now[0])

    def copy(item, units):
        stack = Stack(item, units)
        total, before = tally.plan(stack)
        tally.done(stack, total if total is not None else before)
        return total, before

    assert copy("Chaos Orb", 500) == (500, None)  # the first copy sets the line
    assert copy("Chaos Orb", 620) == (1120, 500)  # a different count right after: another stack
    assert copy("Chaos Orb", 620) == (None, 1120)  # the same count again: the same stack, no write
    assert copy("Chaos Orb", 7) == (1127, 1120)
    assert copy("Test Orb", 3) == (3, None)
    assert copy("Chaos Orb", 620) == (620, None)  # another item came between: the count starts over
    # Three stacks, two of them equal but not next to each other: all three count.
    assert [copy("Test Scrap", n)[0] for n in (20, 7, 20)] == [20, 27, 47]
    # Two equal stacks next to each other count once. That is the rule's known gap.
    assert [copy("Test Orb", n)[0] for n in (20, 20, 7)] == [20, None, 27]
    # Checked again after some trading: that replaces the count, it does not add to it.
    assert copy("Chaos Orb", 500) == (500, None)
    now[0] += clipboard.ROW_SECONDS + 1
    assert copy("Chaos Orb", 620) == (620, None)
    now[0] += clipboard.ROW_SECONDS - 1
    assert copy("Chaos Orb", 30) == (650, 620)


def test_stash_switch_writes_the_copied_stack_and_prints_the_sum():
    desk, tally, said = FakeDesk(stash={"was": 80.0}), Tally(), []
    clip = FakeClip(text=orbs(100))
    handle_copy(clip, 7, desk, said.append, tally)
    assert clip.reads == 1 and desk.kept == [("Test Orb", 100, "New")]
    assert any("Stash: Test Orb set to 100. It replaces the 80 the list held." in line for line in said)
    assert any("Your stash list: 2 lines, worth 3 Divine (450 Chaos)." in line for line in said)
    assert "Stash: Test Orb set to 100." in desk.events[-1][1], "the hideout window hears of the write too"

    clip = FakeClip(text=orbs(40))
    handle_copy(clip, 7, desk, said.append, tally)
    assert clip.reads == 1 and desk.kept[-1] == ("Test Orb", 140, "New")
    assert any("Stash: Test Orb 100 + 40 = 140 (another stack)." in line for line in said)

    said.clear()
    handle_copy(FakeClip(text=orbs(40)), 7, desk, said.append, tally)  # the same stack pressed twice
    assert len(desk.kept) == 2 and any("Stash unchanged" in line and "stays at 140" in line for line in said)

    handle_copy(FakeClip(text=orbs(1500)), 7, desk, said.append, tally)
    assert desk.kept[-1] == ("Test Orb", 1640, "New")
    assert any("Stash: Test Orb 140 + 1,500 = 1,640 (another stack)." in line for line in said)


def test_a_failed_write_leaves_the_count_where_it_was():
    desk, tally, said = FakeDesk(), Tally(), []
    handle_copy(FakeClip(text=orbs(100)), 7, desk, said.append, tally)
    desk._stash = RuntimeError("Can't reach the desk")
    handle_copy(FakeClip(text=orbs(40)), 7, desk, said.append, tally)
    assert any("The stash was not changed: Can't reach the desk" in line for line in said)
    desk._stash = {}
    handle_copy(FakeClip(text=orbs(40)), 7, desk, said.append, tally)  # copied again: the same sum is tried again
    assert [k[1] for k in desk.kept] == [100, 140, 140]


def test_an_unknown_item_writes_nothing_and_ends_the_row():
    desk, tally, said = FakeDesk(), Tally(), []
    handle_copy(FakeClip(text=orbs(100)), 7, desk, said.append, tally)
    desk._worth = RuntimeError("No item called 'Doom Tether' traded in New.")
    clip = FakeClip(text=RARE)
    assert handle_copy(clip, 7, desk, said.append, tally) is None
    assert clip.reads == 1 and len(desk.kept) == 1
    desk._worth = WORTH
    handle_copy(FakeClip(text=orbs(40)), 7, desk, said.append, tally)
    assert desk.kept[-1] == ("Test Orb", 40, "New"), "a copy came between, so this one sets the line"
    # The desk not answering is no copy in between: the next copy carries on with the count.
    desk._worth = screen.DeskUnreachable("Can't reach the desk")
    assert handle_copy(FakeClip(text=orbs(7)), 7, desk, said.append, tally) is None
    desk._worth = WORTH
    handle_copy(FakeClip(text=orbs(7)), 7, desk, said.append, tally)
    assert desk.kept[-1] == ("Test Orb", 47, "New")


def test_copied_stacks_fill_the_stash_through_the_real_api(server):
    client, tally, said = screen.DeskClient(server), Tally(), []
    assert call(f"{server}/api/stash", {"item": "Test Orb", "units": 80})[1]["was"] is None  # typed by hand first
    handle_copy(FakeClip(text=orbs(100)), 7, client, said.append, tally)
    assert any("It replaces the 80 the list held." in line for line in said)
    handle_copy(FakeClip(text=orbs(40)), 7, client, said.append, tally)
    handle_copy(FakeClip(text=orbs(40)), 7, client, said.append, tally)
    rows = call(f"{server}/api/stash")[1]["rows"]
    assert [(r["name"], r["units"]) for r in rows] == [("Test Orb", 140)]
    assert any("Your stash list: 1 line, worth " in line for line in said)


def test_a_copy_during_a_rehearsal_goes_to_the_practice_stash(server):
    client, said = screen.DeskClient(server), []
    assert call(f"{server}/api/rehearsal", {"league": NEW, "start_day": 2, "bankroll": 1000})[1]["running"]
    handle_copy(FakeClip(text=orbs(100)), 7, client, said.append, Tally())
    assert any("This is the practice stash: a rehearsal is running." in line for line in said)
    assert [r["units"] for r in call(f"{server}/api/stash")[1]["rows"]] == [100]
    request = urllib.request.Request(f"{server}/api/rehearsal", method="DELETE")
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
    assert call(f"{server}/api/stash")[1]["rows"] == [], "the real stash was never written"


# ------------------------------------------------------------------ the Windows reader
class Fn:
    """A stand-in for one Windows function: counts its calls, and takes the type hints ctypes sets."""

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, *args):
        self.calls += 1
        return self.answer(*args) if callable(self.answer) else self.answer


def windows(text=ORBS, window_class="POEWindowClass", opens=True, window=1234):
    """`WindowsClipboard` over stand-in libraries, with the stand-ins to count calls on."""
    held = ctypes.create_unicode_buffer(text or "")

    def class_name(hwnd, buffer, size):
        buffer.value = window_class
        return len(window_class)

    user32 = types.SimpleNamespace(
        GetClipboardSequenceNumber=Fn(41), GetForegroundWindow=Fn(window), GetClassNameW=Fn(class_name),
        OpenClipboard=Fn(opens), GetClipboardData=Fn(99 if text is not None else 0), CloseClipboard=Fn(1))
    # `held` rides along so the text stays in memory for as long as the stand-in points at it.
    kernel32 = types.SimpleNamespace(GlobalLock=Fn(ctypes.addressof(held)), GlobalUnlock=Fn(1), held=held)
    return clipboard.WindowsClipboard(user32, kernel32), user32, kernel32


def test_windows_reader_reads_the_contents_once_and_always_closes():
    clip, user32, kernel32 = windows()
    assert clip.read() == ORBS
    assert user32.GetClipboardData.calls == 1 and user32.OpenClipboard.calls == 1 and user32.CloseClipboard.calls == 1
    assert kernel32.GlobalLock.calls == 1 and kernel32.GlobalUnlock.calls == 1
    # The change counter and the window check never touch the contents.
    assert clip.sequence() == 41 and clip.game_in_front()
    assert user32.GetClipboardData.calls == 1 and user32.OpenClipboard.calls == 1


def test_windows_reader_reads_nothing_when_the_clipboard_cannot_be_opened():
    clip, user32, kernel32 = windows(opens=False)  # another program holds it for the whole wait
    assert clip.read() == ""
    assert user32.OpenClipboard.calls > 1 and user32.GetClipboardData.calls == 0 and user32.CloseClipboard.calls == 0


def test_windows_reader_closes_the_clipboard_when_it_holds_no_text():
    clip, user32, kernel32 = windows(text=None)  # an image, say
    assert clip.read() == ""
    assert user32.GetClipboardData.calls == 1 and kernel32.GlobalLock.calls == 0 and user32.CloseClipboard.calls == 1


@pytest.mark.parametrize("window_class, window, in_front", [
    ("POEWindowClass", 1234, True),
    ("Chrome_WidgetWin_1", 1234, False),  # a browser tab titled "Path of Exile" is not the game
    ("CabinetWClass", 1234, False),  # nor is a folder window on the game's folder
    ("POEWindowClass", 0, False),  # no window in front at all
])
def test_only_the_games_own_window_counts_as_in_front(window_class, window, in_front):
    clip, user32, _ = windows(window_class=window_class, window=window)
    assert clip.game_in_front() is in_front
    # With another window in front, a press ends before the change counter is even looked at.
    desk, said = FakeDesk(), []
    if not in_front:
        assert handle_copy(clip, 41, desk, said.append) is None
        assert user32.GetClipboardSequenceNumber.calls == 0 and user32.OpenClipboard.calls == 0 and said == []


# ------------------------------------------------------------------ the gate, the desk, the command
def test_gate_runs_the_job_a_press_brings_and_drops_presses_while_busy():
    gate, ran = PressGate(), []
    started, release = threading.Event(), threading.Event()

    def copy_job():
        ran.append("copy")
        started.set()
        release.wait(5)

    thread = threading.Thread(target=gate.serve, args=(lambda: ran.append("screen"),), daemon=True)
    thread.start()
    assert gate.press(copy_job) and started.wait(5)
    # While the stack check runs, neither key is taken: no second read of anything, and nothing queues.
    assert not gate.press() and not gate.press(copy_job)
    release.set()
    for _ in range(200):
        if not gate._busy.locked():
            break
        threading.Event().wait(0.01)
    assert gate.press()  # the screen key, with no job of its own, does the screen check
    for _ in range(200):
        if ran == ["copy", "screen"]:
            break
        threading.Event().wait(0.01)
    gate.stop()
    thread.join(5)
    assert ran == ["copy", "screen"] and not thread.is_alive()


def test_worth_endpoint_values_a_holding_and_saves_nothing(server):
    # Known answers of the synthetic archive: Test Orb's Divine market pays 10% more and trades 4,000 an hour.
    status, out = call(f"{server}/api/worth?item=test%20orb&units=100")
    assert status == 200 and out["name"] == "Test Orb" and out["units"] == 100 and out["league"] == "New"
    assert out["low"] <= out["value"] <= out["high"]
    assert out["best"] == "divine" and out["best_by"] == pytest.approx(0.10, abs=0.01)
    assert out["hours"] == pytest.approx(100 / 4000, rel=0.01) and not out["thin"]
    assert call(f"{server}/api/worth?item=Chaos%20Orb&units=50")[1]["value"] == 50
    assert call(f"{server}/api/worth?item=Test%20Orb")[1]["units"] == 1  # no count: one item
    assert call(f"{server}/api/stash")[1]["rows"] == [], "valuing a stack must not write to the stash"
    assert call(f"{server}/api/worth?item=Doom%20Tether&units=1")[0] == 404
    assert call(f"{server}/api/worth?item=Test%20Orb&units=0")[0] == 400
    assert call(f"{server}/api/worth?units=3")[0] == 400
    assert call(f"{server}/api/worth?item=Test%20Orb", headers={"Origin": "https://evil.example"})[0] == 403


def test_a_copied_stack_reaches_the_hideout_through_the_real_api(server):
    client, said = screen.DeskClient(server), []
    worth = handle_copy(FakeClip(), 7, client, said.append)
    assert worth["best"] == "divine" and worth["units"] == 100
    event = call(f"{server}/api/events?league={worth['league']}")[1]["events"][-1]
    assert event["title"].startswith("100 Test Orb: ") and event["title"].endswith(", 10% more for Divines")
    assert event["body"].startswith("Read: 100 Test Orb. Worth ")
    # An item the exchange does not trade: the desk's own message, and no event.
    said.clear()
    assert handle_copy(FakeClip(text=RARE), 7, client, said.append) is None
    assert "No item called 'Doom Tether'" in said[-1]
    assert call(f"{server}/api/events?league={worth['league']}")[1]["events"][-1] == event


@pytest.mark.skipif(sys.platform == "win32", reason="the Windows clipboard exists there")
def test_stacks_says_it_needs_windows():
    with pytest.raises(SystemExit) as err:
        clipboard.open_clipboard()
    assert "only under Windows Python" in str(err.value)


def test_cli_passes_the_switch_and_it_is_off_unless_asked(monkeypatch, tmp_path):
    from vaalstreet import cli

    seen = []
    monkeypatch.setenv("VAAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(screen, "run", lambda *args: seen.append(args[-2:]))  # (stacks, stash)
    cli.main(["screen"])
    cli.main(["screen", "--stacks"])
    cli.main(["screen", "--stash"])  # writing to the stash turns the stack check on
    assert seen == [(False, False), (True, False), (True, True)]
