"""Screen check: read one offer off the Currency Exchange window when you press a key, and judge it.

This is the companion to the typed trade check (`check.py`). It runs as its own small process,
`vaalstreet screen`, next to the game, under Windows Python, because a Python inside WSL can neither
see the Windows screen nor hear Windows hotkeys. It talks to the desk over `http://localhost:<port>`
(WSL2 forwards localhost) and never opens the desk's archive or ledger itself.

GGG's rules for tools that read the screen shape the whole design (see docs/data-sources.md):
- One capture and one text-recognition pass per manual key press. There is no timer, no polling
  and no trigger on window or log changes. Presses that arrive while a check is still running are
  dropped, not queued, so a held key cannot turn into a stream of captures.
- The player knows what is read: the console says which rectangle is captured, and every verdict
  echoes what was read ("buy 64 Orb of Alteration for 1 Divine"), so a misread digit is caught.
- Nothing is sent to the game: no keys, clicks, chat or mouse moves. The verdict appears in the
  companion's console and as a desk event, which the hideout window shows and can read aloud.
- No game memory or files are read, and the screenshot lives in memory only.

The parser is the testable core. It is a pure function from recognised text lines to one offer,
matched against the desk's item names. The layout it assumes is the exchange window's labels:
"I Want" followed by an item, "I Have" followed by an item, and a "Market Ratio" row reading
"<want units> : <have units>", with an optional "Stock" number. It keys on those labels, not on
positions, because text recognition returns lines in a loose order and often drops spaces
("OrbofAlteration") or splits a row in two. When it is not sure, it fails with a message that tells
the player to press again or type the offer, rather than guessing a verdict.

The companion can also value a stack you copy in game with the game's own copy key
(`--stacks`, off unless you ask for it). That reads the clipboard, not the screen, once per press,
and lives in `clipboard.py`. Both keys go through one `PressGate`, so only one read runs at a time.

The optional libraries (`pip install "vaalstreet[screen]"`) are imported only when the companion
starts, so the rest of the package never needs them.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from vaalstreet.check import parse_ratio

CURRENCIES = {"chaosorb": "chaos", "divineorb": "divine"}
CHAOS, DIVINE = "Chaos Orb", "Divine Orb"
# Ctrl+Alt+C is the game's own "copy item with advanced details", and the listener does not swallow
# keys (the game sees every press too), so the default stays clear of the game's bindings.
DEFAULT_HOTKEY = "<ctrl>+<alt>+<f9>"
DEFAULT_DESK = "http://localhost:3000"
SETTINGS_FILE = "screen.json"
# A name is accepted only if it is this close to a known item, and clearly closer than the next
# best one: "Orb of Alteration" and "Orb of Augmentation" must never be confused.
NAME_CUTOFF = 0.85
NAME_MARGIN = 0.06
TRY_AGAIN = "press the key again, or type the offer with `vaalstreet check`."
INSTALL = ('The screen check needs its optional libraries. On Windows, in the Python you run it with:\n'
           '    pip install "vaalstreet[screen]"\n'
           '(from a checkout: pip install -e ".[screen]"). See "Screen check (optional)" in the README.')


class ScreenError(ValueError):
    """The recognised text could not be read with confidence; the message says what to do."""


@dataclasses.dataclass
class Reading:
    item: str
    market: str  # "chaos" or "divine": the currency the offer is priced in
    side: str  # "buy": you give currency for the item; "sell": you give the item for currency
    item_units: float
    currency_units: float
    stock: float | None = None

    def describe(self) -> str:
        """What was read, in the words a player would say it, so a misread is easy to spot."""
        text = (f"{self.side} {self.item_units:g} {self.item} for {self.currency_units:g} "
                f"{self.market.title()}")
        return text + (f" ({self.stock:g} on offer)" if self.stock else "")


# ------------------------------------------------------------------ parsing
def _key(text: str) -> str:
    """Case, spaces and punctuation removed: recognition often drops spaces between words."""
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def match_name(text: str, names: Iterable[str]) -> str | None:
    """The one item name `text` is, or None when no name is close enough or two are about as close."""
    key = _key(text)
    if len(key) < 3:
        return None
    keys = {}
    for name in names:
        keys.setdefault(_key(name), name)
    if key in keys:
        return keys[key]
    scored = sorted(((difflib.SequenceMatcher(None, key, k).ratio(), k) for k in keys), reverse=True)
    if not scored or scored[0][0] < NAME_CUTOFF:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < NAME_MARGIN:
        return None
    return keys[scored[0][1]]


# Digits as recognition misreads them; fixed only inside number tokens, never in names
# ("Orb of Alteration" holds both an O and an l).
_DIGITS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"})
_DIGITS_RUN = r"[0-9OolI|][0-9OolI|,.]*(?![0-9OolI|,.])"
_NUM = r"(?<![A-Za-z0-9])" + _DIGITS_RUN  # not the tail of a word
_RATIO_ROW = re.compile(rf"({_NUM})\s*[:;]\s*({_NUM})")
_STOCK = re.compile(rf"stock\s*[:;]?\s*({_DIGITS_RUN})?", re.I)  # "Stock1,280": spaces are often lost


def _number(token: str) -> float:
    """A recognised number, with letter-for-digit misreads fixed and thousands commas removed."""
    raw = token.strip().translate(_DIGITS).replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", raw):
        raw = raw.replace(",", "")
    raw = raw.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", raw):
        raise ValueError(token)
    return float(raw)


def _label(lines: list[str], label: str) -> int | None:
    for k, line in enumerate(lines):
        if _key(line).startswith(label):
            return k
    return None


def _item_after(lines: list[str], at: int, label: str, names: list[str], what: str) -> str:
    """The item named on the label's own line, or failing that on the next line."""
    rest = lines[at]
    stripped = re.sub(r"^\W*" + r"\W*".join(label) + r"\W*", "", rest, flags=re.I)
    for text in (stripped, lines[at + 1] if at + 1 < len(lines) else ""):
        if _key(text):
            name = match_name(text, names)
            if name:
                return name
            raise ScreenError(f"Couldn't tell which item \"{text.strip()}\" is (the {what} side); {TRY_AGAIN}")
    raise ScreenError(f"Couldn't read the item on the {what} side; {TRY_AGAIN}")


