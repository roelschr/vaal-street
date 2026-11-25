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
- After each press the companion tells the local desk what the recognition returned: the text
  lines (at most `MAX_LINES` of `MAX_CHARS`), what it understood or why not. The page shows the last
  few, so a misread can be seen and taught (a correction: "when it reads this, it means that item").
  The desk keeps them in memory only. The screenshot itself is never sent, stored or shown.
- An understood reading can be recorded as an order you posted, from the desk's page, by a click
  there. The companion records nothing itself. When the amounts you typed under "I Want" and "I Have"
  can be read without doubt (`player_amounts`), the form starts from them; where they appear in the
  recognised text has not been seen in game, so that reading is strict and gives nothing when unsure.

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
import unicodedata
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
# What one reading may send to the desk: plenty for one exchange window, and a bound on what is kept.
MAX_LINES = 60
MAX_CHARS = 120
LABELS = ("iwant", "ihave")
TRY_AGAIN = "press the key again, or type the offer with `vaalstreet check`."
INSTALL = ('The screen check needs its optional libraries. On Windows, in the Python you run it with:\n'
           '    pip install "vaalstreet[screen]"\n'
           '(from a checkout: pip install -e ".[screen]"). See "Screen check (optional)" in the README.')


class ScreenError(ValueError):
    """The recognised text could not be read with confidence; the message says what to do.

    `text` is the item text that matched no name, when that was the trouble: the page offers it as
    the text of a correction.
    """

    def __init__(self, message: str, text: str | None = None):
        super().__init__(message)
        self.text = text


class DeskUnreachable(RuntimeError):
    """The desk did not answer at all, as opposed to answering with an error."""


@dataclasses.dataclass
class Reading:
    item: str
    market: str  # "chaos" or "divine": the currency the offer is priced in
    side: str  # "buy": you give currency for the item; "sell": you give the item for currency
    item_units: float
    currency_units: float
    stock: float | None = None
    # The amounts the player typed into the window, under "I Want" and "I Have", when both were read
    # without doubt (`player_amounts`); otherwise both None. Not used by the check: the ratio above is.
    want_amount: int | None = None
    have_amount: int | None = None

    def describe(self) -> str:
        """What was read, in the words a player would say it, so a misread is easy to spot."""
        text = (f"{self.side} {self.item_units:g} {self.item} for {self.currency_units:g} "
                f"{self.market.title()}")
        return text + (f" ({self.stock:g} on offer)" if self.stock else "")


# ------------------------------------------------------------------ parsing
def _key(text: str) -> str:
    """Case, spaces and punctuation removed: recognition often drops spaces between words."""
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def correction_key(text: str) -> str:
    """The key a correction is saved under: the text as `match_name` sees it, without a leading label.

    "I Want: Exaltd Orb" on one line reaches `match_name` as "Exaltd Orb", so both are keyed "exaltdorb".
    """
    key = _key(text)
    for label in LABELS:
        if key.startswith(label) and len(key) > len(label):
            return key[len(label):]
    return key


def match_name(text: str, names: Iterable[str], corrections: dict[str, str] | None = None) -> str | None:
    """The one item name `text` is, or None when no name is close enough or two are about as close.

    `corrections` (normalised text -> item name) are the player's own: "when it reads this, it means
    that item". Only an exact match of the text counts, and only for an item among `names`; a text
    merely like a corrected one still goes through the similarity rule below.
    """
    key = _key(text)
    if len(key) < 3:
        return None
    keys = {}
    for name in names:
        keys.setdefault(_key(name), name)
    meant = (corrections or {}).get(key)
    if meant is not None and _key(meant) in keys:
        return keys[_key(meant)]
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
_NOT_A_NAME = ("iwant", "ihave", "marketratio", "placeorder", "availabletrades", "competingtrades")
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


# How far under the labels the two names are looked for. The window's own rows end within a few
# lines; the rest of a whole-screen capture (the desk's page can be on screen too) holds other names.
NAME_WINDOW = 14
NEARLY = 0.6
_EMPTY_SIDE = ("selectcurrency", "clicktoselect")


