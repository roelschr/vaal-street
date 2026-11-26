"""The Gold each item costs on the exchange, from poedb's public Currency Exchange page.

Every order costs Gold, and the game charges a fixed number per item: 35 for a Stacked Deck,
250 for a Divine Orb, 15 for a Chaos Orb. GGG does not publish the list, but the game files carry
it and poedb.tw shows it on one page (https://poedb.tw/us/Currency_Exchange). The numbers were
checked against orders placed in game in November 2026: 13 Stacked Decks cost 455 Gold and one
Eldritch Exalted Orb cost 525, exactly what the page says.

This module fetches that one page, reads the item names and their numbers, and keeps the answer
in the data directory. It is a source like the exchange digests: the page as poedb served it is
kept next to the table read from it, so a fix to the reader (`VERSION`) reaches an existing
install without asking poedb again.

Rules we follow:
- One page, and nothing else on the site. poedb's robots.txt (read 2026-11-26) keeps crawlers
  out of only its edit-history paths.
- Fees change with game patches, not from hour to hour, so the page is asked for when there is
  no table yet, when the live league is not the one the table was fetched in, or when you ask
  (`vaalstreet fees refresh`). A failed try waits `RETRY_AFTER` before the next one.
- Every request carries the desk's User-Agent (`archive.user_agent`).
- A page we cannot read never replaces a table we could: the answer must name the Chaos Orb and
  the Divine Orb and hold at least `MIN_ITEMS` items, or the old table stays, marked stale.
- An item the page lists twice with different numbers has no fee here. Saying "unknown" is
  better than picking one.

The table says what one item costs. What a whole order costs is the desk's model (`desk.Costs`).
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from vaalstreet import items
from vaalstreet.archive import user_agent
from vaalstreet.paths import fees_file, fees_page

DEFAULT_URL = "https://poedb.tw/us/Currency_Exchange"
CREDIT = "Gold fees from poedb.tw"
VERSION = 1  # bump when `parse` learns something new: tables are read again from the kept page
MIN_ITEMS = 200  # the real page lists about a thousand; far fewer means the page changed shape
RETRY_AFTER = 6 * 3600  # seconds to wait after a failed fetch before asking again
REQUIRED = ("Chaos Orb", "Divine Orb")

# One entry on the page: the item's link, then its Gold in a <span>. Fractions ("1/8") are real:
# lifeforce and shards cost less than one Gold each.
_ENTRY = re.compile(r'<a\b[^>]*>([^<]+)</a>\s*<span>\s*([\d,]+)(?:\s*/\s*(\d+))?\s*</span>')


class FeesError(Exception):
    """The page could not be fetched or did not read as a fee table."""


@dataclass
class Table:
    """Gold per item by name. `fetched` and `league` say when and in which league poedb was asked;
    `error` is why the last try failed, and `stale` whether the table is older than it should be."""

    fees: dict[str, float] = field(default_factory=dict)
    fetched: int | None = None
    league: str | None = None
    checked: int | None = None
    error: str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        self._by_name = {name.casefold(): gold for name, gold in self.fees.items()}

    def __len__(self) -> int:
        return len(self.fees)

    def get(self, name: str) -> float | None:
        return self._by_name.get(name.casefold())

    def status(self) -> dict:
        return {"items": len(self), "fetched": self.fetched, "league": self.league, "checked": self.checked,
                "error": self.error, "stale": self.stale, "credit": CREDIT}


def parse(page: str, min_items: int | None = None) -> dict[str, float]:
    """Item name -> Gold, from the page's text. Raises `FeesError` if it does not look like the table."""
    min_items = MIN_ITEMS if min_items is None else min_items
    found: dict[str, float] = {}
    clash: set[str] = set()
    for name, whole, part in _ENTRY.findall(page):
        name = html.unescape(name).strip()
        gold = int(whole.replace(",", "")) / int(part or 1) if not part or int(part) else None
        if not name or gold is None:
            continue
        if name in found and found[name] != gold:
            clash.add(name)
        found.setdefault(name, gold)
    for name in clash:
        del found[name]
    missing = [name for name in REQUIRED if name not in found]
    if missing or len(found) < min_items:
        raise FeesError(f"The page did not read as a fee table ({len(found)} items"
                        + (f", no {' or '.join(missing)}" if missing else "") + ").")
    return found


def _download(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _save(root: Path, realm: str, stored: dict) -> None:
    _write(fees_file(root, realm), json.dumps(stored, ensure_ascii=False).encode("utf-8"))
    _read.cache_clear()


@lru_cache(maxsize=8)
def _read(root: Path, realm: str, stamp: float) -> dict:
    """The stored table. `stamp` is the file's modification time, so another process's fetch is seen."""
    path = fees_file(root, realm)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if stored.get("version") != VERSION and stored.get("fees"):
        # The reader changed since this was written: read the kept page again, without the network.
        try:
            stored = dict(stored, version=VERSION, fees=parse(gzip.decompress(fees_page(root, realm).read_bytes()).decode("utf-8")))
        except (OSError, ValueError, FeesError):
            pass  # keep what the older reader understood
    return stored


def _stored(root: Path, realm: str) -> dict:
    try:
        stamp = fees_file(root, realm).stat().st_mtime
    except OSError:
        return {}
    return _read(root, realm, stamp)


def load(root: Path, realm: str = "poe1", league: str | None = None) -> Table:
    """The table on disk, without touching the network. Empty if none was ever fetched.
    With `league`, the table is marked stale when it was fetched in another league."""
    stored = _stored(root, realm)
    stale = bool(stored.get("fees")) and league is not None and stored.get("league") != league
    return Table(stored.get("fees") or {}, stored.get("fetched"), stored.get("league"), stored.get("checked"),
                 stored.get("error"), stale)


def by_item(root: Path, realm: str = "poe1") -> dict[str, float]:
    """Item id -> Gold for every item the export names and the table lists."""
    table = load(root, realm)
    if not len(table):
        return {}
    out = {}
    for item_id, entry in items.load(root, realm).items():
        gold = table.get(entry["name"])
        if gold is not None:
            out[item_id] = gold
    return out


def due(root: Path, realm: str, league: str | None, now: float | None = None) -> bool:
    """Whether to ask poedb now: no table yet, or one from another league, and no failed try lately."""
    stored = _stored(root, realm)
    now = time.time() if now is None else now
    if stored.get("error") and now - (stored.get("checked") or 0) < RETRY_AFTER:
        return False
    return not stored.get("fees") or (league is not None and stored.get("league") != league)


def refresh(root: Path, realm: str = "poe1", league: str | None = None, *, url: str = DEFAULT_URL,
            force: bool = False, min_items: int | None = None, clock: Callable[[], float] = time.time) -> Table:
    """Fetch the page if it is due (or `force`), and return the table now on disk. Never raises:
    a failure keeps the old table and records why in `error`."""
    if not force and not due(root, realm, league, clock()):
        return load(root, realm, league)
    now = int(clock())
    try:
        page = _download(url)
        found = parse(page, min_items)
    except Exception as err:  # the network, the page's shape: none of it may stop the desk
        _save(root, realm, dict(_stored(root, realm), version=VERSION, checked=now, error=str(err) or type(err).__name__))
        return load(root, realm, league)
    _write(fees_page(root, realm), gzip.compress(page.encode("utf-8")))
    _save(root, realm, {"version": VERSION, "source": url, "fetched": now, "checked": now, "league": league,
                        "error": None, "fees": found})
    return load(root, realm, league)
