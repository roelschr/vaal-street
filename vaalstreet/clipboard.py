"""Stack check: press the game's own copy key over a stack, and see what it is worth.

Path of Exile copies an item's text to the clipboard when you hover it and press Ctrl+C. For a
stack, that text names the item and says how many you hold ("Stack Size: 2,448/40"). The companion
(`vaalstreet screen --stacks`) hears that key press, reads the copied text once, and asks the desk
what the stack is worth at fair value and in each of its two markets. No typing, and no stash list
to keep: the question "what is this worth, and where do I sell it" is answered for the stack under
the cursor.

GGG's rules for tools that read the clipboard shape the design, as they do for the screen check
(see docs/data-sources.md):
- One read of the clipboard per manual key press, and the press is yours: the companion sends no
  key to the game, unlike price-check overlays that press the copy key for you.
- The game writes the clipboard a moment after the key goes down. The companion must not read
  before that, or it would value whatever was copied last. So it notes Windows' clipboard sequence
  number when the key goes down, waits up to half a second for the number to change, and only then
  reads. The sequence number is a counter of changes; watching it reads nothing of the contents.
  If it does not change, the game copied nothing and the companion reads nothing.
- Ctrl+C is pressed in every program. A press counts only while the game is the window in front,
  and anything on the clipboard that is not item text is dropped: never printed, never sent to the
  desk. Only the item's name and the size of the stack leave this module.

With `--stash` each copied stack is also written to the stash list on the desk, so the list fills
without typing. One item can sit in several stacks, and the copied text does not say which stack it
came from. The rule is the owner's: a copy is compared with the copy just before it. The same item
with a different count is another stack, and adds. The same item with the same count is the same
stack pressed twice, and changes nothing. Any other copy sets the item's line to the count read.
"Just before" is also bounded in time (`ROW_SECONDS`): checking one currency again an hour later
must replace its count, not add to it.

A player who also plays maps drops loot into the stash all the time, so the same pile is copied
again with a new count far more often than a second stack is. The copied text tells the two apart
in the common case: it gives the size of a full stack after the slash ("2,448/40"), and a count
above that can only be a pile in a tab with one space for the item, such as the currency tab. Such
a pile always sets the line and never adds. Counts up to a full stack keep the rule above. So
count the pile first and loose stacks after it: 2,448, then 12 from the inventory, makes 2,460,
and once the 12 are dropped into the pile, copying the pile at 2,460 says 2,460.

What stays wrong, and why the console prints the sum on every write: two stacks of one item with
equal counts count once; a stack of up to a full stack copied twice with its count changed in
between is added to itself; two piles of one item in two tabs count as the later one; and a loose
stack copied before the pile is dropped when the pile is copied.

The parser and `handle_copy` are plain functions over an injected clipboard, so the tests run
without Windows. The Windows calls go through `ctypes` from the standard library and are resolved
only when the companion starts with `--stacks`.
"""

from __future__ import annotations

import dataclasses
import re
import time
from typing import Callable

from vaalstreet.screen import DeskUnreachable

# The game's copy key. Ctrl+Alt+C, the copy with advanced details, holds the same two keys and
# copies the same header, so it counts too.
COPY_KEY = "<ctrl>+c"
# The game's window class. Its title is not used: an Explorer window on the game's folder or a
# browser tab about the game also starts with "Path of Exile", and a Ctrl+C there must not be read.
GAME_CLASS = "POEWindowClass"
FIRST_LINE = "Item Class: "  # every copied item starts with this, in an English client
RARITY = "Rarity: "
STACK = "Stack Size: "
SEPARATOR = "--------"
WAIT = 0.5  # seconds the game gets to write the clipboard after the key goes down
# Copies of one item count as "one after another" only this close together. Counting the stacks of a
# tab takes seconds; a count taken again after some trading must replace the old one.
ROW_SECONDS = 120.0
TICK = 0.01
CF_UNICODETEXT = 13


class NotAnItem(ValueError):
    """The clipboard holds something other than a copied item; it is dropped unseen."""


@dataclasses.dataclass
class Stack:
    item: str
    units: int
    full: int | None = None  # the size of a full stack, when the copied text gave it

    @property
    def pile(self) -> bool:
        """More than a full stack: it lies in a tab with one space for the item, not in a stack."""
        return self.full is not None and self.units > self.full

    def describe(self) -> str:
        """What was read, in a player's words, so a wrong item or count is easy to spot."""
        return f"{self.units:,} {self.item}"


# ------------------------------------------------------------------ parsing
def parse_item(text: str) -> Stack:
    """The item and stack size from the text the game copies.

    The text opens with the item's class, its rarity and its name, then sections split by a line of
    dashes; a stackable item has a "Stack Size: held/full stack" line. An item without that line is
    one item. The held count can carry a thousands separator, which differs by Windows language.
    """
    if not text.startswith(FIRST_LINE):
        raise NotAnItem("not item text")
    lines = [ln.strip() for ln in text.splitlines()]
    head = lines[1:lines.index(SEPARATOR)] if SEPARATOR in lines else lines[1:]
    head = [ln for ln in head if ln and not ln.startswith(RARITY)]
    if not head:
        raise NotAnItem("no item name")
    units, full = 1, None
    for line in lines:
        if line.startswith(STACK):
            held, _, of = line[len(STACK):].partition("/")
            held, of = re.sub(r"\D", "", held), re.sub(r"\D", "", of)
            if not held or int(held) < 1:
                raise NotAnItem("no stack size")
            units, full = int(held), int(of) if of and int(of) > 0 else None
            break
    return Stack(head[0], units, full)


