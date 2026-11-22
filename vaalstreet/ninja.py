"""Prices for things the exchange does not trade, from poe.ninja's public economy API.

Some divination card sets turn into a unique, a skill gem or a base type, and none of those trade
on the Currency Exchange. poe.ninja prices them from stash listings, so this module asks it for
the few categories the desk's card rewards need and keeps the answers in a cache file.

Rules we follow (https://poe.ninja/docs/api, read 2026-11-21):
- Only the economy endpoints: leagues, exchange overview, stash item overview and stash currency
  overview. Never the builds or profiles API.
- At most one request per endpoint and category an hour (their PoE 1 data refreshes about every
  15 minutes), sent as a conditional request with the ETag we last saw, so an unchanged answer
  costs them a 304 and no body.
- One request at a time with a pause between them, and back off on 429 and 5xx, honouring
  `Retry-After`. A long wait is not slept out: no list is asked for again before then, so a
  rate limit or an outage on one list stops the others too.
- Every request carries the desk's User-Agent with the project's address (`archive.user_agent`).

Their docs ask desktop apps to go through their own backend. The desk's local server is that
backend: API handlers read only the cache, and the sync loop alone talks to poe.ninja. Each
install still calls poe.ninja itself, at most hourly per category.

If poe.ninja is unreachable the last answer is used and marked stale. A stash listing price is
weaker evidence than exchange trades, so these prices never enter fair value; they are shown only
on the card sets whose reward has no exchange market, marked as coming from poe.ninja.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from vaalstreet.archive import retry_after, user_agent
from vaalstreet.paths import ninja_file

DEFAULT_BASE_URL = "https://poe.ninja"
LEAGUES = "/poe1/api/economy/leagues"
EXCHANGE = "/poe1/api/economy/exchange/current/overview"
STASH_ITEMS = "/poe1/api/economy/stash/current/item/overview"
STASH_CURRENCY = "/poe1/api/economy/stash/current/currency/overview"

MIN_INTERVAL = 3600  # seconds between two requests for the same endpoint and category
STALE_AFTER = 3 * 3600  # an answer older than this is marked stale even if the last request worked
DELAY = 2.0  # pause between two requests
MAX_WAIT = 60.0  # longest back-off slept out; a longer Retry-After postpones the next try instead
ATTEMPTS = 3
HOST_PAUSE = 900  # after a failure without Retry-After, ask poe.ninja nothing for this long
HOST = ("*", "")  # cache key holding the host-wide pause: a 429 on one list holds back every list

CREDIT = "prices from poe.ninja"
NOTE = "reward priced by poe.ninja (stash listings, not the exchange)"

# Card text never names a unique's base, so a unique reward may sit in any of these lists.
UNIQUE_TYPES = ("UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel", "UniqueMap")
KIND_TYPES = {"unique": UNIQUE_TYPES, "gem": ("SkillGem",), "base": ("BaseType",)}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses(
    endpoint TEXT NOT NULL, params TEXT NOT NULL,
    etag TEXT, body BLOB,
    fetched INTEGER,               -- when poe.ninja last sent or confirmed (304) this body
    checked INTEGER NOT NULL,      -- when we last asked, whatever the outcome
    not_before INTEGER NOT NULL DEFAULT 0,  -- a Retry-After we are honouring
    error TEXT,                    -- why the last request failed, if it did
    PRIMARY KEY(endpoint, params));
"""

# One request at a time across every client in the process, and a pause between them.
_ONE_AT_A_TIME = threading.Lock()
_last_request = [0.0]


@dataclass
class Cached:
    body: Any
    fetched: int | None
    stale: bool
    error: str | None