def parse_screen(text: str | Iterable[str], names: Iterable[str]) -> Reading:
    """One offer from the recognised text of the exchange window.

    `names` are the desk's item names for the league. Raises ScreenError with a plain message
    whenever a part cannot be read with confidence.
    """
    lines = [ln for ln in (text.splitlines() if isinstance(text, str) else list(text)) if ln.strip()]
    names = list(dict.fromkeys([CHAOS, DIVINE, *names]))
    want_at, have_at = _label(lines, "iwant"), _label(lines, "ihave")
    if want_at is None or have_at is None:
        raise ScreenError("Couldn't find the \"I Want\" and \"I Have\" labels. Check the captured area covers the "
                          f"exchange window (vaalstreet screen --calibrate), then {TRY_AGAIN}")
    want = _item_after(lines, want_at, "iwant", names, "\"I Want\"")
    have = _item_after(lines, have_at, "ihave", names, "\"I Have\"")
    if want == have:
        raise ScreenError(f"Read {want} on both sides; {TRY_AGAIN}")

    ratio_at = _label(lines, "marketratio")
    search = lines[ratio_at:] if ratio_at is not None else lines
    rows = []
    for k, line in enumerate(search):
        for m in _RATIO_ROW.finditer(line):
            try:
                rows.append((k, _number(m.group(1)), _number(m.group(2))))
            except ValueError:
                continue
    if not rows:
        raise ScreenError(f"Couldn't read the ratio; {TRY_AGAIN}")
    if ratio_at is None and len({r[1:] for r in rows}) > 1:
        raise ScreenError(f"Read more than one ratio and no \"Market Ratio\" label to pick one; {TRY_AGAIN}")
    row_k, want_units, have_units = rows[0]

    stock = None
    for line in search[row_k:row_k + 3]:
        m = _STOCK.search(line)
        if not m:
            continue
        token = m.group(1)
        if token is None:  # "Stock" on its own line, the number split onto the next one
            k = search.index(line)
            token = search[k + 1] if k + 1 < len(search) else ""
        try:
            stock = _number(token)
        except ValueError:
            stock = None  # stock is optional: a check without it is still a check
        break

    # The window reads want : have. Whichever side is Chaos or Divine is the market's currency;
    # Divine against Chaos is the Divine Orb's own Chaos market.
    want_cur, have_cur = CURRENCIES.get(_key(want)), CURRENCIES.get(_key(have))
    if want_cur and have_cur:
        item, market, side = DIVINE, "chaos", "buy" if want_cur == "divine" else "sell"
    elif have_cur:
        item, market, side = want, have_cur, "buy"
    elif want_cur:
        item, market, side = have, want_cur, "sell"
    else:
        raise ScreenError(f"Neither side is Chaos or Divine ({want} for {have}); the desk only judges offers "
                          "priced in Chaos or Divine.")
    item_side, money_side = (want_units, have_units) if side == "buy" else (have_units, want_units)
    ratio = f"{item_side:g}:{money_side:g}"
    item_units, currency_units = parse_ratio(ratio, market)  # the typed check's own ratio rules
    return Reading(item, market, side, item_units, currency_units, stock)