# ------------------------------------------------------------------ wording
TITLE_ROOM = 60  # the desk cuts an event's title here; a spoken line should be short anyway


def _money(chaos: float, bridge: float | None, both: bool = True) -> str:
    """Chaos, or Divines (with the Chaos after it, if `both`) once the amount is worth a Divine or more."""
    in_chaos = f"{chaos:,.0f} Chaos" if chaos >= 100 else f"{chaos:.3g} Chaos"
    if bridge and chaos >= bridge:
        return f"{chaos / bridge:.3g} Divine" + (f" ({in_chaos})" if both else "")
    return in_chaos


def _hours(hours: float) -> str:
    if hours < 1:
        return "under an hour"
    return f"about {hours:.0f} hour{'s' if round(hours) != 1 else ''}"


def describe_worth(stack: Stack, worth: dict) -> tuple[str, list[str]]:
    """A short line that can be read aloud, and the lines the console shows under it."""
    if worth.get("value") is None:
        note = worth.get("note") or "The desk has no price for it."
        return f"{stack.describe()}: no price", [note]
    bridge = worth.get("bridge")
    lines = [f"Worth {_money(worth['value'], bridge)} at fair value "
             f"({_money(worth['low'], bridge)} to {_money(worth['high'], bridge)})."]
    best, tail = worth.get("best"), ""
    if best:
        other = "divine" if best == "chaos" else "chaos"
        if worth.get("best_by") is not None and worth.get(other):
            tail = f", {worth['best_by']:.0%} more for {'Divines' if best == 'divine' else 'Chaos'}"
            lines.append(f"The {best.title()} market pays {worth['best_by']:.0%} more: "
                         f"{_money(worth[best]['value'], bridge)} against {_money(worth[other]['value'], bridge)}.")
        else:
            lines.append(f"Only its {best.title()} market trades steadily: "
                         f"{_money(worth[best]['value'], bridge)} there.")
        hours = worth.get("hours")
        if hours is None:
            lines.append("That market's volume is unknown, so selling it all may take long.")
        elif worth.get("thin"):
            lines.append(f"Thin market: this stack is {_hours(hours)} of that market's volume, "
                         "so selling it all at that price will push the price.")
        else:
            lines.append(f"Selling it all there: {_hours(hours)} of that market's volume.")
    else:
        lines.append("Neither of its markets trades steadily, so that value is a rough guide.")
    rest = f": {_money(worth['value'], bridge, both=False)}{tail}"
    name = stack.item
    room = TITLE_ROOM - len(f"{stack.units:,} ") - len(rest)
    if len(name) > room:  # a long name gives way, so the amount and the better market are never cut
        name = name[: max(room - 1, 3)].rstrip() + "…"
    return f"{stack.units:,} {name}{rest}", lines


# ------------------------------------------------------------------ the stash
class Tally:
    """Adds up the stacks of one item that are copied one after another (see the module docstring)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last: tuple[str, int, int, float] | None = None  # item, its last count, running total, when

    def plan(self, stack: Stack) -> tuple[int | None, int | None]:
        """(the total to write, the total it adds to). No total: the same stack again, nothing to write.
        Nothing to add to: this copy sets the line."""
        last = self._last
        if not last or last[0] != stack.item or self._clock() - last[3] > ROW_SECONDS:
            return stack.units, None
        if last[1] == stack.units:
            return None, last[2]
        if stack.pile:
            return stack.units, None  # the pile counted again, with loot in it or an order out of it
        return last[2] + stack.units, last[2]

    def done(self, stack: Stack, total: int) -> None:
        """Call once the desk has taken the write (or the copy changed nothing), never before."""
        self._last = (stack.item, stack.units, total, self._clock())

    def forget(self) -> None:
        """A copy the stash cannot hold came in between: the next copy starts a new count."""
        self._last = None


def keep(stack: Stack, league: str, desk, tally: Tally, say: Callable[[str], None]) -> str | None:
    """Write the copied stack to the stash list by the tally's rule; the words for what happened."""
    total, before = tally.plan(stack)
    if total is None:
        tally.done(stack, before)
        words = (f"Stash unchanged: the same count as the copy before, so the same stack. "
                 f"{stack.item} stays at {before:,}.")
        try:
            # The same units written again: nothing changes but the time the line was last counted.
            desk.set_stash(stack.item, before, league)
        except RuntimeError:
            pass  # the line is right as it stands; only its "last counted" time is a little old
        say(f"    {words}")
        return words
    try:
        out = desk.set_stash(stack.item, total, league)
    except RuntimeError as err:
        say(f"    The stash was not changed: {err}")
        return None  # the tally has not moved, so copying the stack again tries again
    tally.done(stack, total)
    if before is not None:
        words = f"Stash: {stack.item} {before:,} + {stack.units:,} = {total:,} (another stack)."
    else:
        words = f"Stash: {stack.item} set to {total:,}."
        was = out.get("was")
        if was is not None and float(was) != total:
            words += f" It replaces the {float(was):,.10g} the list held."
        if stack.pile:
            words += f" More than a full stack of {stack.full:,}, so it is the pile in your tab: it never adds."
    if out.get("rehearsal"):
        words += " This is the practice stash: a rehearsal is running."
    say(f"    {words}")
    rows, value = out.get("rows") or [], (out.get("total") or {}).get("value")
    if value is not None:
        say(f"    Your stash list: {len(rows)} line{'s' if len(rows) != 1 else ''}, "
            f"worth {_money(value, out.get('bridge'))}.")
    return words


