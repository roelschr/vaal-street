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

The parser and `handle_copy` are plain functions over an injected clipboard, so the tests run
without Windows. The Windows calls go through `ctypes` from the standard library and are resolved
only when the companion starts with `--stacks`.
"""

from __future__ import annotations

import dataclasses
import re
import time
from typing import Callable

# The game's copy key. Ctrl+Alt+C, the copy with advanced details, holds the same two keys and
# copies the same header, so it counts too.
COPY_KEY = "<ctrl>+c"
# What the game's window is called: its window class, or failing that the start of its title.
GAME_CLASS = "POEWindowClass"
GAME_TITLE = "Path of Exile"
FIRST_LINE = "Item Class: "  # every copied item starts with this, in an English client
RARITY = "Rarity: "
STACK = "Stack Size: "
SEPARATOR = "--------"
WAIT = 0.5  # seconds the game gets to write the clipboard after the key goes down
TICK = 0.01
CF_UNICODETEXT = 13


class NotAnItem(ValueError):
    """The clipboard holds something other than a copied item; it is dropped unseen."""


@dataclasses.dataclass
class Stack:
    item: str
    units: int

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
    units = 1
    for line in lines:
        if line.startswith(STACK):
            held = re.sub(r"\D", "", line[len(STACK):].split("/")[0])
            if not held or int(held) < 1:
                raise NotAnItem("no stack size")
            units = int(held)
            break
    return Stack(head[0], units)


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


def handle_copy(clip, since: int, desk, say: Callable[[str], None] = print) -> dict | None:
    """Everything one press of the copy key does: at most one clipboard read, one valuation, one event.

    `clip` is the clipboard (`WindowsClipboard`, or a stand-in in tests); `since` is its change
    counter from the moment the key went down. Nothing is read unless the game is in front and the
    clipboard changed after the press. Text that is not an item is dropped without being shown.
    """
    if not clip.game_in_front():
        return None
    if not wait_changed(clip.sequence, since):
        return None  # the game copied nothing: there was no item under the cursor
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
        return None
    title, lines = describe_worth(stack, worth)
    for line in lines:
        say(f"    {line}")
    desk.event(title, f"Read: {stack.describe()}. " + " ".join(lines[:2]), worth["league"])
    return worth


# ------------------------------------------------------------------ Windows
class WindowsClipboard:
    """The Windows clipboard and the window in front, through `ctypes`; nothing to install."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # AttributeError off Windows
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
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
        if name.value == GAME_CLASS:
            return True
        self._user32.GetWindowTextW(window, name, 256)
        return name.value.startswith(GAME_TITLE)

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