def _wordy(text: str) -> bool:
    """Text that could be a name: letters, and not a number or a ratio with misread digits."""
    return bool(_key(text)) and not re.fullmatch(rf"\W*{_DIGITS_RUN}(\s*[:;]\s*{_DIGITS_RUN})?\W*", text.strip())


def _nearly_a_name(text: str, names: list[str]) -> bool:
    """Close enough to some item's name to be a misread of it, as opposed to other text on screen."""
    key = _key(text)
    return len(key) >= 4 and any(difflib.SequenceMatcher(None, key, _key(n)).ratio() >= NEARLY for n in names)


def _strip_label(text: str, label: str) -> str:
    return re.sub(r"^\W*" + r"\W*".join(label) + r"\W*", "", text, flags=re.I)


def _find_items(lines: list[str], want_at: int, have_at: int, names: list[str],
                corrections: dict[str, str] | None = None) -> tuple[str, str, int | None, int | None]:
    """(item wanted, item offered, and the line each name stands on when it has a line of its own).

    The game's window, as its recognised text reads top to bottom and left to right (seen in game,
    November 2026): "MARKET RATIO"; then "I WANT", the market's ratio, "I HAVE" on one row; then one
    row with the wanted item's name, its amount, the ratio, the other amount, and the offered item's
    name. So both names come after both labels, the wanted one first. A name on a label's own line,
    or between the two labels, is read as that label's item, which is how a typed-out offer reads.
    """
    first, second = sorted((("iwant", want_at), ("ihave", have_at)), key=lambda x: x[1])
    found: dict[str, tuple[str, int | None]] = {}
    for label, at in (first, second):
        text = _strip_label(lines[at], label)
        if _wordy(text):
            name = match_name(text, names, corrections)
            if name:
                found[label] = (name, None)
    end = min(len(lines), second[1] + 1 + NAME_WINDOW)
    window = [k for k in range(first[1] + 1, end) if k != second[1]]
    if any(_key(lines[k]).startswith(_EMPTY_SIDE) for k in window):
        raise ScreenError("One side of the exchange window has no item yet. Pick both items in game, then press the key again.")
    # Text right under a label that is words but no known name is a misread name: say which, so the
    # player can teach it. (In the game's own layout a ratio or the other label stands there.)
    for label, at in (first, second):
        k = at + 1
        if label not in found and k < len(lines) and k != second[1] and _wordy(lines[k]) \
                and not _key(lines[k]).startswith(_NOT_A_NAME):
            name = match_name(lines[k], names, corrections)
            if name:
                found[label] = (name, k)
            elif (k < second[1] or label == second[0]) and _nearly_a_name(lines[k], names):
                what = '"I Want"' if label == "iwant" else '"I Have"'
                raise ScreenError(f"Couldn't tell which item \"{lines[k].strip()}\" is (the {what} side); {TRY_AGAIN}",
                                  text=lines[k].strip())
    taken = {k for _, k in found.values() if k is not None}
    named = [(k, match_name(lines[k], names, corrections)) for k in window if k not in taken and _wordy(lines[k])]
    named = [(k, n) for k, n in named if n]
    between = [x for x in named if x[0] < second[1]]
    after = [x for x in named if x[0] > second[1]]
    if first[0] not in found and between:
        found[first[0]] = (between[0][1], between[0][0])
    # What is left comes in reading order: the wanted item stands on the left, so it comes first.
    for label in ("iwant", "ihave"):
        if label not in found and after:
            k, name = after.pop(0)
            found[label] = (name, k)
    if "iwant" not in found or "ihave" not in found:
        used = {k for _, k in found.values() if k is not None}
        for k in window:  # a line that is nearly a name is the likely misread: offer it for teaching
            if k not in used and _wordy(lines[k]) and not _key(lines[k]).startswith(_NOT_A_NAME) \
                    and _nearly_a_name(lines[k], names):
                raise ScreenError(f"Couldn't tell which item \"{lines[k].strip()}\" is; {TRY_AGAIN}", text=lines[k].strip())
        side = "either side" if not found else ('the "I Want" side' if "iwant" not in found else 'the "I Have" side')
        raise ScreenError(f"Couldn't read the item on {side}. If its name was misread, click that line under "
                          f"\"What the screen check read\" on the desk and say which item it is; then {TRY_AGAIN}")
    return found["iwant"][0], found["ihave"][0], found["iwant"][1], found["ihave"][1]