class Ninja:
    """A polite, cached client. `refresh=False` reads the cache only and never touches the network."""

    def __init__(self, root: Path, base_url: str = DEFAULT_BASE_URL, *, min_interval: float = MIN_INTERVAL,
                 delay: float = DELAY, max_wait: float = MAX_WAIT, attempts: int = ATTEMPTS,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval, self.delay, self.max_wait, self.attempts = min_interval, delay, max_wait, attempts
        self.clock, self.sleep = clock, sleep
        path = ninja_file(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.Lock()  # short; cache reads never wait on a request in flight
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(_SCHEMA)

    # ------------------------------------------------------------ endpoints
    def leagues(self, refresh: bool = True) -> Cached:
        return self.get(LEAGUES, {}, refresh=refresh)

    def league_id(self, league: str, refresh: bool = True) -> str | None:
        """poe.ninja's id for one of our league names, or None when their list has no such league.
        Never guesses: their first entry is the challenge league, but it may not be ours."""
        for entry in self.leagues(refresh).body or []:
            if league.lower() in (str(entry.get("name", "")).lower(), str(entry.get("id", "")).lower()):
                return entry.get("id")
        return None

    def exchange_overview(self, league_id: str, kind: str, refresh: bool = True) -> Cached:
        return self.get(EXCHANGE, {"league": league_id, "type": kind}, refresh=refresh)

    def item_overview(self, league_id: str, kind: str, refresh: bool = True) -> Cached:
        return self.get(STASH_ITEMS, {"league": league_id, "type": kind}, refresh=refresh)

    def currency_overview(self, league_id: str, kind: str, refresh: bool = True) -> Cached:
        return self.get(STASH_CURRENCY, {"league": league_id, "type": kind}, refresh=refresh)

    # ---------------------------------------------------------------- cache
    def _row(self, endpoint: str, key: str) -> sqlite3.Row | None:
        with self._db_lock:
            return self._db.execute("SELECT * FROM responses WHERE endpoint = ? AND params = ?", (endpoint, key)).fetchone()

    def paused_until(self) -> int:
        """When poe.ninja may be asked again after a failure (0: now)."""
        row = self._row(*HOST)
        return row["not_before"] if row else 0

    def _store(self, endpoint: str, key: str, **fields: Any) -> None:
        with self._db_lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO responses(endpoint, params, checked) VALUES (?, ?, 0)", (endpoint, key))
            sets = ", ".join(f"{name} = ?" for name in fields)
            self._db.execute(f"UPDATE responses SET {sets} WHERE endpoint = ? AND params = ?",
                             (*fields.values(), endpoint, key))

    def get(self, endpoint: str, params: dict, *, refresh: bool = True) -> Cached:
        """The cached answer, refreshed first when `refresh` and the minimum interval has passed."""
        key = json.dumps(params, sort_keys=True)
        row = self._row(endpoint, key)
        if refresh:
            with _ONE_AT_A_TIME:
                row = self._row(endpoint, key)
                now = self.clock()
                due = row is None or (now - row["checked"] >= self.min_interval and now >= row["not_before"])
                if due and now >= self.paused_until():
                    self._fetch(endpoint, params, key, row)
                    row = self._row(endpoint, key)
        if row is None:
            return Cached(None, None, True, None)
        body = json.loads(row["body"]) if row["body"] else None
        stale = body is None or row["error"] is not None or self.clock() - (row["fetched"] or 0) > STALE_AFTER
        return Cached(body, row["fetched"], stale, row["error"])

    def _fetch(self, endpoint: str, params: dict, key: str, row: sqlite3.Row | None) -> None:
        url = self.base_url + endpoint + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"User-Agent": user_agent(), "Accept": "application/json", "Accept-Encoding": "gzip"}
        if row is not None and row["etag"] and row["body"]:
            headers["If-None-Match"] = row["etag"]
        error = "no attempt made"
        for attempt in range(self.attempts):
            gap = self.delay - (time.monotonic() - _last_request[0])
            if gap > 0:
                self.sleep(gap)
            _last_request[0] = time.monotonic()
            now = int(self.clock())
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    json.loads(raw)  # refuse to cache something that is not JSON
                    self._store(endpoint, key, etag=response.headers.get("ETag"), body=raw, fetched=now,
                                checked=now, not_before=0, error=None)
                return
            except urllib.error.HTTPError as err:
                if err.code == 304:  # unchanged: keep the body, note that it is still current
                    self._store(endpoint, key, fetched=now, checked=now, not_before=0, error=None)
                    return
                error = f"HTTP {err.code}"
                if err.code != 429 and err.code < 500:
                    break  # will not fix itself; do not hammer
                wait = retry_after(err.headers.get("Retry-After")) or min(self.max_wait, 5 * 2**attempt)
                if wait > self.max_wait or attempt == self.attempts - 1:
                    self._store(endpoint, key, checked=now, not_before=int(now + wait), error=error)
                    self._store(*HOST, checked=now, not_before=int(now + max(wait, HOST_PAUSE)), error=error)
                    return
                self.sleep(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, OSError) as err:
                error = f"no answer: {getattr(err, 'reason', err)}"
                now = int(self.clock())
                # An unreachable host is tried again later, and the other lists wait too.
                self._store(*HOST, checked=now, not_before=now + HOST_PAUSE, error=error)
                break
        self._store(endpoint, key, checked=int(self.clock()), error=error)

    def status(self) -> list[dict]:
        """Every cached answer: what, when it was fetched and asked for, and the last error."""
        with self._db_lock:
            rows = self._db.execute("SELECT endpoint, params, fetched, checked, not_before, error, length(body) AS bytes "
                                    "FROM responses WHERE endpoint <> ? ORDER BY endpoint, params", (HOST[0],)).fetchall()
        return [dict(r) | {"params": json.loads(r["params"])} for r in rows]


# ------------------------------------------------------------------ card rewards
def card_rewards(meta: dict[str, dict]) -> dict[str, dict]:
    """Card id -> its set, for cards whose reward is a unique, gem or base type."""
    return {item_id: e["set"] for item_id, e in meta.items() if (e.get("set") or {}).get("kind") in KIND_TYPES}


def needed_types(meta: dict[str, dict]) -> list[str]:
    """The stash item categories the card rewards in `meta` need, and no others."""
    kinds = {s["kind"] for s in card_rewards(meta).values()}
    return [t for kind in ("unique", "gem", "base") if kind in kinds for t in KIND_TYPES[kind]]


def refresh(client: Ninja, league: str, meta: dict[str, dict]) -> dict:
    """Bring the categories the desk needs up to date (each at most hourly). Returns a summary."""
    types = needed_types(meta)
    if not types:
        return {"league": league, "league_id": None, "types": [], "errors": []}
    league_id = client.league_id(league)
    if league_id is None:
        listed = client.leagues(refresh=False)
        why = (f"could not read poe.ninja's league list: {listed.error}" if listed.body is None or listed.error
               else f"poe.ninja lists no league called {league}")
        return {"league": league, "league_id": None, "types": [], "errors": [why]}
    errors = []
    for kind in types:
        got = client.item_overview(league_id, kind)
        if got.error:
            errors.append(f"{kind}: {got.error}")
            if client.paused_until() > client.clock():
                break  # poe.ninja asked us to wait, or is down: leave the other lists for later
    return {"league": league, "league_id": league_id, "types": types, "errors": errors}


def reward_prices(client: Ninja, league: str, meta: dict[str, dict]) -> dict[str, dict]:
    """Card id -> the price of one unit of its reward in Chaos, from the cache only.

    Each answer carries how many listings back it, when poe.ninja sent it, whether it is stale,
    and a note when the exact variant the card gives had no listing.
    """
    league_id = client.league_id(league, refresh=False)
    if league_id is None:
        return {}
    lists: dict[str, Cached] = {kind: client.item_overview(league_id, kind, refresh=False) for kind in needed_types(meta)}
    out = {}
    for card, reward in card_rewards(meta).items():
        found = []
        for kind in KIND_TYPES[reward["kind"]]:
            got = lists.get(kind)
            if got and got.body:
                found += [(line, got) for line in got.body.get("lines") or []]
        picked = _pick(reward, found)
        if picked:
            out[card] = picked
    return out


def _pick(reward: dict, found: list[tuple[dict, Cached]]) -> dict | None:
    """The listing that best matches what the card gives; the most-listed one when several do."""
    name = reward["reward"]
    # poe.ninja names support gems "Enhance Support" where the card says "Enhance" (sample of 2026-11-21).
    names = {name, f"{name} Support"} if reward["kind"] == "gem" else {name}
    candidates = [(line, got) for line, got in found
                  if (line.get("name") in names or (reward["kind"] == "base" and line.get("baseType") == name))
                  and (line.get("chaosValue") or 0) > 0]
    if not candidates:
        return None
    notes = []

    def narrow(test: Callable[[dict], bool], missing: str) -> None:
        nonlocal candidates
        kept = [c for c in candidates if test(c[0])]
        if kept:
            candidates = kept
        else:
            notes.append(missing)

    corrupted = bool(reward.get("corrupted"))
    if reward["kind"] == "unique":
        # Foil copies cost far more: itemClass 9 in poe.ninja's docs, 10 in their live data.
        narrow(lambda line: line.get("itemClass") not in (9, 10), "only foil listings")
        narrow(lambda line: not line.get("links"), "no unlinked listing")
        if corrupted:  # unique lines carry no corruption flag; the uncorrupted price stands in
            notes.append("priced as uncorrupted (poe.ninja does not list corrupted uniques apart)")
    elif reward["kind"] == "gem":
        # A card that names no level gives a level 1 gem, and one that names no quality gives none.
        level, quality = reward.get("level") or 1, reward.get("quality") or 0
        narrow(lambda line: line.get("gemLevel") == level, f"no listing at level {level}")
        narrow(lambda line: (line.get("gemQuality") or 0) == quality, f"no listing at {quality}% quality")
        narrow(lambda line: bool(line.get("corrupted")) == corrupted,
               "no corrupted listing" if corrupted else "no uncorrupted listing")
    else:
        narrow(lambda line: not line.get("variant"), "no listing without influence")
    if len(candidates) > 1:
        notes.append(f"took the most-listed of {len(candidates)} variants")
    line, got = max(candidates, key=lambda c: c[0].get("listingCount") or c[0].get("count") or 0)
    return {
        "chaos": float(line["chaosValue"]),
        "listings": line.get("listingCount") if line.get("listingCount") is not None else line.get("count"),
        "fetched": got.fetched, "stale": got.stale,
        "variant": "; ".join(notes) or None,
    }
