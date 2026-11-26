"""The local desk: a loopback HTTP server over the analytics, plus a background sync.

Nothing leaves the machine except read-only requests to the public exchange
history, the item-name export and (optionally) item icons. The server binds to
127.0.0.1 only and has no accounts or tokens.
"""

from __future__ import annotations

import collections
import json
import math
import mimetypes
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from vaalstreet import archive, backtest, basket, calibrate, check, db, export, fees, items, ninja, screen, search, searchstring, wealth
from vaalstreet.desk import PATIENCE, Costs, Desk, _num, session_plan
from vaalstreet.ingest import ANCHORS, ingest
from vaalstreet.leagues import LeagueBook
from vaalstreet.ledger import ALERT_FIELDS, ALERT_KINDS, SETTINGS, Ledger, describe_alert, describe_key, recipe_key
from vaalstreet.paths import data_dir, icons_dir
from vaalstreet.rehearsal import PRACTICE_FILE, PracticeLedger, Rehearsal, league_hours

WEB = Path(__file__).parent / "web"
HOUR = 3600
REALM = "poe1"
PASTE_LIMIT = 200_000  # characters in a pasted stash list: thousands of lines, and far under the request limit
READINGS_KEPT = 10  # the screen check's last readings, kept in memory only
SCREEN_STATES = ("waiting", "listening", "reading", "off")  # what the screen check says of itself
SCREEN_QUIET_SECONDS = 90  # it says so every 20 seconds; silent for this long, it is taken to be gone


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class App:
    """Shared state: one DuckDB over the Parquet files, cached desks, the ledger, the sync thread."""

    def __init__(self, root: Path | None = None, *, sync: bool = True, bootstrap_days: int = 14,
                 ninja_base_url: str = ninja.DEFAULT_BASE_URL, fees_url: str = fees.DEFAULT_URL) -> None:
        self.root = root or data_dir()
        self.ninja_base_url = ninja_base_url
        self.fees_url = fees_url
        self._ninja: ninja.Ninja | None = None
        # Kept apart from sync_state: a poe.ninja failure must not hold up the exchange sync.
        self.ninja_state = {"last_run": None, "league_id": None, "types": [], "errors": []}
        self._ledger = Ledger(self.root)
        self.rehearsal: Rehearsal | None = None
        self._rehearsal_alerts: int | None = None  # the rehearsal hour practice alerts were last checked at
        self._thread = threading.local()  # the sync thread marks itself: it always works on the live desk
        self.bootstrap_days = bootstrap_days
        self._lock = threading.RLock()
        self._con = None
        self._desks: dict[str, Desk] = {}
        self._book: LeagueBook | None = None
        self._generation = 0
        self.sync_state = {"running": False, "message": "idle", "last_run": None, "error": None, "backfill": False}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._backfill = threading.Event()
        # What the screen check read on its last presses, so a misread can be seen on the page. In memory
        # only: recognised text is not worth keeping, and it is gone when the desk restarts.
        self._readings: collections.deque[dict] = collections.deque(maxlen=READINGS_KEPT)
        self._reading_id = 0
        self._screen_state: dict | None = None  # what the screen check last said of itself; memory only
        # Its own lock: `_lock` is held for seconds while a league's desk is built, and neither the
        # companion's press nor the page's look every two seconds should wait for that.
        self._screen_lock = threading.Lock()
        if sync:
            threading.Thread(target=self._sync_loop, name="vaal-sync", daemon=True).start()

    # ----------------------------------------------------------------- data
    def _practice(self) -> Rehearsal | None:
        """The running rehearsal, unless this is the sync thread, which only ever sees the live market."""
        return None if getattr(self._thread, "live", False) else self.rehearsal

    @property
    def ledger(self) -> Ledger:
        """The real ledger, or while a rehearsal runs the practice ledger for activity and the real one
        for game facts and your settings (see `rehearsal.PracticeLedger`)."""
        practice = self._practice()
        return PracticeLedger(practice.ledger, self._ledger) if practice else self._ledger

    @ledger.setter
    def ledger(self, value: Ledger) -> None:
        self._ledger = value

    def cursor(self):
        practice = self._practice()
        return practice.cursor() if practice else self._live_cursor()

    def _live_cursor(self):
        with self._lock:
            if self._con is None:
                if not db.has_data(self.root, REALM):
                    raise ApiError("No market data yet - the first sync is still running.", 503)
                self._con = db.connect(self.root, REALM)
            return self._con.cursor()

    def _invalidate(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
            self._con = None
            self._desks.clear()
            self._book = None
            self._generation += 1
            if self.rehearsal:
                self.rehearsal.reset_data()

    def leagues(self) -> list[dict]:
        cur = self.cursor()
        rows = cur.execute(
            """
            SELECT league, kind, family, epoch(first_hour)::BIGINT, epoch(last_hour)::BIGINT, hours, value
            FROM leagues WHERE kind <> 'private' AND value > 0 ORDER BY last_hour DESC, value DESC
            """
        ).fetchall()
        return [dict(zip(("league", "kind", "family", "first_hour", "last_hour", "hours", "value"), r)) for r in rows]

    def default_league(self) -> str:
        """The live challenge league with the most trade; the user's choice wins if it still exists."""
        practice = self._practice()
        if practice:
            return practice.league
        leagues = self.leagues()
        if not leagues:
            raise ApiError("No leagues in the archive yet.", 503)
        chosen = self.ledger.get("league")
        if chosen and any(l["league"] == chosen for l in leagues):
            return chosen
        return self._busiest_live(leagues)

    @staticmethod
    def _busiest_live(leagues: list[dict]) -> str:
        newest = max(l["last_hour"] for l in leagues)
        live = [l for l in leagues if l["last_hour"] >= newest - 6 * HOUR]
        challenge = [l for l in live if l["family"] == "challenge"] or live
        return max(challenge, key=lambda l: l["value"] / max(l["hours"], 1))["league"]

    def live_league(self) -> str | None:
        """The challenge league running now, whatever league you chose to look at and whether or not a
        rehearsal runs. Game facts such as the Gold fee table follow this one."""
        was = getattr(self._thread, "live", False)
        self._thread.live = True  # read the live archive, as the sync thread does
        try:
            leagues = self.leagues()
        finally:
            self._thread.live = was
        return self._busiest_live(leagues) if leagues else None

    def desk(self, league: str | None = None) -> Desk:
        practice = self._practice()
        if practice:
            return practice.desk(league)
        league = league or self.default_league()
        with self._lock:
            if league not in self._desks:
                self._desks[league] = Desk.build(self.cursor(), league, realm=REALM, root=self.root)
            return self._desks[league]

    def book(self) -> LeagueBook:
        practice = self._practice()
        if practice:
            return practice.book()
        with self._lock:
            if self._book is None:
                self._book = LeagueBook(self.cursor(), REALM)
            return self._book

    # ----------------------------------------------------------------- sync
    def _sync_loop(self) -> None:
        self._thread.live = True
        while not self._stop.is_set():
            self.sync_once()
            self.refresh_ninja()  # at most hourly per category; the client enforces it
            self.refresh_fees()  # once per league; `fees.due` decides
            if self._reach_league_start():
                continue  # keep going back, a fortnight at a time, until the live league's first hour is on disk
            self._wake.wait(timeout=300)
            self._wake.clear()

    def _reach_league_start(self) -> bool:
        """Download a further fortnight of history if the live league started before the archive does."""
        try:
            league = self.default_league()
            info = next((l for l in self.book().leagues() if l["league"] == league), None)
        except Exception:
            return False
        if not info or not info["truncated"] or self.sync_state["error"]:
            return False
        earliest = min(archive.have(self.root, REALM))
        todo = archive.missing(self.root, REALM, earliest - 14 * 24 * HOUR, earliest)
        if not todo:
            return False
        self.sync_state.update(running=True, message=f"Reaching back to the start of {league}")
        try:
            archive.archive_hours(self.root, REALM, todo, log=None, stop=self._stop)
            ingest(self.root, log=None)
            self._invalidate()
            self.sync_state["message"] = "Up to date"
        except Exception as err:
            self.sync_state.update(error=str(err), message="Sync failed")
            return False
        finally:
            self.sync_state.update(running=False)
        return not self._stop.is_set()

    def refresh_fees(self) -> None:
        """Fetch the Gold fee table when there is none, or when the live league is a new one. Never raises."""
        # The desk's own clock as well as the file's: if the data directory cannot be written, the
        # failed try is not on disk, and without this every sync round would ask poedb again.
        now = time.time()
        if now < getattr(self, "_fees_wait", 0.0):
            return
        try:
            league = self.live_league()
        except Exception:
            return  # no league yet: the next round of the sync loop tries again
        if not fees.due(self.root, REALM, league, now):
            return
        self._fees_wait = now + fees.RETRY_AFTER
        try:
            fees.refresh(self.root, REALM, league, url=self.fees_url)
        except Exception:
            pass

    def fees_status(self) -> dict:
        """What the desk knows about the Gold fee table, for the status line. Reads the cache only."""
        try:
            league = self.live_league()
        except Exception:
            league = None
        return fees.load(self.root, REALM, league).status()

    def ninja(self) -> ninja.Ninja:
        with self._lock:
            if self._ninja is None:
                self._ninja = ninja.Ninja(self.root, self.ninja_base_url)
            return self._ninja

    def ninja_status(self) -> dict:
        """The footer's poe.ninja line. After a restart the in-memory state is empty until the next
        hourly refresh, so the newest time in the cache stands in; reading the cache sends nothing."""
        out = dict(self.ninja_state, on=self.ninja_on(), credit=ninja.CREDIT, from_cache=False)
        if out["last_run"] is None:
            try:
                newest = max((r["fetched"] for r in self.ninja().status() if r["fetched"]), default=None)
            except Exception:  # a broken cache must not take the status answer down
                newest = None
            if newest is not None:
                out.update(last_run=int(newest), from_cache=True)
        return out

    def ninja_on(self) -> bool:
        if self._practice():
            return False  # poe.ninja knows only today's prices: in a rehearsal they would leak the future
        return bool(self.settings().get("ninja_prices"))

    def refresh_ninja(self) -> None:
        """Ask poe.ninja for the categories the card rewards need. Never raises."""
        if not self.ninja_on():
            return
        try:
            summary = ninja.refresh(self.ninja(), self.default_league(), items.load(self.root, REALM))
            self.ninja_state.update(last_run=int(time.time()), league_id=summary["league_id"],
                                    types=summary["types"], errors=summary["errors"])
        except Exception as err:  # the desk works without these prices
            self.ninja_state.update(last_run=int(time.time()), errors=[str(err)])

    def _outside(self, desk, share: float, costs: Costs | None = None) -> list[dict]:
        """Card sets whose reward is priced by poe.ninja, read from the cache only."""
        if not self.ninja_on():
            return []
        prices = ninja.reward_prices(self.ninja(), desk.league, desk.meta)
        rows = desk.outside_conversions(prices, share=share, costs=costs)
        now = time.time()
        for row in rows:
            fetched = row.get("reward_fetched")
            row["reward_age_minutes"] = int((now - fetched) // 60) if fetched else None
        return rows

    def request_sync(self, backfill: bool = False) -> None:
        if backfill:
            self._backfill.set()
        self._wake.set()

    def sync_once(self) -> None:
        state = self.sync_state
        if state["running"]:
            return
        state.update(running=True, error=None)
        try:
            if not items.is_current(self.root, REALM):
                state["message"] = "Downloading item names"
                items.download(self.root, REALM)
            newest = archive.latest_published()
            backfill = self._backfill.is_set()
            start = archive.FIRST_HOUR[REALM] if backfill else newest - self.bootstrap_days * 24 * HOUR
            # Never leave a hole between what is on disk and now.
            on_disk = archive.have(self.root, REALM)
            if on_disk and not backfill:
                start = min(start, max(on_disk))
            todo = archive.missing(self.root, REALM, start, newest)
            state["backfill"] = backfill
            if todo:
                done = 0
                for chunk_start in range(0, len(todo), 48):
                    if self._stop.is_set():
                        break
                    chunk = todo[chunk_start : chunk_start + 48]
                    state["message"] = f"Downloading market history: {done}/{len(todo)} hours"
                    done += archive.archive_hours(self.root, REALM, chunk, log=None, stop=self._stop)
                    state["message"] = f"Indexing: {done}/{len(todo)} hours"
                    ingest(self.root, log=None)
                    self._invalidate()
                self._after_refresh()
                self._warm()  # here, in the sync thread; a rehearsal step refreshes in the request thread and is not warmed
            elif not db.has_data(self.root, REALM):
                ingest(self.root, log=None)
                self._invalidate()
            if backfill:
                self._backfill.clear()
            state["message"] = "Up to date"
        except Exception as err:  # keep the desk alive; the next cycle retries
            state["error"] = str(err)
            state["message"] = "Sync failed"
            traceback.print_exc()
        finally:
            state.update(running=False, last_run=int(time.time()), backfill=False)

    def _after_refresh(self) -> None:
        try:
            desk = self.desk()
            rows = {row["id"]: row for row in desk.market()}
            self.ledger.evaluate_alerts(desk.league, rows, desk.as_of, self._alert_context(desk))
        except ApiError:
            pass
        try:
            self._auto_snapshots()
        except Exception:  # a snapshot must never take the refresh down, nor mark the sync as failed
            traceback.print_exc()

    def _auto_snapshots(self) -> list[int]:
        """One snapshot of each league you hold something in, at the hour just published; returns the new ids.

        Paused for the whole of a rehearsal, in the sync thread too: practice has no wealth to chart, and
        the pause keeps the real ledger exactly as it was. A league with an auto snapshot at its newest
        hour already (one that ended, say) is skipped before its desk is built.
        """
        if self.rehearsal is not None or not self.settings().get("auto_snapshots"):
            return []
        ledger = self.ledger
        last_hour = {l["league"]: l["last_hour"] for l in self.leagues()}
        taken = []
        for league in ledger.held_leagues():
            if league not in last_hour or ledger.has_auto_snapshot(league, last_hour[league]):
                continue
            try:
                new = wealth.take(ledger, self.desk(league), "auto")
            except (ApiError, LookupError):
                continue
            if new is not None:
                taken.append(new)
        return taken

    def _warm(self) -> None:
        """Compute the slow answers now, after each rebuild in the sync thread, so a page load never waits.

        The flip replays (12 simulator runs) and the ladder's replays at each pick's stake take
        several seconds on a live league; the desk remembers them for its hour. Warmed with the
        pages' default settings, which is what they ask for first.
        """
        for warm in (lambda: self.api_flips({}), lambda: self.api_ladder({})):
            try:
                warm()
            except ApiError:
                return
            except Exception:  # warming is a convenience; never let it take the refresh down
                traceback.print_exc()

    def _alert_context(self, desk) -> dict:
        """What the plan, round-trip, order and precedent alerts look at, computed only for kinds in use.

        Each piece is computed on its own, so one failing model call skips only its own alerts.
        """
        alerts = [a for a in self.ledger.alerts(desk.league) if a["active"]]
        kinds = {a.get("kind") or "price" for a in alerts}
        out: dict = {}

        def attempt(key, fn):
            try:
                out[key] = fn()
            except Exception:  # an alert must never take the refresh down
                traceback.print_exc()

        if "plan" in kinds:
            low = min(a["params"]["min_margin"] for a in alerts if a.get("kind") == "plan") / 100
            # The same calibrated shares as the Flips page, so an alert names the plan you see there.
            attempt("flips", lambda: desk.flips(min_margin=low, costs=self.costs(desk.league),
                                                share_by_item=calibrate.shares(self.fill_calibration(desk))))
        if "trip" in kinds:
            low = min(a["params"]["min_edge"] for a in alerts if a.get("kind") == "trip") / 100
            attempt("loops", lambda: desk.loops(min_edge=low, fixed=self.ledger.fixed_conversions(),
                                                costs=self.costs(desk.league)))
        if "order" in kinds:
            attempt("orders", lambda: self._advise(desk))
        precedents = [a["params"] for a in alerts if a.get("kind") == "precedent"]
        if any(p["mode"] == "standing" for p in precedents):
            attempt("standing", lambda: self.book().standing(desk.league)["items"])
        horizons = {p["horizon"] for p in precedents if p["mode"] == "playbook"}
        if horizons:
            attempt("playbook", lambda: {h: self.book().playbook(desk.league, h)["items"] for h in horizons})
        return out

    def _alerts(self, desk) -> list[dict]:
        alerts = self.ledger.alerts(desk.league)
        names = {a["item"]: desk._name(a["item"]) for a in alerts if a["item"]}
        return [dict(a, description=describe_alert(a, names), name=names.get(a["item"])) for a in alerts]

    def api_alerts(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        return {"alerts": self._alerts(desk), "kinds": ALERT_KINDS, "alert_fields": ALERT_FIELDS}

    def api_events(self, q: dict) -> dict:
        """New events after event id `since`, for notifications; cheap enough to poll every few seconds."""
        league = q.get("league") or self.default_league()
        try:
            since = int(q.get("since") or 0)
        except ValueError as err:
            raise ApiError("since must be an event number.") from err
        events = self.ledger.events_since(league, since)
        return {"events": events, "last": events[-1]["id"] if events else since}

    def api_names(self, q: dict) -> dict:
        """The league's item names, for the screen check, which matches recognised text against them,
        and your corrections (normalised text -> item name), which it uses before its similarity match."""
        desk = self.desk(q.get("league"))
        return {"league": desk.league, "names": [str(n) for n in desk.panel.names],
                "corrections": {c["key"]: c["item"] for c in self.ledger.screen_corrections()}}

    def api_reading(self, body: dict) -> dict:
        """Keep what one press of the screen check (or one copied stack) read, in memory, for the page.

        Everything is cut and rebuilt here rather than stored as sent: the lines are untrusted text, and
        what was understood is rebuilt from its fields, so the page's words are the desk's own.
        """
        try:
            kind = body.get("kind", "screen")
            if kind not in ("screen", "stack"):
                raise ValueError("kind must be screen or stack")
            row: dict = {"kind": kind, "league": _clip(body.get("league"), 80), "error": _clip(body.get("error"), 300),
                         "lines": [], "reading": None, "said": None, "unmatched": None,
                         "recorded": body.get("recorded") is True}
            if kind == "screen":
                lines = body.get("lines") or []
                if not isinstance(lines, list):
                    raise ValueError("lines must be a list")
                row["lines"] = [str(line)[:screen.MAX_CHARS] for line in lines[:screen.MAX_LINES]]
                row["unmatched"] = _clip(body.get("unmatched"), screen.MAX_CHARS)
                got = body.get("reading")
                if got:
                    stock, want, have = got.get("stock"), got.get("want_amount"), got.get("have_amount")
                    if want is None or have is None:
                        want = have = None  # both amounts or neither, as the companion reads them
                    reading = screen.Reading(
                        str(got["item"])[:120], str(got["market"]), str(got["side"]),
                        _positive(got["item_units"]), _positive(got["currency_units"]),
                        _positive(stock) if stock not in (None, "") else None,
                        _whole(want) if want is not None else None, _whole(have) if have is not None else None,
                        _whole(got["gold"]) if got.get("gold") not in (None, "") else None)
                    if reading.market not in ("chaos", "divine") or reading.side not in ("buy", "sell"):
                        raise ValueError("market must be chaos or divine, side buy or sell")
                    row["reading"], row["said"] = vars(reading), reading.describe()
            else:
                item, units = str(body["item"])[:120], _positive(body["units"])
                row["reading"], row["said"] = {"item": item, "units": units}, f"{units:,g} {item}"
        except (KeyError, ValueError, TypeError, AttributeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        with self._lock:
            self._reading_id += 1
            row = {"id": self._reading_id, "t": int(time.time())} | row
            self._readings.append(row)
        return {"ok": True, "id": row["id"]}

    def api_screen_state(self, body: dict) -> dict:
        """Keep what the screen check says of itself, in memory, for the page.

        A press used to show nothing until its reading was done, and a screen check that had turned
        itself off said so only in a console the Windows app does not have. So the companion says
        when it waits for market data, listens, reads, or is off and why, and repeats it every few
        seconds. Only these words, its two keys and one clipped sentence are kept: nothing it read.
        """
        state = body.get("state")
        if state not in SCREEN_STATES:
            raise ApiError(f"Bad request: state must be one of {', '.join(SCREEN_STATES)}.")
        now = int(time.time())
        told = {"state": state, "message": _clip(body.get("message"), 300), "hotkey": _clip(body.get("hotkey"), 60),
                "record_hotkey": _clip(body.get("record_hotkey"), 60), "stacks": body.get("stacks") is True}
        with self._screen_lock:
            old = self._screen_state
            same = old is not None and all(old[k] == v for k, v in told.items())
            # `t` is when it came to this state, `seen` when it last said anything.
            self._screen_state = told | {"t": old["t"] if same else now, "seen": now}
        return {"ok": True}

    def screen_state(self) -> dict | None:
        """The screen check's own state for the page; `quiet` when it has stopped saying anything."""
        with self._screen_lock:
            state = dict(self._screen_state) if self._screen_state else None
        if state:
            state["quiet"] = state["state"] != "off" and time.time() - state["seen"] > SCREEN_QUIET_SECONDS
        return state

    def api_readings(self, q: dict) -> dict:
        """The screen check's last readings, newest first, your corrections, and its own state.

        Each understood screen reading carries `order`: the "Posted this order…" form's prefill
        (`posted_prefill`). With `since` equal to the newest reading's id the answer is only that
        nothing changed, so the page can look often without the desk working out plans each time.
        The state (`screen`, see `api_screen_state`) comes with every answer: it changes without a
        new reading.
        """
        with self._lock:
            rows = [dict(r) for r in reversed(self._readings)]
        last = rows[0]["id"] if rows else 0
        if q.get("since") not in (None, "") and str(q["since"]) == str(last):
            return {"readings": None, "last": last, "screen": self.screen_state()}
        plans: dict[str | None, dict] = {}
        for row in rows:
            row["order"] = None
            if row["kind"] != "screen" or not row["reading"]:
                continue
            try:
                desk = self.desk(row["league"])
                item = check.resolve(desk, row["reading"]["item"])
                if desk.league not in plans:
                    plans[desk.league] = plan_orders(desk.flips())
            except (ApiError, LookupError, ValueError):
                continue  # a league or an item this desk does not know: no prefill, the reading still shows
            reading = row["reading"]
            row["order"] = posted_prefill(reading, item, desk._name(item),
                                          plans[desk.league].get((item, reading["side"], reading["market"])))
        return {"readings": rows, "last": last, "corrections": self.ledger.screen_corrections(),
                "screen": self.screen_state()}

    def api_screen_correction(self, body: dict) -> dict:
        """Save "when the screen check reads this text, it means that item", or delete one by its key."""
        if body.get("delete"):
            self.ledger.delete_screen_correction(str(body["delete"]))
            return self.api_readings({})
        desk = self.desk(body.get("league"))
        text = str(body.get("text") or "").strip()
        key = screen.correction_key(text)
        if len(key) < 3 or key in screen.LABELS:
            raise ApiError("Bad request: give the text as the screen check read it, at least three letters or digits.")
        names = {screen._key(n): str(n) for n in [screen.CHAOS, screen.DIVINE, *desk.panel.names]}
        if key in names:
            raise ApiError(f"Bad request: \"{text}\" already reads as {names[key]}, so there is nothing to correct.")
        item = names.get(screen._key(str(body.get("item") or "")))
        if item is None:
            raise ApiError("Bad request: choose the item from the list of item names.")
        self.ledger.set_screen_correction(key, text, item)
        return self.api_readings({})

    def api_event(self, body: dict) -> dict:
        """Post an event (the screen check's verdict), so the hideout window shows it and can read it aloud."""
        league = body.get("league") or self.default_league()
        try:
            return self.ledger.add_event(str(league), body["title"], body.get("body", ""))
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err

    # ------------------------------------------------------------------ api
    def api_status(self, q: dict) -> dict:
        have = archive.have(self.root, REALM)
        out = {
            "sync": dict(self.sync_state),
            "hours_on_disk": len(have),
            "first_hour": min(have) if have else None,
            "last_hour": max(have) if have else None,
            "archive_start": archive.FIRST_HOUR[REALM],
            "generation": self._generation,
            "ready": db.has_data(self.root, REALM),
            "ninja": self.ninja_status(),
            "fees": self.fees_status(),
        }
        if out["ready"]:
            try:
                out["leagues"] = self.leagues()
                out["league"] = self.default_league()
                events = self.ledger.events(out["league"], 1)
                out["last_event"] = events[0] if events else None
            except ApiError:
                out["ready"] = False
        return out

    def api_desk(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        watch = set(self.ledger.watchlist(desk.league))
        market = desk.market()
        for row in market:
            row["watched"] = row["id"] in watch
        book = self.book()
        info = next((l for l in book.leagues() if l["league"] == desk.league), None)
        # Without the league's first hour on disk, "day N" would be counted from the wrong place.
        day = book.day_of(desk.league) if info and not info["truncated"] else None
        return {
            "summary": desk.summary() | {"league_day": day},
            "market": market,
            "events": self.ledger.events(desk.league),
        }

    def api_item(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        try:
            series = desk.series(q["id"], int(q.get("hours", 0)) or None)
        except KeyError as err:
            raise ApiError(str(err), 404) from err
        plans = [p for p in desk.flips(share=float(q.get("share", 0.1)), min_margin=0.0) if p["id"] == series["id"]]
        sets = [c for c in desk.conversions() if series["id"] in (c["id"], c["reward_id"])]
        return {"series": series, "flips": plans, "conversions": sets,
                "meta": desk.meta.get(series["id"], {}), "alerts": self.ledger.alerts(desk.league),
                "orders": [o for o in self._posted(desk.league) if o["item"] == series["id"]]}

    def _posted(self, league: str) -> list[dict]:
        """The orders you recorded that are still on the exchange.

        The Flips page and an item's panel show them next to the plan they belong to, so that a plan you
        already posted says so and is not recorded a second time.
        """
        keep = ("id", "item", "side", "market", "currency_units", "item_units", "units", "filled")
        return [{k: o[k] for k in keep} for o in self.ledger.orders(league) if o["status"] in ("open", "part-filled")]

    def costs(self, league: str) -> Costs:
        """Your records first, then the fee table. The table is a game fact, so a rehearsal uses it as it is."""
        return self.ledger.costs(league, fees.by_item(self.root, REALM))

    def settings(self) -> dict:
        return self.ledger.settings()

    def api_flips(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        bankroll = float(q["bankroll"]) if q.get("bankroll") else None
        bankroll = bankroll if bankroll and bankroll > 0 else None
        share = float(q.get("share", 0.1))
        plans = desk.flips(share=share, min_margin=float(q.get("min_margin", 0.02)), bankroll=bankroll,
                           costs=self.costs(desk.league),
                           share_by_item=calibrate.shares(self.fill_calibration(desk, share)))
        # Filter by market before any cut. The Flips page re-ranks and searches the whole list itself, so a
        # cut to the most profitable plans would hide plans that rank high by Gold or match a search.
        kind = q.get("kind", "all")
        plans = [p for p in plans if kind == "all" or p["kind"] == kind]
        limit = int(q["limit"]) if q.get("limit") else None
        out = {"summary": desk.summary(), "flips": plans[:limit], "settings": self.settings(),
               "orders": self._posted(desk.league)}
        if bankroll:
            budget = float(q["gold_budget"]) if q.get("gold_budget") else None
            out["session"] = session_plan(plans, bankroll, budget if budget is not None and budget >= 0 else None)
        return out

    def api_arbitrage(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        bankroll = float(q["bankroll"]) if q.get("bankroll") else 0.0
        poor = 0 < bankroll < desk.bridge  # cannot hold a single Divine: stay in the Chaos markets
        costs = self.costs(desk.league)
        share = float(q.get("share", 0.1))
        fixed = self.ledger.fixed_conversions()
        checks = self.ledger.recipe_checks()
        # Three swaps answer in well under a second on a full league; four can take several.
        swaps = max(1, min(int(q.get("swaps", 3)), 3))
        conversions = desk.conversions(share=share, fixed=fixed, costs=costs)
        # Kept apart: the page lists `conversions` as exchange prices, and these rewards are not.
        outside = self._outside(desk, share, costs)
        loops = desk.loops(chaos_only=poor, share=share, swaps=swaps, fixed=fixed, costs=costs)[:80]
        for row in conversions + outside + loops:
            _flag(row, checks)
            if row.get("vendor_keys"):
                row["vendor_checks"] = {k: _status(checks.get(k)) for k in row["vendor_keys"]}
        return {"summary": desk.summary(), "conversions": conversions, "ninja_conversions": outside,
                "ninja_credit": ninja.CREDIT, "loops": loops, "chaos_only": poor,
                "settings": self.settings(), "swaps": swaps, "fixed": self._fixed(desk, fixed, checks)}

    @staticmethod
    def _fixed(desk, fixed: list[dict], checks: dict) -> list[dict]:
        """Your recipes and vendor prices as stored, with names and checks, priced or not in this league."""
        out = []
        for f in fixed:
            row = dict(f, output_name=desk._name(f["output"]),
                       inputs=[{"id": i, "name": desk._name(i), "units": n} for i, n in f["inputs"]])
            row["recipe_key"] = f["key"]
            _flag(row, checks)
            out.append(row)
        return out

    def api_baskets(self, q: dict) -> dict:
        """Your baskets, each priced in the league being viewed: cost now, its changes and its series."""
        desk = self.desk(q.get("league"))
        out = []
        for b in self.ledger.baskets():
            out.append(basket.cost(desk, b["items"]) | {"id": b["id"], "name": b["name"], "note": b["note"],
                                                        "saved": b["t"]})
        return {"league": desk.league, "bridge": _num(desk.bridge), "baskets": out}

    def _read_basket(self, desk, text: str) -> tuple[dict[str, dict], list[dict]]:
        """A pasted list read as basket lines: item id -> {"units", "lines"}, and the lines not read.

        Names are matched as for a pasted stash (`check.read_holdings`). A basket is kept for every
        league, so a line whose item does not trade here is still kept when the item export knows the
        name exactly: it may trade in another league, and the basket names it as having no price here.
        """
        lines, unread = check.read_holdings(desk, text)
        still = []
        for row in unread:
            parsed, _ = check.parse_holdings(row["text"])
            try:
                item = self._item_ref(parsed[0]["name"]) if len(parsed) == 1 else None
            except ApiError:
                item = None
            if item is None:
                still.append(row)
                continue
            entry = lines.setdefault(item, {"units": 0.0, "lines": []})
            entry["units"] += parsed[0]["units"]
            entry["lines"].append(row["line"])
        return lines, still

    def api_basket(self, body: dict) -> dict:
        """Read a basket from a name and a pasted list, and save it unless "preview" is set; or delete one.

        A preview answers with what every line was read as, priced in the league, and the lines that could
        not be read, so the page shows them before anything is saved. Saving reads the list again and keeps
        only the lines read; it is refused when no line could be read.
        """
        if body.get("delete") is not None:
            try:
                self.ledger.delete_basket(int(body["delete"]))
            except (ValueError, TypeError) as err:
                raise ApiError(f"Bad request: {err}") from err
            return {"ok": True}
        desk = self.desk(body.get("league"))
        name, text = str(body.get("name") or "").strip(), body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError("Bad request: paste a list first, one item per line.")
        if len(text) > PASTE_LIMIT:
            raise ApiError("Bad request: that list is too long to read in one go.")
        lines, unread = self._read_basket(desk, text)
        items = [(item, entry["units"]) for item, entry in sorted(lines.items(), key=lambda kv: kv[1]["lines"][0])]
        priced = basket.cost(desk, items) if items else {"items": [], "cost": None, "cheaper": None, "unpriced": []}
        for row, (item, _) in zip(priced["items"], items):
            row["lines"] = lines[item]["lines"]
        answer = {"league": desk.league, "name": name, "lines": priced["items"], "unread": unread,
                  "cost": priced["cost"], "cheaper": priced["cheaper"], "unpriced": priced["unpriced"],
                  "saved": None}
        if body.get("preview"):
            return answer
        if not name:
            raise ApiError("Bad request: give the basket a name.")
        if not items:
            raise ApiError("Bad request: no line of that list could be read.")
        try:
            answer["saved"] = self.ledger.add_basket(name, items, str(body.get("note") or ""))
        except (ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        return answer

    def api_ladder(self, q: dict) -> dict:
        from vaalstreet import ladder

        desk = self.desk(q.get("league"))
        start = float(q["bankroll"]) if q.get("bankroll") else 25.0
        hours = float(q["hours"]) if q.get("hours") else None
        target = float(q["target"]) if q.get("target") else None
        out = ladder.plan(desk, max(start, 0.0), hours=hours, target=target, costs=self.costs(desk.league),
                          share=float(q.get("share", 0.1)), min_margin=float(q.get("min_margin", 0.02)),
                          fixed=self.ledger.fixed_conversions())
        return {"summary": desk.summary(), **out}

    def api_league(self, q: dict) -> dict:
        book = self.book()
        league = q.get("league") or self.default_league()
        unit = q.get("unit", "divine")
        info = next((l for l in book.leagues() if l["league"] == league), None)
        out = {"leagues": book.leagues(), "league": league, "day": book.day_of(league),
               "truncated": bool(info and info["truncated"]),
               "precedents": book.precedents(league), "divine": book.divine_curve(int(q.get("max_day", 90)))}
        out["playbook"] = book.playbook(league, int(q.get("horizon", 7)), unit=unit,
                                        day=int(q["day"]) if q.get("day") else None)
        out["standing"] = book.standing(league, unit=unit)
        out["divine_outlook"] = book.divine_outlook(league)
        out["family"] = book.family_of(league)
        out["divine_timing"] = book.divine_timing(league)
        out["meta"] = book.meta(league)
        out["launch"] = book.launch(league, unit=q.get("launch_unit", "chaos"), limit=int(q.get("launch_rows", 150)))
        return out

    def api_cross_league(self, q: dict) -> dict:
        return self.book().cross_league(q.get("league") or self.default_league(), days=int(q.get("days", 7)))

    def api_curve(self, q: dict) -> dict:
        return {"curve": self.book().curve(q["id"], max_day=int(q.get("max_day", 90)))}

    def api_backtest(self, q: dict, body: dict) -> dict:
        league = body.get("league") or self.default_league()
        # A portfolio is a list of {"strategy": name, "params": {...}}, settled in the order given.
        members = body.get("strategies") or [{"strategy": body.get("strategy", "swing"), "params": body.get("params")}]
        if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
            raise ApiError("strategies must be a list of {strategy, params}.")
        runs = []
        for m in members:
            if m.get("strategy", "swing") not in backtest.STRATEGIES:
                raise ApiError("The desk runs built-in strategies only; use the CLI for your own modules.")
            runs.append((m.get("strategy", "swing"),
                         {k: v for k, v in (m.get("params") or {}).items() if v not in ("", None)}))
        try:
            options = dict(
                capital=float(body.get("capital", 10000)), participation=float(body.get("participation", 0.1)),
                slippage=float(body.get("slippage", 0.03)), start_day=float(body.get("start_day", 0)),
                days=float(body["days"]) if body.get("days") else None,
                attention=int(body.get("attention", 1)), compound=bool(body.get("compound", False)),
                whole_lots=bool(body.get("whole_lots", False)),
                gold_per_unit=self._lab_gold(body),
                gold_budget=float(body["gold_budget"]) if body.get("gold_budget") not in (None, "") else None,
                schedule=body.get("schedule") or None,
                fill_model=str(body.get("fill_model") or "queue"),
            )
            if body.get("strategies"):
                result = backtest.run_portfolio(self.cursor(), league, runs, **options)
            else:
                result = backtest.run(self.cursor(), league, runs[0][0], params=runs[0][1], **options)
        except (TypeError, ValueError, LookupError) as err:
            raise ApiError(str(err)) from err
        step = max(1, len(result.hours) // 600)
        return {
            "summary": result.summary(),
            "t": result.hours[::step].tolist(),
            "equity": [_num(v) for v in result.equity[::step]],
            "divine": [_num(v * result.start_capital) for v in result.divine[::step]],
        }

    def api_strategy_export(self, body: dict) -> dict:
        """The strategy file for a lab run, as `backtest --export` writes it; the browser saves it."""
        from vaalstreet import sharing

        spec = body.get("strategy")
        if spec not in backtest.STRATEGIES:
            raise ApiError("The desk exports built-in strategies only; use the CLI for your own modules.")
        report = body.get("walk_forward")
        if report is not None:
            # A strategy search: the setting it would pick next and its out-of-sample table, as `search --export`.
            try:
                setting = report["next"]["setting"]
                params, _ = search._split(setting, spec)
                options = {**(report.get("options") or {}), **setting}
                return sharing.document(spec, params, options, search.export_rows(report),
                                        extra={"walk_forward": report})
            except (KeyError, TypeError, ValueError) as err:
                raise ApiError(f"That is not a search result: {err}") from err
        params, options, results = body.get("params") or {}, body.get("options") or {}, body.get("results") or []
        if not (isinstance(params, dict) and isinstance(options, dict) and isinstance(results, list)):
            raise ApiError("params and options must be sets of named values, results a list.")
        return sharing.document(spec, params, options, results)

    def _lab_gold(self, body: dict):
        """A backtest's Gold: the fee table when asked for (each order pays for what it asks for), else a flat fee."""
        if not body.get("fee_table"):
            return float(body.get("gold_per_unit") or 0)
        table = fees.by_item(self.root, REALM)
        if not table:
            raise ApiError("The Gold fee table has not been fetched yet. It arrives with the next sync, "
                           "or run `python -m vaalstreet fees refresh`.")
        return backtest.FeeTable(table)

    def api_search(self, body: dict) -> dict:
        """A small walk-forward strategy search. Requests are synchronous, so the grid is capped hard."""
        strategy = body.get("strategy", "swing")
        if strategy not in backtest.STRATEGIES:
            raise ApiError("The desk searches built-in strategies only; use the CLI for your own modules.")
        leagues = body.get("leagues") or None
        if leagues is not None and not (isinstance(leagues, list) and all(isinstance(x, str) for x in leagues)):
            raise ApiError("leagues must be a list of league names.")
        try:
            options = dict(
                capital=float(body.get("capital", 10000)), days=float(body["days"]) if body.get("days") else None,
                gold_per_unit=self._lab_gold(body),
            )
            return search.walk_forward(self.cursor(), strategy, body.get("grid"), leagues=leagues,
                                       how=str(body.get("score") or "divine"), cap=12, max_runs=48, **options)
        except (TypeError, ValueError, LookupError) as err:
            raise ApiError(str(err)) from err

    def fill_calibration(self, desk, default: float = 0.1, settled: bool = False) -> list[dict]:
        """Share of the flow your orders caught, from orders and volume up to the desk's last hour.

        `settled` counts only orders that are filled, cancelled or collected (see `calibrate.from_ledger`).
        """
        rows = calibrate.from_ledger(self.ledger, desk.league, desk.panel, desk.as_of + HOUR, default, settled)
        for row in rows:
            row["name"] = desk._name(row["item"])
        return rows

    def _positions(self, desk) -> tuple[list[dict], dict]:
        """Your positions valued at fair value now, and their totals, as the Ledger page and its export show them."""
        positions = []
        total_value = total_cost = total_realized = total_loot = 0.0
        for item_id, p in self.ledger.positions(desk.league).items():
            mark = desk.fair(item_id)
            value = p["units"] * mark if mark is not None else None
            total_cost += p["cost"]
            total_realized += p["realized"]
            total_loot += p["loot"]
            total_value += value or 0.0
            positions.append({
                "id": item_id, "name": desk._name(item_id), "units": _num(p["units"]),
                "average": _num(p["cost"] / p["units"]) if p["units"] else None, "mark": _num(mark),
                "value": _num(value), "unrealized": _num(value - p["cost"]) if value is not None else None,
                # `loot`: the part of the realised profit that came from units sold with no recorded cost.
                "realized": _num(p["realized"]), "loot": _num(p["loot"]), "last": p["last"],
            })
        positions.sort(key=lambda r: -(r["value"] or 0))
        totals = {"value": _num(total_value), "cost": _num(total_cost), "unrealized": _num(total_value - total_cost),
                  "realized": _num(total_realized), "loot": _num(total_loot)}
        return positions, totals

    def _trades(self, desk) -> tuple[list[dict], list[dict]]:
        """Every trade and every item made, oldest first, with names, the merges recorded for each sale, and
        the units each took beyond what the ledger held (sold or used at no recorded cost)."""
        trades = self.ledger.trades(desk.league)
        uncovered = self.ledger.uncovered(desk.league)
        made = [self._made_row(desk, m) for m in self.ledger.made(desk.league)]
        merges: dict[int, list] = {}
        for m in made:
            if m.get("sale") is not None:
                merges.setdefault(m["sale"], []).append(m)
        for tr in trades:
            tr["name"] = desk._name(tr["item"])
            tr["merges"], tr["uncovered"] = merges.get(tr["id"], []), _num(uncovered.get(("trade", tr["id"])))
        for m in made:
            m["uncovered"] = _num(uncovered.get(("made", m["id"])))
        return trades, made

    def api_ledger(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        share = float(q.get("share", 0.1))  # the share of the flow the page assumes, as Flips takes it
        positions, totals = self._positions(desk)
        trades, made = self._trades(desk)
        fees = self.ledger.gold_fees(desk.league)
        costs = self.costs(desk.league)
        return {
            "league": desk.league, "bridge": _num(desk.bridge), "positions": positions, "trades": trades[::-1][:200],
            "made": made[::-1][:200],
            "totals": totals,
            "watchlist": self.ledger.watchlist(desk.league), "alerts": self._alerts(desk),
            "fill_calibration": self.fill_calibration(desk, share),
            "alert_fields": ALERT_FIELDS, "alert_kinds": ALERT_KINDS,
            # `table` is what the fee table says, and `disputed` whether the desk keeps to the table
            # because too few of your records disagree with it by too much (`Costs.fee`).
            "gold_fees": [{"id": k, "name": desk._name(k), **v, "table": costs.table.get(k),
                           "disputed": "disputed" in costs.fee(k)[1]} for k, v in sorted(fees.items())],
            "orders": self._advise(desk, share), "sessions": self.ledger.sessions(desk.league),
            # For the "Record items you made" form: what each item is made from, by name.
            "made_from": {desk._name(out): [{"inputs": [[desk._name(i), n] for i, n in r["inputs"]], "gives": r["gives"]}
                                            for r in rs] for out, rs in self._merge_recipes(desk, for_sale=False).items()},
            **self._recipes(desk),
        }

    @staticmethod
    def _made_row(desk, m: dict) -> dict:
        return dict(m, inputs=[{"id": item, "name": desk._name(item), "units": units} for item, units in m["inputs"]],
                    name=desk._name(m["output"]))

    def _merge_recipes(self, desk, for_sale: bool = True) -> dict[str, list[dict]]:
        """What each item can be made from, for a sale the ledger's holdings do not cover (`Ledger.add_trade`).

        The game data's recipes (essences and oils three for one, card sets, shards and splinters) and the
        recipes you entered. A recipe you marked as not working in game is left out, and so is any that
        makes or takes Chaos Orbs, which the ledger does not hold. For a sale, nothing makes Divine Orbs
        either: they are money, and selling ones you looted must not turn in a card set you bought to trade.
        """
        chaos = ANCHORS[desk.realm][0]
        money = set(ANCHORS[desk.realm]) if for_sale else {chaos}
        failed = {k for k, c in self.ledger.recipe_checks().items() if c["status"] == "failed"}
        by_name: dict[str, str] = {}
        for item_id, m in desk.meta.items():
            by_name.setdefault(m.get("name"), item_id)
        by_name.update(zip(desk.panel.names, desk.panel.items))  # the league's own market first, as for trades
        out: dict[str, list[dict]] = {}

        def add(output: str, inputs: list, gives: float) -> None:
            recipe = {"inputs": [(i, float(n)) for i, n in inputs], "gives": float(gives)}
            if output not in money and all(i != chaos for i, _ in recipe["inputs"]) and recipe not in out.get(output, []):
                out.setdefault(output, []).append(recipe)

        for item_id, m in desk.meta.items():
            recipe = m.get("set") or {}
            reward = by_name.get(recipe.get("reward"))
            if reward and recipe.get("gives") and recipe_key("set", [item_id, reward]) not in failed:
                add(reward, [(item_id, recipe["need"])], recipe["gives"])
        for f in self.ledger.fixed_conversions():
            if f["kind"] == "recipe" and f["key"] not in failed:
                add(f["output"], f["inputs"], f["gives"])
        return out

    def _sale_answer(self, desk, trade_id: int | None) -> dict:
        """What a new sale took beyond the holdings: the merges recorded for it and the units with no cost."""
        if trade_id is None:
            return {}
        return {"trade": {"id": trade_id,
                          "merges": [self._made_row(desk, m) for m in self.ledger.merges_for(trade_id)],
                          "uncovered": _num(self.ledger.uncovered(desk.league).get(("trade", trade_id)))}}

    def api_check(self, body: dict) -> dict:
        """Go / no-go for an offer typed from the order window. The ratio is items first."""
        desk = self.desk(body.get("league"))
        try:
            market = str(body["market"]).lower()
            if body.get("ratio") is not None:
                item_units, currency_units = check.parse_ratio(str(body["ratio"]), market)
            else:
                item_units, currency_units = float(body["item_units"]), float(body["currency_units"])
            stock = float(body["stock"]) if body.get("stock") not in (None, "") else None
            return check.check(desk, str(body["item"]), market, str(body["side"]), item_units, currency_units, stock)
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err

    def api_ratio(self, q: dict) -> dict:
        """The whole-number ratio to type for a price per item. Needs no league: it is arithmetic."""
        try:
            return check.ratio_helper(q.get("price"), q.get("currency") or "chaos", q.get("max_items"),
                                      q.get("max_currency"))
        except ValueError as err:
            raise ApiError(f"Bad request: {err}") from err

    def api_search_string(self, q: dict) -> dict:
        """Search strings for the game's search box that find the given items and no other item of the league."""
        desk = self.desk(q.get("league"))
        ids = [i for i in (q.get("items") or "").split(",") if i.strip()]
        if not ids:
            raise ApiError("Bad request: name at least one item.")
        try:
            limit = int(q.get("limit") or searchstring.DEFAULT_LIMIT)
            names = [desk.panel.names[desk.panel.index(i.strip())] for i in ids]
        except KeyError as err:
            raise ApiError(f"Bad request: no item {err} trades in {desk.league}.") from err
        except ValueError as err:
            raise ApiError(f"Bad request: the length limit must be a whole number ({err}).") from err
        # Every name the league trades, and the Chaos Orb the prices are counted in, which is not one of them.
        known = set(desk.panel.names) | {desk._name(ANCHORS[desk.realm][0])}
        try:
            return {"league": desk.league, **searchstring.build(names, known, limit)}
        except ValueError as err:
            raise ApiError(f"Bad request: {err}") from err

    def api_stash_paste(self, body: dict) -> dict:
        """A pasted list of what you hold, read into stash lines. Written only with "write": true.

        The answer says what every line was read as and what it would replace, so the page can show it
        before anything changes: `lines` (each with the units the stash holds now, `was`), `unread`
        (lines that could not be read, with the reason) and, with "replace", `dropped` (lines the stash
        holds that the list does not name, which would go). Replacing is refused while a line is unread:
        an item whose line could not be read would silently lose its place in the stash.
        """
        desk = self.desk(body.get("league"))
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError("Bad request: paste a list first, one item per line.")
        if len(text) > PASTE_LIMIT:
            raise ApiError("Bad request: that list is too long to read in one go.")
        holdings, unread = check.read_holdings(desk, text)
        held, replace = self.ledger.stash(desk.league), bool(body.get("replace"))
        lines = [{"id": item, "name": desk._name(item), "units": _num(entry["units"]), "was": _num(held.get(item)),
                  "lines": entry["lines"]} for item, entry in holdings.items()]
        dropped = [{"id": item, "name": desk._name(item), "units": _num(units)}
                   for item, units in held.items() if replace and item not in holdings]
        written = False
        if body.get("write"):
            if replace and unread:
                raise ApiError("Bad request: some lines could not be read. Fix or delete them before replacing the "
                               "whole stash, or the items on those lines would be removed from it.")
            if not lines:
                raise ApiError("Bad request: no line of that list could be read.")
            try:
                self.ledger.set_stash_many(desk.league, {item: entry["units"] for item, entry in holdings.items()},
                                           replace=replace)
            except (ValueError, TypeError) as err:
                raise ApiError(f"Bad request: {err}") from err
            written = True
        return {"league": desk.league, "lines": sorted(lines, key=lambda row: row["lines"][0]), "unread": unread,
                "dropped": dropped, "replace": replace, "written": written}

    def _holdings(self, desk, source: str | None) -> dict[str, float]:
        """What the Stash panel values: the stash you entered, or with source=ledger the ledger's positions."""
        if source == "ledger":
            return {k: p["units"] for k, p in self.ledger.positions(desk.league).items() if p["units"] > 1e-9}
        return self.ledger.stash(desk.league)

    def api_stash(self, q: dict) -> dict:
        """Your stash (or, with source=ledger, the ledger's positions) valued at today's prices."""
        desk = self.desk(q.get("league"))
        out = check.stash_value(desk, self._holdings(desk, q.get("source"))) | {"source": q.get("source") or "stash"}
        if out["source"] == "stash":
            # How long ago each line was last set, by the ledger's own clock: in a rehearsal that is practice time.
            counted, now = self.ledger.stash_counted(desk.league), self.ledger.clock()
            for row in out["rows"]:
                row["counted_ago"] = max(0, int(now - counted[row["id"]])) if row["id"] in counted else None
        return out

    def api_stash_plan(self, q: dict) -> dict:
        """A sell order for every line of the stash (or the ledger's positions), as `check.stash_plan` makes it.

        `reach` is how patient the orders are (one of the flip plans' levels), `min_value` leaves out lines
        worth less in Chaos. Each item sells at the share of the flow your own orders measured there, as on
        the Flips page, and `orders` lists the orders still on the exchange so a posted line says so.
        """
        desk = self.desk(q.get("league"))
        try:
            reach = float(q["reach"]) if q.get("reach") else check.DEFAULT_REACH
            min_value = float(q["min_value"]) if q.get("min_value") else 0.0
            share = float(q.get("share", 0.1))
            if not 0 < share <= 1:
                raise ValueError("share must be above 0 and at most 1.")
            out = check.stash_plan(desk, self._holdings(desk, q.get("source")), reach=reach, min_value=min_value,
                                   share=share, share_by_item=calibrate.shares(self.fill_calibration(desk, share)))
        except ValueError as err:
            raise ApiError(f"Bad request: {err}") from err
        return out | {"source": q.get("source") or "stash", "reaches": list(PATIENCE),
                      "orders": self._posted(desk.league)}

    def export(self, what: str, league: str | None = None) -> tuple[str, str]:
        """One of the Ledger page's tables as CSV text, with the file name to save it under.

        The rows are the ones the page's API answers, so the file agrees with the page; where the page
        shows only the latest 200 trades or items made, the file has them all, and orders that are done
        (cancelled or collected) are in it too. During a rehearsal `self.ledger` is the practice ledger,
        so the file holds practice records, and its name says so.
        """
        if what not in export.KINDS:
            raise ApiError(f"Bad request: what must be one of {', '.join(export.KINDS)}.")
        desk = self.desk(league)
        if what == "trades":
            rows = [[export.iso(t["t"]), t["t"], t["name"], t["item"], t["side"], t["units"], t["price"], t.get("gold"),
                     t["note"], t.get("order_id"), t["uncovered"]] for t in self._trades(desk)[0]]
        elif what == "positions":
            rows = [[p["name"], p["id"], p["units"], p["average"], p["mark"], p["value"], p["unrealized"], p["realized"],
                     p["loot"]] for p in self._positions(desk)[0] if p["units"] > 0 or p["realized"]]  # as the page lists
        elif what == "orders":
            rows = [[export.iso(o["created"]), desk._name(o["item"]), o["item"], o["side"], o["market"].title(),
                     o["item_units"], o["currency_units"], o["units"], o["filled"], o["gold"], o["status"]]
                    for o in self.ledger.orders(desk.league, include_done=True)]
        elif what == "made":
            rows = [[export.iso(m["t"]), " + ".join(f"{x['units']:g} {x['name']}" for x in m["inputs"]),
                     f"{m['gives']:g} {m['name']}", m["note"]] for m in self._trades(desk)[1]]
        else:
            counted = self.ledger.stash_counted(desk.league)
            rows = [[r["name"], r["id"], r["units"], export.iso(counted.get(r["id"])), r.get("value"), r.get("low"),
                     r.get("high"), (r.get("chaos") or {}).get("value"), (r.get("divine") or {}).get("value"),
                     (r.get("best") or "").title()]
                    for r in check.stash_value(desk, self.ledger.stash(desk.league))["rows"]]
        return export.filename(desk.league, what, practice=self._practice() is not None), export.text(what, rows)

    def api_snapshots(self, q: dict) -> dict:
        """Your worth over time for the Wealth panel: the stored snapshots of one picture, read as stored.

        `source` is "stash" (what you entered) or "ledger" (your ledger positions). During a rehearsal
        snapshots pause, and the answer says so instead of listing anything.
        """
        source = q.get("source") or "stash"
        if source not in wealth.SOURCES:
            raise ApiError("Bad request: source must be stash or ledger.")
        auto = bool(self.settings().get("auto_snapshots"))
        if self.rehearsal is not None:
            return {"league": self.default_league(), "source": source, "auto": auto, "paused": True, "rows": []}
        league = q.get("league") or self.default_league()
        return {"league": league, "source": source, "auto": auto, "paused": False,
                "rows": wealth.chart_rows(self.ledger, league, source)}

    def api_snapshot_diff(self, q: dict) -> dict:
        """What changed between snapshots `a` and `b`, item by item, from what they stored (`wealth.compare`)."""
        if self.rehearsal is not None:
            raise ApiError("Snapshots pause during a practice run. Stop the practice to compare them.", 409)
        try:
            out = wealth.compare(self.ledger.snapshot(int(q["a"])), self.ledger.snapshot(int(q["b"])),
                                 q.get("source") or "stash")
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        name = self.desk(out["league"])._name  # for the names only; nothing is valued again
        for key in ("gained", "lost", "no_price"):
            for row in out[key]:
                row["name"] = name(row["id"])
        return out

    def api_snapshot_post(self, league: str, body: dict) -> dict:
        """Take a snapshot now (`note` optional), or delete one (`delete`: its id)."""
        if self.rehearsal is not None:
            raise ApiError("Snapshots pause during a practice run: practice has no wealth of yours to chart. "
                           "Stop the practice to take one.", 409)
        if body.get("delete"):
            self.ledger.delete_snapshot(int(body["delete"]))
            return {}
        new = wealth.take(self.ledger, self.desk(league), "manual", str(body.get("note") or ""))
        if new is None:
            raise ApiError("There is nothing to picture yet: enter what you hold in the Stash panel, "
                           "or record a trade, then take a snapshot.")
        return {"snapshot": new}

    def api_worth(self, q: dict) -> dict:
        """What one holding is worth, for the stack check: valued like a stash line, and not saved."""
        desk = self.desk(q.get("league"))
        try:
            units = float(q.get("units") or 1)
            if not (units > 0 and math.isfinite(units)):
                raise ValueError("units must be more than zero")
            item = check.resolve(desk, str(q["item"]))
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        valued = check.stash_value(desk, {item: units})
        return {"league": valued["league"], "bridge": valued["bridge"]} | valued["rows"][0]

    def api_stash_post(self, body: dict) -> dict:
        """Set a line (units 0 removes it), or clear the stash; answers with the new valuation.

        `was` is the units the line held before, so the stack check can say what a copy replaced.
        """
        desk = self.desk(body.get("league"))
        was = None
        try:
            if body.get("clear"):
                self.ledger.clear_stash(desk.league)
            else:
                item = check.resolve(desk, str(body["item"]))
                was = self.ledger.stash(desk.league).get(item)
                self.ledger.set_stash(desk.league, item, body.get("units", 0))
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        return self.api_stash({"league": desk.league}) | {"was": was}

    def _advise(self, desk, share: float = 0.1) -> list[dict]:
        """Open orders with advice, from today's prices and the flip plan's own order levels.

        A buy order also carries `then_sell`: the sell order of the best plan that buys this item in
        this order's market, with the market it sells in. A flip is two orders, and the second is the
        one that gets forgotten: when the buy fills, the page offers this sell order ready to record.

        An order still on the exchange also carries a proposed fill (`_proposed_fill`), from `share`
        where your own orders have not measured one for its item and market.
        """
        prices = {r["id"]: {"fair": r["price"], "chaos": r["chaos"], "divine": r["divine"]} for r in desk.market()}
        plans: dict[str, dict] = {}
        sell_after: dict[tuple[str, str], dict] = {}
        markets = {"chaos": ("chaos", "chaos"), "divine": ("divine", "divine"), "cross": ("chaos", "divine")}
        for plan in desk.flips():  # best first; every plan's level is kept, since two plans can share a market
            buy_market, sell_market = markets[plan["kind"]]
            sides = plans.setdefault(plan["id"], {})
            sides.setdefault("buy", {}).setdefault(buy_market, []).append(plan["buy_order"])
            sides.setdefault("sell", {}).setdefault(sell_market, []).append(plan["sell_order"])
            sell_after.setdefault((plan["id"], buy_market), plan["sell_order"] | {"market": sell_market, "kind": plan["kind"]})
        orders = self.ledger.advise_orders(desk.league, prices, plans, desk.bridge, self.costs(desk.league))
        now = self.ledger.clock()  # in a rehearsal, practice time
        # Settled orders only: an open order must not lower the share that proposes its own fills.
        learned = calibrate.shares(self.fill_calibration(desk, share, settled=True)) if orders else {}
        for order in orders:
            order["name"] = desk._name(order["item"])
            order["then_sell"] = sell_after.get((order["item"], order["market"])) if order["side"] == "buy" else None
            order.update(self._order_history(order, now))
            order.update(self._proposed_fill(desk, order, learned, share))
        return orders

    @staticmethod
    def _proposed_fill(desk, order: dict, learned: dict, share: float) -> dict:
        """How much more of an open order has probably filled, for the page to propose (`calibrate.likely_filled`).

        The share is measured from the orders you have settled in the order's item and market
        (`fill_share_orders` > 0: filled, cancelled or collected ones), otherwise it is `share`. Only hours published up to the
        desk's last one count, so the estimate is one to two hours behind the game, like every price here.
        """
        if order["status"] not in ("open", "part-filled"):
            return {"proposed_fill": None}
        used, n = (learned.get(order["item"]) or {}).get(order["market"]) or [share, 0]
        guess = calibrate.likely_filled(order, desk.panel, used, desk.as_of + HOUR)
        return {"proposed_fill": _num(guess["more"]), "likely_filled": _num(guess["likely"]),
                "traded_through": _num(guess["through"]), "fill_share": used, "fill_share_orders": n}

    def _order_history(self, order: dict, now: float) -> dict:
        """How long an order has stood, and for a re-posted one what came before it.

        `age` is seconds at the current ratio; `listed` is seconds since the first order of its chain
        was posted (None for an order posted fresh). `earlier` is the ratios of the orders it replaced,
        oldest first. `chain_gold` adds up the Gold paid to post every order of the chain, since Gold is
        not refunded on cancel; `chain_gold_known` is False when some order of it has no Gold recorded,
        so the sum is only a floor.
        """
        chain = self.ledger.order_chain(order["id"])
        everything = chain + [order]
        recorded = [o["gold"] for o in everything if o["gold"] is not None]
        return {
            "age": max(0, int(now - order["created"])),
            "listed": max(0, int(now - chain[0]["created"])) if chain else None,
            "earlier": [{"item_units": o["item_units"], "currency_units": o["currency_units"]} for o in chain],
            "chain_gold": _num(sum(recorded)) if recorded else None,
            "chain_gold_known": len(recorded) == len(everything),
        }

    def _recipes(self, desk) -> dict:
        """The recipes and round trips the desk knows today, and your checks of them (all leagues)."""
        recipes = {}
        fixed = self.ledger.fixed_conversions()
        for f in fixed:  # yours first, so they can be checked even where they do not trade
            left = " + ".join(f"{n:g} {desk._name(i)}" for i, n in f["inputs"])
            verb = "buys" if f["kind"] == "vendor" else "→"
            recipes[f["key"]] = f"{left} {verb} {f['gives']:g} {desk._name(f['output'])}"
        for c in desk.conversions(fixed=fixed):
            if c["how"] != "yours":
                recipes[c["recipe_key"]] = f"{c['need']} {c['name']} → {c['gives']} {c['reward']}"
        for loop in desk.loops(fixed=fixed)[:80]:
            recipes.setdefault(loop["recipe_key"], " → ".join(step["name"] for step in loop["path"]))
        checks = self.ledger.recipe_checks()
        return {"recipes": [{"key": k, "label": v, "check": checks.get(k)} for k, v in recipes.items()],
                # A check outlives its recipe: name it from its key rather than show the key itself.
                "recipe_checks": [dict(c, label=recipes.get(c["key"]) or describe_key(c["key"], desk._name))
                                  for c in checks.values()]}

    def _item_ref(self, text) -> str:
        """An item id from an id or a name (any case) in the item export, which knows items that never traded."""
        text = str(text or "").strip()
        meta = items.load(self.root, REALM)
        if text in meta:
            return text
        found = next((i for i, m in meta.items() if (m.get("name") or "").casefold() == text.casefold()), None)
        if not found:
            raise ApiError(f"No item called {text!r}. Use the name as the exchange shows it.")
        return found

    def _ledger_item(self, desk, text) -> str:
        """The item a ledger entry names, from an id or a name.

        The league's own market comes first, so a made item is the very item the trade form buys and
        sells; then an id the ledger already holds, which may have stopped trading; then the item
        export, which knows items that never traded here.
        """
        text = str(text or "").strip()
        try:
            return check.resolve(desk, text)
        except LookupError:
            pass
        if text in self.ledger.positions(desk.league):
            return text
        return self._item_ref(text)

    def api_post(self, path: str, body: dict) -> dict:
        league = body.get("league") or self.default_league()
        answer: dict = {}
        try:
            if path == "/api/trade":
                if body.get("delete"):
                    self.ledger.delete_trade(body["delete"])
                elif body.get("edit"):
                    fields = ("item", "side", "units", "price", "gold", "note")
                    self.ledger.edit_trade(body["edit"], **{k: body[k] for k in fields if k in body})
                else:
                    desk = self.desk(league)
                    # "merge": false records the sale as it is, with nothing worked out from what the ledger holds.
                    recipes = self._merge_recipes(desk) if body["side"] == "sell" and body.get("merge", True) else None
                    answer = self._sale_answer(desk, self.ledger.add_trade(
                        league, body["item"], body["side"], body["units"], body["price"], body.get("note", ""),
                        gold=body.get("gold"), recipes=recipes))
            elif path == "/api/made":
                if body.get("delete"):
                    self.ledger.delete_made(body["delete"])
                else:
                    desk = self.desk(league)
                    inputs = [[self._ledger_item(desk, item), units] for item, units in body["inputs"]]
                    output = self._ledger_item(desk, body["output"])
                    # The ledger counts in Chaos and holds none: as an input it could never be "held", and as
                    # an output it would sit there as an item whose profit is never realised.
                    chaos, why = ANCHORS[desk.realm][0], "The ledger counts in Chaos Orbs and does not hold them, so "
                    if output == chaos:
                        raise ApiError(why + "they cannot be the item you got. Getting Chaos for items is a sale: "
                                       "record it as a trade, Sold, with the Chaos you got for each item.")
                    if any(item == chaos for item, _ in inputs):
                        if len(inputs) == 1:
                            raise ApiError(why + "they cannot be an item you used. Paying Chaos for an item is a buy: "
                                           "record it as a trade, Bought, with the Chaos you paid for each item.")
                        raise ApiError(why + "they cannot be an item you used. Record the entry without them; "
                                       "the Chaos you paid on top is then missing from the cost of the item you made.")
                    self.ledger.add_made(league, inputs, output, body.get("gives", 1),
                                         body.get("note", ""), name=desk._name)
            elif path == "/api/order":
                if body.get("delete"):
                    self.ledger.delete_order(body["delete"])
                elif body.get("fill"):
                    desk, order = self.desk(league), self.ledger.order(body["fill"])
                    bridge = desk.bridge if order["market"] == "divine" else None
                    # As for a typed sale: "merge": false sells what the ledger lacks as looted, at no cost.
                    recipes = self._merge_recipes(desk) if order["side"] == "sell" and body.get("merge", True) else None
                    answer = self._sale_answer(desk, self.ledger.fill_order(
                        body["fill"], body["units"], bridge=bridge, record_trade=bool(body.get("record_trade", True)),
                        recipes=recipes))
                elif body.get("status"):
                    self.ledger.set_order_status(body["id"], body["status"])
                elif body.get("repost"):
                    # Cancelled in game and posted again: the old order's item, side and market carry over.
                    self.ledger.repost_order(body["repost"], body["currency_units"], body["item_units"], body["units"],
                                             gold=body.get("gold"))
                else:
                    # The page sends the item's id; the screen check's record key sends its name.
                    self.ledger.add_order(league, self._ledger_item(self.desk(league), body["item"]), body["side"],
                                          body["market"], body["currency_units"],
                                          body["item_units"], body["units"], gold=body.get("gold"))
            elif path == "/api/session":
                if body.get("delete"):
                    self.ledger.delete_session(body["delete"])
                elif body.get("stop"):
                    self.ledger.stop_session(body["stop"], gold_earned=body.get("gold_earned"))
                else:
                    self.ledger.start_session(league, body.get("expected_per_hour"), body.get("plan"))
            elif path == "/api/recipe_check":
                if body.get("delete"):
                    self.ledger.delete_recipe_check(body["delete"])
                else:
                    self.ledger.check_recipe(body["key"], body["status"], body.get("note", ""))
            elif path == "/api/recipe":
                if body.get("delete"):
                    self.ledger.delete_fixed(body["delete"])
                else:
                    # The desk's form sends names, as the CLI takes them; ids still work.
                    inputs = [[self._item_ref(item), units] for item, units in body["inputs"]]
                    self.ledger.add_fixed(body["kind"], inputs, self._item_ref(body["output"]), body.get("gives", 1),
                                          body.get("note", ""))
            elif path == "/api/snapshot":
                answer = self.api_snapshot_post(league, body)
            elif path == "/api/watch":
                self.ledger.watch(league, body["item"], bool(body.get("on", True)))
            elif path == "/api/alert":
                if body.get("delete"):
                    self.ledger.delete_alert(body["delete"])
                else:
                    kind = body.get("kind", "price")
                    if kind == "price":
                        if not all(body.get(k) not in (None, "") for k in ("item", "field", "op", "threshold")):
                            raise ApiError("A price alert needs an item, a field, a direction and a threshold.")
                        self.ledger.add_alert(league, body["item"], body["field"], body["op"], body["threshold"])
                    else:
                        params = body.get("params") or {}
                        if not isinstance(params, dict):
                            raise ApiError("The alert's rule must be a set of named values.")
                        self.ledger.add_alert(league, kind=kind, params=params)
            elif path == "/api/settings":
                for key, value in body.items():
                    if key not in SETTINGS:
                        raise ValueError(f"unknown setting {key}")
                    if value in (None, ""):
                        self.ledger.set(key, SETTINGS[key])  # empty restores the default (no assumed fee)
                        continue
                    value = float(value)
                    if not (math.isfinite(value) and value >= 0):
                        raise ValueError(f"{key} must be a number of zero or more")
                    self.ledger.set(key, value)
            elif path == "/api/league":
                self.ledger.set("league", body["league"])
            elif path == "/api/sync":
                self.request_sync(backfill=bool(body.get("backfill")))
            else:
                raise ApiError("Unknown endpoint.", 404)
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        return {"ok": True, **answer}

    # ------------------------------------------------------------- rehearsal
    def rehearsal_banner(self) -> dict | None:
        """What every answer carries during a rehearsal; also checks practice alerts once per new hour."""
        practice = self.rehearsal
        if not practice:
            return None
        banner = practice.banner()
        if self._rehearsal_alerts != banner["hour"]:
            self._rehearsal_alerts = banner["hour"]
            try:
                self._after_refresh()  # the request thread: desk and ledger are the practice ones
            except Exception:  # an alert must never take an answer down
                traceback.print_exc()
        return banner

    def api_rehearsal(self, q: dict) -> dict:
        practice = self.rehearsal
        if not practice:
            return {"running": False}
        return {"running": True, **practice.banner(), "bankroll": practice.bankroll, "result": practice.result()}

    def api_rehearsal_post(self, path: str, body: dict) -> dict:
        try:
            if path == "/api/rehearsal/step":
                if not self.rehearsal:
                    raise ApiError("No rehearsal is running.", 409)
                if body.get("speed") is not None:
                    self.rehearsal.set_speed(float(body["speed"]))
                else:
                    self.rehearsal.step(int(body.get("hours", 1)))
                return self.api_rehearsal({})
            if body.get("stop"):
                return self.stop_rehearsal()
            return self.start_rehearsal(body)
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err

    def start_rehearsal(self, body: dict) -> dict:
        """Start practice on a past league; any running rehearsal ends first (its result is returned too)."""
        league = str(body["league"])
        first, last = league_hours(self._live_cursor(), league)  # checked before a running session is ended
        if body.get("start") not in (None, ""):
            start = int(body["start"])
        else:
            start = first + int(float(body.get("start_day") or 0) * 24) * HOUR
        speed, bankroll = float(body.get("speed") or 0), body.get("bankroll")
        bankroll = float(bankroll) if bankroll not in (None, "") else 1000.0
        if not first <= start <= last:
            raise ApiError(f"{league} has {(last - first) // HOUR // 24 + 1} days on disk; start inside them.")
        if not (math.isfinite(speed) and speed >= 0 and math.isfinite(bankroll) and bankroll > 0):
            raise ApiError("speed must be zero or more game hours per real minute, and bankroll a positive number.")
        ended = self.stop_rehearsal()["result"] if self.rehearsal else None
        practice = Rehearsal(self.root, league, first, last, start, realm=REALM, speed=speed, bankroll=bankroll,
                             ledger_path=self._ledger.path.with_name(PRACTICE_FILE))
        with self._lock:
            self.rehearsal, self._rehearsal_alerts = practice, None
        return self.api_rehearsal({}) | {"previous": ended}

    def stop_rehearsal(self) -> dict:
        """End the rehearsal and report its result; the practice ledger stays on disk until the next one."""
        with self._lock:
            practice, self.rehearsal = self.rehearsal, None
        if not practice:
            return {"running": False, "result": None}
        result = practice.result()
        practice.close()
        return {"running": False, "result": result}

    # ---------------------------------------------------------------- icons
    def icon(self, item_id: str) -> bytes | None:
        art = (items.load(self.root, REALM).get(item_id) or {}).get("art")
        if not art:
            return None
        path = icons_dir(self.root) / (art.replace("/", "_") + ".png")
        if path.exists():
            return path.read_bytes()
        try:
            request = urllib.request.Request(items.ICON_URL.format(art=urllib.parse.quote(art)),
                                             headers={"User-Agent": archive.user_agent()})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read()
        except Exception:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data


def plan_orders(plans: list[dict]) -> dict[tuple[str, str, str], dict]:
    """(item id, side, market) -> the order the best flip plan posts there. Plans come best first, and each
    plan buys in one market and sells in the other (or the same), as `App._advise` reads them."""
    markets = {"chaos": ("chaos", "chaos"), "divine": ("divine", "divine"), "cross": ("chaos", "divine")}
    out: dict[tuple[str, str, str], dict] = {}
    for plan in plans:
        buy_market, sell_market = markets[plan["kind"]]
        out.setdefault((plan["id"], "buy", buy_market), plan["buy_order"])
        out.setdefault((plan["id"], "sell", sell_market), plan["sell_order"])
    return out


def posted_prefill(reading: dict, item: str, name: str, plan_order: dict | None) -> dict:
    """What the "Posted this order…" form opens with for a screen reading.

    The ratio and the count come from, in this order: the amounts you typed into the window, if the
    screen check read both (reduced to lowest terms; the count is the item amount); else the flip plan's
    order for this item, side and market; else the market ratio read off the window, which is the
    market's suggestion, not your order. In the last two cases the ratio can be changed in the form, the
    count is left empty, and the lead sentence says where the numbers came from.
    """
    side, market = reading["side"], reading["market"]
    currency = "Divine" if market == "divine" else "Chaos"
    base = {"item": item, "name": name, "side": side, "market": market, "currency": currency,
            "title": f"{'Buy' if side == 'buy' else 'Sell'} order you posted"}
    if reading.get("gold") is not None:
        base["gold"] = reading["gold"]  # the fee the window showed; the form's Gold field starts from it
    want, have = reading.get("want_amount"), reading.get("have_amount")
    if want and have:
        items, money = (want, have) if side == "buy" else (have, want)
        common = math.gcd(int(items), int(money))
        return base | {"source": "amounts", "item_units": int(items) // common, "currency_units": int(money) // common,
                       "units": int(items),
                       "lead": f"Read from the amounts in the exchange window: {int(items):,} {name} for "
                               f"{int(money):,} {currency}. Check them against what you posted."}
    change = "Change the ratio and the count to what you posted."
    if plan_order:
        return base | {"source": "plan", "editable": True, "item_units": plan_order["item_units"],
                       "currency_units": plan_order["currency_units"],
                       "lead": f"The ratio is the flip plan's for this item and market; the screen check did not "
                               f"read your own amounts. {change}"}
    # A ratio with a part of a unit in it cannot be posted: those fields are left empty to type in.
    items, money = float(reading["item_units"]), float(reading["currency_units"])
    return base | {"source": "market", "editable": True, "item_units": int(items) if items.is_integer() else None,
                   "currency_units": int(money) if money.is_integer() else None,
                   "lead": f"The ratio is the market ratio the screen check read, the market's suggestion, not "
                           f"your order; it did not read your own amounts. {change}"}


def _whole(value) -> int:
    number = _positive(value)
    if number != int(number):
        raise ValueError("amounts must be whole numbers")
    return int(number)


def _clip(value, limit: int) -> str | None:
    return str(value)[:limit] if value not in (None, "") else None


def _positive(value) -> float:
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise ValueError("amounts must be more than zero")
    return number


def _status(found: dict | None) -> str:
    return found["status"] if found else "unchecked"


def _flag(row: dict, checks: dict) -> None:
    """Add the latest in-game check of a row's recipe: confirmed, failed or unchecked."""
    found = checks.get(row["recipe_key"])
    row["check"] = _status(found)
    row["check_note"] = found["note"] if found else ""


GET_ROUTES = {
    "/api/status": "api_status", "/api/desk": "api_desk", "/api/item": "api_item", "/api/flips": "api_flips",
    "/api/arbitrage": "api_arbitrage", "/api/baskets": "api_baskets", "/api/ladder": "api_ladder", "/api/league": "api_league", "/api/curve": "api_curve",
    "/api/cross_league": "api_cross_league",
    "/api/ledger": "api_ledger", "/api/stash": "api_stash", "/api/stash_plan": "api_stash_plan", "/api/worth": "api_worth",
    "/api/snapshots": "api_snapshots", "/api/snapshot_diff": "api_snapshot_diff",
    "/api/alerts": "api_alerts", "/api/events": "api_events", "/api/names": "api_names",
    "/api/readings": "api_readings", "/api/rehearsal": "api_rehearsal", "/api/ratio": "api_ratio",
    "/api/search_string": "api_search_string",
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VaalStreet/2"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store",
                  headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200, banner: bool = True) -> None:
            if banner and isinstance(payload, dict) and app.rehearsal and "rehearsal" not in payload:
                # Every answer says it is practice, so the page can show a banner and never pass for live.
                payload = payload | {"rehearsal": app.rehearsal_banner()}
            self._send(status, json.dumps(payload, separators=(",", ":"), default=_default).encode(), "application/json")

        def _local(self) -> bool:
            """Refuse cross-site requests: a web page must not be able to drive the desk."""
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                return False
            if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
            return True

        def _guarded(self, fn) -> None:
            if not self._local():
                self._json({"error": "Local requests only."}, 403)
                return
            try:
                fn()
            except ApiError as err:
                self._json({"error": str(err)}, err.status)
            except LookupError as err:
                self._json({"error": str(err)}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as err:
                traceback.print_exc()
                self._json({"error": f"{type(err).__name__}: {err}"}, 500)

        def do_GET(self) -> None:
            self._guarded(self._get)

        def do_POST(self) -> None:
            self._guarded(self._post)

        def do_DELETE(self) -> None:
            self._guarded(self._delete)

        def _delete(self) -> None:
            if urllib.parse.urlparse(self.path).path != "/api/rehearsal":
                raise ApiError("Unknown endpoint.", 404)
            self._json(app.stop_rehearsal())

        def _get(self) -> None:
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path in GET_ROUTES:
                self._json(getattr(app, GET_ROUTES[url.path])(q))
            elif url.path == "/api/export":
                # A file to save, not a page: the byte order mark lets Excel read the item names right.
                name, text = app.export(q.get("what", ""), q.get("league"))
                self._send(200, (export.BOM + text).encode("utf-8"), "text/csv; charset=utf-8",
                           headers={"Content-Disposition": f'attachment; filename="{name}"'})
            elif url.path == "/icon":
                data = app.icon(q.get("id", ""))
                if data:
                    self._send(200, data, "image/png", "max-age=604800")
                else:
                    self._send(404, b"", "text/plain")
            else:
                name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
                file = (WEB / name).resolve()
                if WEB.resolve() not in file.parents or not file.is_file():
                    self._send(404, b"Not found", "text/plain")
                    return
                ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                self._send(200, file.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") or "javascript" in ctype else ""))

        def _post(self) -> None:
            url = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ApiError("Request too large.", 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as err:
                raise ApiError("Body must be JSON.") from err
            if "json" not in (self.headers.get("Content-Type") or ""):
                raise ApiError("Content-Type must be application/json.", 415)
            if url.path == "/api/backtest":
                self._json(app.api_backtest({}, body))
            elif url.path == "/api/strategy_export":
                self._json(app.api_strategy_export(body), banner=False)  # a file to share, not a page
            elif url.path == "/api/search":
                self._json(app.api_search(body))
            elif url.path == "/api/check":
                self._json(app.api_check(body))
            elif url.path == "/api/stash/paste":
                self._json(app.api_stash_paste(body))
            elif url.path == "/api/basket":
                self._json(app.api_basket(body))
            elif url.path == "/api/event":
                self._json(app.api_event(body))
            elif url.path == "/api/reading":
                self._json(app.api_reading(body))
            elif url.path == "/api/screen_state":
                self._json(app.api_screen_state(body))
            elif url.path == "/api/screen_correction":
                self._json(app.api_screen_correction(body))
            elif url.path == "/api/stash":
                self._json(app.api_stash_post(body))
            elif url.path in ("/api/rehearsal", "/api/rehearsal/step"):
                self._json(app.api_rehearsal_post(url.path, body))
            else:
                self._json(app.api_post(url.path, body))

    return Handler


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return _num(o)
    raise TypeError(type(o).__name__)


def _listen(port: int, root: Path | None, sync: bool) -> tuple[ThreadingHTTPServer, App]:
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), None)
    except OSError as err:
        raise RuntimeError(f"Cannot listen on port {port} ({err.strerror}). Is the desk already running? "
                           f"Try --port {port + 1}.") from err
    app = App(root, sync=sync)
    httpd.RequestHandlerClass = make_handler(app)
    return httpd, app


class Running:
    """A desk that answers on a background thread, for a program that has other work on its main thread."""

    def __init__(self, httpd: ThreadingHTTPServer, app: App) -> None:
        self.httpd, self.app = httpd, app
        self.port = httpd.server_port
        # 127.0.0.1 and not localhost: on Windows localhost can resolve to ::1 first, where nothing listens.
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=httpd.serve_forever, name="vaal-desk", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.app._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


def start(port: int = 3000, root: Path | None = None, *, sync: bool = True) -> Running:
    """Start the desk without blocking. Port 0 takes any free port; read it from the result."""
    return Running(*_listen(port, root, sync))


def serve(port: int = 3000, root: Path | None = None, *, sync: bool = True) -> None:
    httpd, app = _listen(port, root, sync)
    print(f"Vaal Street desk: http://localhost:{httpd.server_port}   (data in {app.root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app._stop.set()
        httpd.server_close()