def parse_screen(text: str | Iterable[str], names: Iterable[str],
                 corrections: dict[str, str] | None = None) -> Reading:
    """One offer from the recognised text of the exchange window.

    `names` are the desk's item names for the league, `corrections` the player's own (see
    `match_name`). Raises ScreenError with a plain message whenever a part cannot be read with
    confidence.
    """
    # Recognition returns some punctuation in its full-width form ("1：2"); fold it to the plain one.
    lines = [unicodedata.normalize("NFKC", ln) for ln in (text.splitlines() if isinstance(text, str) else list(text))
             if ln.strip()]
    names = list(dict.fromkeys([CHAOS, DIVINE, *names]))
    want_at, have_at = _label(lines, "iwant"), _label(lines, "ihave")
    if want_at is None or have_at is None:
        raise ScreenError("Couldn't find the \"I Want\" and \"I Have\" labels. Check the captured area covers the "
                          f"exchange window (vaalstreet screen --calibrate), then {TRY_AGAIN}")
    want, have, want_k, have_k = _find_items(lines, want_at, have_at, names, corrections)
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
    want_amount, have_amount = _row_amounts(lines, want_k, have_k)
    if want_amount is None:
        want_amount, have_amount = player_amounts(lines, names, want, have, corrections)
    return Reading(item, market, side, item_units, currency_units, stock, want_amount, have_amount)


def _row_amounts(lines: list[str], want_k: int | None, have_k: int | None) -> tuple[int | None, int | None]:
    """The two amounts on the game's own row: wanted name, amount, ratio, amount, offered name.

    Only that exact shape counts: one whole number on each side of one ratio, between the two names.
    """
    if want_k is None or have_k is None or not 0 < have_k - want_k <= 6:
        return None, None
    between = [ln for ln in lines[want_k + 1:have_k] if not _wordy(ln) or _RATIO_ROW.search(ln)]
    if len(between) != 3 or not _RATIO_ROW.fullmatch(between[1].strip()):
        return None, None
    a, b = _amount(between[0].strip()), _amount(between[2].strip())
    return (a, b) if a is not None and b is not None else (None, None)


# The player's own amounts in an offer typed out label by label. This is
# strict: a whole number with at least one real digit, standing alone on a line or right after the
# item's name, between the side's label and the next label. Anything less sure gives nothing.
_CLOSERS = ("iwant", "ihave", "marketratio", "stock")
_AMOUNT = re.compile(r"(?<![A-Za-z0-9])([0-9OolI|]{1,3}(?:,[0-9OolI|]{3})+|[0-9OolI|]+)\s*$")


def _amount(token: str) -> int | None:
    if not re.search(r"\d", token):
        return None  # a lone "O", "l" or "I" is a letter, not a number
    try:
        value = _number(token)
    except ValueError:
        return None
    return int(value) if value == int(value) and value > 0 else None


def _side_amount(lines: list[str], at: int, label: str, item: str, names: list[str],
                 corrections: dict[str, str] | None) -> int | None:
    """The one amount under a label, or None when there is none, more than one, or the region has no end."""
    end = next((k for k in range(at + 1, len(lines)) if _key(lines[k]).startswith(_CLOSERS)), None)
    if end is None:
        return None
    item_ends_in_digit = _key(item)[-1:].isdigit()
    found = []
    for k in range(at, end):
        text = lines[k]
        if k == at:
            text = re.sub(r"^\W*" + r"\W*".join(label) + r"\W*", "", text, flags=re.I)
        m = _AMOUNT.search(text)
        if not m:
            continue
        before = text[:m.start()].strip()
        if before:
            # "Exalted Orb 5": the number must follow the item's own name, and must not be part of it.
            if item_ends_in_digit or match_name(before, names, corrections) != item:
                return None
        value = _amount(m.group(1))
        if value is None:
            return None
        found.append(value)
    return found[0] if len(found) == 1 else None