# ------------------------------------------------------------------ one press
def wait_changed(sequence: Callable[[], int], since: int, timeout: float = WAIT, tick: float = TICK,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> bool:
    """True once the clipboard's change counter has moved on from `since`; False after `timeout`.

    This watches the counter only. The clipboard's contents are not read here.
    """
    deadline = clock() + timeout
    while sequence() == since:
        if clock() >= deadline:
            return False
        sleep(tick)
    return True


def handle_copy(clip, since: int, desk, say: Callable[[str], None] = print,
                tally: Tally | None = None) -> dict | None:
    """Everything one press of the copy key does: at most one clipboard read, one valuation, one event.

    `clip` is the clipboard (`WindowsClipboard`, or a stand-in in tests); `since` is its change
    counter from the moment the key went down. Nothing is read unless the game is in front and the
    clipboard changed after the press. Text that is not an item is dropped without being shown.
    With a `tally` (`--stash`) the stack is also written to the stash list; without one, nothing is
    saved on the desk.
    """
    if not clip.game_in_front():
        return None
    if not wait_changed(clip.sequence, since):
        # The game copied nothing, so nothing is read. Said aloud, or a first try that fails is a riddle.
        say("\nCtrl+C heard in game, but nothing was copied: is the cursor over an item?")
        return None
    text = clip.read()
    try:
        stack = parse_item(text)
    except NotAnItem:
        say("  The copied text is not an item the desk can read (the game must be in English); ignored.")
        return None
    finally:
        del text
    say(f"\nCopied in game. Read: {stack.describe()}")
    try:
        worth = desk.worth(stack.item, stack.units)
    except RuntimeError as err:
        say(f"  {err}")
        # An item the desk does not know is a copy in between, and ends the row. A desk that could
        # not be reached is not: copying the stack again should carry on where the count stood.
        if tally is not None and not isinstance(err, DeskUnreachable):
            tally.forget()
        return None
    title, lines = describe_worth(stack, worth)
    for line in lines:
        say(f"    {line}")
    kept = keep(stack, worth["league"], desk, tally, say) if tally is not None else None
    desk.event(title, f"Read: {stack.describe()}. " + " ".join(lines[:2] + ([kept] if kept else [])), worth["league"])
    return worth


# ------------------------------------------------------------------ Windows
class WindowsClipboard:
    """The Windows clipboard and the window in front, through `ctypes`; nothing to install.

    `user32` and `kernel32` are the two Windows libraries; the tests pass stand-ins that count calls.
    By default each is loaded afresh, so the argument types set here do not change the shared
    `ctypes.windll` objects other libraries (the hotkey listener among them) call through.
    """

    def __init__(self, user32=None, kernel32=None):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        user32 = user32 or ctypes.WinDLL("user32")  # AttributeError off Windows
        kernel32 = kernel32 or ctypes.WinDLL("kernel32")
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self._user32, self._kernel32 = user32, kernel32

    def sequence(self) -> int:
        """How many times the clipboard has changed. A counter: it says nothing of the contents."""
        return int(self._user32.GetClipboardSequenceNumber())

    def game_in_front(self) -> bool:
        window = self._user32.GetForegroundWindow()
        if not window:
            return False
        name = self._ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(window, name, 256)
        return name.value == GAME_CLASS

    def read(self) -> str:
        """The clipboard's text, read once. Empty when it holds no text."""
        # The game may still hold the clipboard open while it writes. Opening is retried for a
        # moment; a failed open reads nothing, so this is still one read.
        deadline = time.monotonic() + WAIT
        while not self._user32.OpenClipboard(None):
            if time.monotonic() >= deadline:
                return ""
            time.sleep(TICK)
        try:
            handle = self._user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return self._ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()


def open_clipboard() -> WindowsClipboard:
    """The clipboard for the stack check, or SystemExit where it cannot work."""
    try:
        return WindowsClipboard()
    except (AttributeError, ImportError, OSError) as err:
        raise SystemExit("The stack check (--stacks) works only under Windows Python, next to the game: it reads "
                         "the Windows clipboard. Run the companion without --stacks here.") from err