# ------------------------------------------------------------------ verdict
def verdict_title(result: dict) -> str:
    """A short spoken line, e.g. "Go: Chaos Orb at 140, 3% under the market"."""
    word = {"go": "Go", "caution": "Caution", "no-go": "No go"}.get(result.get("verdict"), "Check")
    price = result.get("price")
    tail = ""
    if result.get("gap") is not None:
        tail = f", {abs(result['gap']):.0%} {'under' if result['gap'] < 0 else 'over'} the market"
    at = f" at {price:.3g}" if price else ""
    room = 60 - len(word) - 2 - len(at) - len(tail)
    name = result.get("name") or ""
    if len(name) > room:
        name = name[: max(room - 1, 3)].rstrip() + "…"
    return f"{word}: {name}{at}{tail}"


# ------------------------------------------------------------------ desk client
class DeskClient:
    """The desk's local HTTP API, with the standard library only."""

    def __init__(self, base_url: str = DEFAULT_DESK, league: str | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.league = league
        self.timeout = timeout

    def _call(self, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            try:
                message = json.loads(err.read()).get("error") or str(err)
            except Exception:
                message = str(err)
            raise RuntimeError(message) from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Can't reach the desk at {self.base_url} ({err.reason}). Is `vaalstreet serve` "
                               "running?") from err

    def names(self) -> tuple[str, list[str]]:
        query = "?" + urllib.parse.urlencode({"league": self.league}) if self.league else ""
        out = self._call("/api/names" + query)
        return out["league"], out["names"]

    def check(self, reading: Reading, league: str) -> dict:
        body = dataclasses.asdict(reading) | {"league": league}
        return self._call("/api/check", body)

    def worth(self, item: str, units: float) -> dict:
        """What a holding is worth, for the stack check. Nothing is saved on the desk."""
        query = {"item": item, "units": f"{units:g}"} | ({"league": self.league} if self.league else {})
        return self._call("/api/worth?" + urllib.parse.urlencode(query))

    def event(self, title: str, body: str, league: str) -> dict:
        return self._call("/api/event", {"title": title, "body": body, "league": league})


# ------------------------------------------------------------------ one press
def handle_press(capture: Callable[[], object], recognise: Callable[[object], list[str]],
                 desk: DeskClient, say: Callable[[str], None] = print) -> dict | None:
    """Everything one key press does: one capture, one recognition pass, one check, one event.

    The screenshot is a local variable and is dropped when this returns; nothing is written to disk.
    """
    image = capture()
    lines = recognise(image)
    del image
    league, names = desk.names()
    try:
        reading = parse_screen(lines, names)
    except ScreenError as err:
        say(f"  {err}")
        return None
    say(f"  Read: {reading.describe()}")
    try:
        result = desk.check(reading, league)
    except RuntimeError as err:
        say(f"  The desk could not check it: {err}")
        return None
    title = verdict_title(result)
    say(f"  {title}")
    for reason in result.get("reasons", []):
        say(f"    - {reason}")
    desk.event(title, f"Read: {reading.describe()}. " + " ".join(result.get("reasons", [])[:2]), league)
    return result


class PressGate:
    """Hands hotkey presses to the main thread, one at a time.

    The hotkey callback runs inside the system keyboard hook, where slow work lags all input, so it
    only sets a flag. The main thread does the work. A press that arrives while a check is running
    is dropped, so one press is always one capture and nothing captures on its own.

    A press may bring its own job, as the copy key does for the stack check. It goes through the
    same gate, so a screen read and a clipboard read never run at once and neither queues.
    """

    def __init__(self):
        self._pressed = threading.Event()
        self._busy = threading.Lock()
        self._job: Callable[[], object] | None = None
        self.stopped = threading.Event()

    def press(self, job: Callable[[], object] | None = None) -> bool:
        """Called from a hotkey; True if the press was taken, False if a check was already running.

        `job` is what this press should do instead of the work `serve` was given.
        """
        if self._busy.locked() or self._pressed.is_set():
            return False
        self._job = job
        self._pressed.set()
        return True

    def stop(self) -> None:
        self.stopped.set()
        self._pressed.set()

    def serve(self, work: Callable[[], object]) -> None:
        """Run `work` once per taken press until stopped. It waits on the press; there is no timer."""
        while True:
            self._pressed.wait()
            if self.stopped.is_set():
                return
            with self._busy:
                job, self._job = self._job or work, None
                self._pressed.clear()
                try:
                    job()
                except Exception as err:  # one bad press must not end the companion
                    print(f"  That press failed: {err}")


# ------------------------------------------------------------------ settings and libraries
def load_settings(root: Path) -> dict:
    try:
        return json.loads((root / SETTINGS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(root: Path, settings: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / SETTINGS_FILE
    path.write_text(json.dumps(settings, indent=2))
    return path


def parse_region(text: str) -> dict:
    """"left top width height" (spaces or commas) as an mss region."""
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
        raise ValueError("Give four whole numbers: left top width height, e.g. \"600 200 700 500\".")
    left, top, width, height = map(int, parts)
    if width <= 0 or height <= 0:
        raise ValueError("Width and height must be more than zero.")
    return {"left": left, "top": top, "width": width, "height": height}


def load_libraries():
    """(mss, pynput.keyboard, RapidOCR) or SystemExit with the install line."""
    try:
        import mss
        from pynput import keyboard
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as err:
        raise SystemExit(f"Missing {err.name}. {INSTALL}") from err
    return mss, keyboard, RapidOCR


def make_capture(mss, region: dict | None):
    """A function that grabs the region (or the main monitor) once, into memory, as an RGB array."""
    import numpy as np

    def capture():
        with mss.mss() as shot:
            area = region or shot.monitors[1]
            raw = shot.grab(area)
            return np.asarray(raw)[:, :, 2::-1]  # BGRA to RGB, in memory only

    return capture


def make_recognise(RapidOCR):
    engine = RapidOCR()

    def recognise(image) -> list[str]:
        result, _ = engine(image)
        # Top to bottom, then left to right, so labels come before what follows them.
        boxes = sorted(result or [], key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
        return [r[1] for r in boxes]

    return recognise


def calibrate(root: Path, mss, ask: Callable[[str], str] = input) -> dict:
    """Capture once to learn the screen size, then save the rectangle the player types in."""
    with mss.mss() as shot:
        mon = shot.monitors[1]
        shot.grab(mon)  # one capture, so the size printed is the size the companion will see
    print(f"Your main screen is {mon['width']} x {mon['height']} pixels, starting at {mon['left']}, {mon['top']}.")
    print("Type the rectangle around the exchange window as: left top width height")
    print(f"(press Enter for the whole screen; e.g. \"{mon['width'] // 4} {mon['height'] // 6} "
          f"{mon['width'] // 2} {mon['height'] * 2 // 3}\")")
    settings = load_settings(root)
    answer = ask("> ").strip()
    settings["region"] = parse_region(answer) if answer else None
    path = save_settings(root, settings)
    print(f"Saved to {path}.")
    return settings


def run(root: Path, desk_url: str = DEFAULT_DESK, hotkey: str | None = None, league: str | None = None,
        do_calibrate: bool = False, region: str | None = None, stacks: bool = False) -> None:
    """The companion: wait for the hotkey, and do one check per press until Ctrl+C.

    With `stacks`, the game's own copy key also values the stack under the cursor (`clipboard.py`).
    """
    mss, keyboard, RapidOCR = load_libraries()
    if do_calibrate:
        calibrate(root, mss)
        return
    settings = load_settings(root)
    if region:
        settings["region"] = parse_region(region)
        save_settings(root, settings)
    hotkey = hotkey or settings.get("hotkey") or DEFAULT_HOTKEY
    area = settings.get("region")
    desk = DeskClient(desk_url, league)
    league_name, names = desk.names()  # fail early if the desk is not running
    recognise = make_recognise(RapidOCR)
    capture = make_capture(mss, area)
    where = (f"the rectangle {area['width']} x {area['height']} at {area['left']}, {area['top']}"
             if area else "your whole main screen")
    print(f"Screen check for {league_name}, talking to the desk at {desk.base_url}.")
    print(f"Each press of {hotkey} takes one screenshot of {where}, reads its text, and checks the offer.")
    print("The screenshot stays in memory and is dropped after the check. Nothing is sent to the game.")
    gate = PressGate()

    def work():
        print("\nReading the screen…")
        handle_press(capture, recognise, desk)

    hotkeys = {hotkey: gate.press}
    if stacks:
        from vaalstreet import clipboard

        clip = clipboard.open_clipboard()

        def copied(since: int) -> None:
            clipboard.handle_copy(clip, since, desk)

        # The counter is noted here, inside the key press, before the game has written the clipboard.
        hotkeys[clipboard.COPY_KEY] = lambda: gate.press(functools.partial(copied, clip.sequence()))
        print("Stack check is on: while the game is the window in front, each press of Ctrl+C over an item reads "
              "the clipboard once.")
        print("It reads the item's name and the size of the stack, and shows what the stack is worth. Anything "
              "else on the clipboard is dropped: never shown, never sent to the desk. You press the key; nothing is "
              "sent to the game.")
    print("Press Ctrl+C here to stop.")
    listener = keyboard.GlobalHotKeys(hotkeys)
    listener.start()
    try:
        gate.serve(work)
    except KeyboardInterrupt:
        pass
    finally:
        gate.stop()
        listener.stop()