def player_amounts(lines: list[str], names: list[str], want: str, have: str,
                   corrections: dict[str, str] | None = None) -> tuple[int | None, int | None]:
    """(amount under "I Want", amount under "I Have"): both read without doubt, or (None, None)."""
    try:
        want_at, have_at = _label(lines, "iwant"), _label(lines, "ihave")
        if want_at is None or have_at is None:
            return None, None
        a = _side_amount(lines, want_at, "iwant", want, names, corrections)
        b = _side_amount(lines, have_at, "ihave", have, names, corrections)
    except Exception:  # the amounts are extra: they must never cost the player the reading itself
        return None, None
    return (a, b) if a is not None and b is not None else (None, None)


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
        self.corrections: dict[str, str] = {}  # the player's own, refreshed with every `names()`

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
            raise DeskUnreachable(f"Can't reach the desk at {self.base_url} ({err.reason}). Is `vaalstreet serve` "
                                  "running?") from err

    def names(self) -> tuple[str, list[str]]:
        query = "?" + urllib.parse.urlencode({"league": self.league}) if self.league else ""
        out = self._call("/api/names" + query)
        self.corrections = dict(out.get("corrections") or {})  # an older desk sends none
        return out["league"], out["names"]

    def check(self, reading: Reading, league: str) -> dict:
        body = dataclasses.asdict(reading) | {"league": league}
        return self._call("/api/check", body)

    def worth(self, item: str, units: float) -> dict:
        """What a holding is worth, for the stack check. Nothing is saved on the desk."""
        query = {"item": item, "units": f"{units:g}"} | ({"league": self.league} if self.league else {})
        return self._call("/api/worth?" + urllib.parse.urlencode(query))

    def set_stash(self, item: str, units: float, league: str) -> dict:
        """Set one line of the stash list (the stack check with --stash); answers with the list, valued."""
        return self._call("/api/stash", {"league": league, "item": item, "units": units})

    def event(self, title: str, body: str, league: str) -> dict:
        return self._call("/api/event", {"title": title, "body": body, "league": league})

    def reading(self, report: dict) -> dict:
        """Tell the desk what one press read (`reading_report`); it keeps the last few in memory."""
        return self._call("/api/reading", report)


def reading_report(lines: Iterable[str], reading: Reading | None, error: str | None, league: str,
                   unmatched: str | None = None) -> dict:
    """What the desk is told about one press: the recognised lines, cut to `MAX_LINES` of `MAX_CHARS`,
    in the order the recognition returned them, and what was understood or why not. Never the screenshot."""
    kept = [str(line)[:MAX_CHARS] for line in list(lines)[:MAX_LINES]]
    return {"kind": "screen", "league": league, "lines": kept,
            "reading": dataclasses.asdict(reading) if reading else None,
            "said": reading.describe() if reading else None, "error": error,
            "unmatched": unmatched[:MAX_CHARS] if unmatched else None}


def tell_desk(desk, report: dict) -> bool:
    """Send a reading to the desk; True if it took it. A desk that cannot take it (one older than the
    companion, or a test stand-in) is not told, and the press goes on as before."""
    send = getattr(desk, "reading", None)
    if send is None:
        return False
    try:
        send(report)
    except RuntimeError:
        return False
    return True


# ------------------------------------------------------------------ one press
def handle_press(capture: Callable[[], object], recognise: Callable[[object], list[str]],
                 desk: DeskClient, say: Callable[[str], None] = print) -> dict | None:
    """Everything one key press does: one capture, one recognition pass, one check, one event.

    The screenshot is a local variable and is dropped when this returns; nothing is written to disk.
    The desk is told the recognised lines and what they were understood as, success or not, so the
    player can see a misread on the page; the screenshot itself is never sent.
    """
    image = capture()
    lines = recognise(image)
    del image
    league, names = desk.names()
    try:
        reading = parse_screen(lines, names, getattr(desk, "corrections", None))
    except ScreenError as err:
        say(f"  {err}")
        tell_desk(desk, reading_report(lines, None, str(err), league, err.text))
        return None
    except ValueError as err:
        # The ratio rules' own refusal (a misread "O:1" reads as zero): shown on the page too, then
        # reported in the console as before.
        tell_desk(desk, reading_report(lines, None, str(err), league))
        raise
    told = tell_desk(desk, reading_report(lines, reading, None, league))
    hint = ("  Posting this order? Record it from the desk's hideout window: \"Posted this order…\" under "
            "\"What the screen check read\". Nothing is recorded until you click it.")
    say(f"  Read: {reading.describe()}")
    try:
        result = desk.check(reading, league)
    except RuntimeError as err:
        say(f"  The desk could not check it: {err}")
        if told:
            say(hint)
        return None
    title = verdict_title(result)
    say(f"  {title}")
    for reason in result.get("reasons", []):
        say(f"    - {reason}")
    if told:
        say(hint)
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
        """Run `work` once per taken press until stopped. Nothing runs without a press.

        The wait wakes twice a second and goes back to waiting. On Windows a wait with no end never
        hears Ctrl+C in the console, so the companion could not be stopped.
        """
        while True:
            while not self._pressed.wait(0.5):
                pass
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


def reading_order(boxes: Iterable[tuple[float, float, float, str]]) -> list[str]:
    """Recognised texts, top to bottom by row and left to right within a row.

    `boxes` are (left, top, bottom, text). Two texts are on one row when their vertical centres are
    closer than half the taller one's height. The parser depends on this: the wanted item's name and
    the offered item's stand on one row, a few pixels apart in height, and the left one is the wanted.
    (Rounding each top edge to a grid, as before, put them in the wrong order about every other press.)
    """
    rows: list[list] = []  # [centre of the row's first text, its height, [(left, text), ...]]
    for left, top, bottom, text in sorted(boxes, key=lambda b: (b[1] + b[2]) / 2):
        centre, height = (top + bottom) / 2, bottom - top
        if rows and centre - rows[-1][0] <= max(height, rows[-1][1]) / 2:
            rows[-1][2].append((left, text))
        else:
            rows.append([centre, height, [(left, text)]])
    return [text for _, _, row in rows for _, text in sorted(row, key=lambda x: x[0])]


def make_recognise(RapidOCR):
    engine = RapidOCR()

    def recognise(image) -> list[str]:
        result, _ = engine(image)
        # Each result is (four corner points, text, score); the corners start top left, clockwise.
        return reading_order((min(p[0] for p in r[0]), min(p[1] for p in r[0]), max(p[1] for p in r[0]), r[1])
                             for r in result or [])

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
        do_calibrate: bool = False, region: str | None = None, stacks: bool = False, stash: bool = False,
        why: bool = False) -> None:
    """The companion: wait for the hotkey, and do one check per press until Ctrl+C.

    With `stacks`, the game's own copy key also values the stack under the cursor (`clipboard.py`).
    With `stash`, which turns `stacks` on, each copied stack is also written to the stash list.
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
    if stacks or stash:
        from vaalstreet import clipboard

        clip = clipboard.open_clipboard()
        tally = clipboard.Tally() if stash else None

        def copied(since: int) -> None:
            clipboard.handle_copy(clip, since, desk, tally=tally, why=why)

        # The counter is noted here, inside the key press, before the game has written the clipboard.
        hotkeys[clipboard.COPY_KEY] = lambda: gate.press(functools.partial(copied, clip.sequence()))
        print("Stack check is on: while the game is the window in front, each press of Ctrl+C over an item reads "
              "the clipboard once.")
        print("It reads the item's name and the size of the stack, and shows what the stack is worth. Anything "
              "else on the clipboard is dropped: never shown, never sent to the desk. You press the key; nothing is "
              "sent to the game.")
        if stash:
            print("Each copied stack is also written to your stash list on the desk (--stash). A copy sets the "
                  "item's line to the count read, replacing what the line held.")
            print(f"The same item copied again within {clipboard.ROW_SECONDS / 60:g} minutes with a different count "
                  "is another stack and adds; with the same count it is the same stack and changes nothing.")
            print("A count above a full stack is the pile in a tab with one space for the item: it always sets the "
                  "line, so loot dropped into it never doubles. Count the pile first, loose stacks after it.")
            print("To start an item's count over, copy another item first, or edit the line on the Ledger page.")
        else:
            print("Nothing is saved. Start with --stash to write copied stacks to your stash list.")
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
