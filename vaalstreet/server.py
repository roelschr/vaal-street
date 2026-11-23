"""The local desk: a loopback HTTP server over the analytics, plus a background sync.

Nothing leaves the machine except read-only requests to the public exchange
history, the item-name export and (optionally) item icons. The server binds to
127.0.0.1 only and has no accounts or tokens.
"""

from __future__ import annotations

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

from vaalstreet import archive, backtest, calibrate, check, db, items, ninja, search
from vaalstreet.desk import Costs, Desk, _num, session_plan
from vaalstreet.ingest import ANCHORS, ingest
from vaalstreet.leagues import LeagueBook
from vaalstreet.ledger import ALERT_FIELDS, ALERT_KINDS, SETTINGS, Ledger, describe_alert, describe_key
from vaalstreet.paths import data_dir, icons_dir
from vaalstreet.rehearsal import PRACTICE_FILE, PracticeLedger, Rehearsal, league_hours

WEB = Path(__file__).parent / "web"
HOUR = 3600
REALM = "poe1"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class App:
    """Shared state: one DuckDB over the Parquet files, cached desks, the ledger, the sync thread."""

    def __init__(self, root: Path | None = None, *, sync: bool = True, bootstrap_days: int = 14,
                 ninja_base_url: str = ninja.DEFAULT_BASE_URL) -> None:
        self.root = root or data_dir()
        self.ninja_base_url = ninja_base_url
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
        newest = max(l["last_hour"] for l in leagues)
        live = [l for l in leagues if l["last_hour"] >= newest - 6 * HOUR]
        challenge = [l for l in live if l["family"] == "challenge"] or live
        return max(challenge, key=lambda l: l["value"] / max(l["hours"], 1))["league"]

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
        """The league's item names, for the screen check, which matches recognised text against them."""
        desk = self.desk(q.get("league"))
        return {"league": desk.league, "names": [str(n) for n in desk.panel.names]}

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
                "meta": desk.meta.get(series["id"], {}), "alerts": self.ledger.alerts(desk.league)}

    def costs(self, league: str) -> Costs:
        return self.ledger.costs(league)

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
        out = {"summary": desk.summary(), "flips": plans[:limit], "settings": self.settings()}
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
                gold_per_unit=float(body.get("gold_per_unit") or 0),
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
                gold_per_unit=float(body.get("gold_per_unit") or 0),
            )
            return search.walk_forward(self.cursor(), strategy, body.get("grid"), leagues=leagues,
                                       how=str(body.get("score") or "divine"), cap=12, max_runs=48, **options)
        except (TypeError, ValueError, LookupError) as err:
            raise ApiError(str(err)) from err

    def fill_calibration(self, desk, default: float = 0.1) -> list[dict]:
        """Share of the flow your orders caught, from orders and volume up to the desk's last hour."""
        rows = calibrate.from_ledger(self.ledger, desk.league, desk.panel, desk.as_of + HOUR, default)
        for row in rows:
            row["name"] = desk._name(row["item"])
        return rows

    def api_ledger(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        positions = []
        total_value = total_cost = total_realized = 0.0
        for item_id, p in self.ledger.positions(desk.league).items():
            mark = desk.fair(item_id)
            value = p["units"] * mark if mark is not None else None
            total_cost += p["cost"]
            total_realized += p["realized"]
            total_value += value or 0.0
            positions.append({
                "id": item_id, "name": desk._name(item_id), "units": _num(p["units"]),
                "average": _num(p["cost"] / p["units"]) if p["units"] else None, "mark": _num(mark),
                "value": _num(value), "unrealized": _num(value - p["cost"]) if value is not None else None,
                "realized": _num(p["realized"]), "last": p["last"],
            })
        positions.sort(key=lambda r: -(r["value"] or 0))
        trades = self.ledger.trades(desk.league)
        for tr in trades:
            tr["name"] = desk._name(tr["item"])
        made = [dict(m, inputs=[{"id": item, "name": desk._name(item), "units": units} for item, units in m["inputs"]],
                     name=desk._name(m["output"])) for m in self.ledger.made(desk.league)]
        fees = self.ledger.gold_fees(desk.league)
        return {
            "league": desk.league, "bridge": _num(desk.bridge), "positions": positions, "trades": trades[::-1][:200],
            "made": made[::-1][:200],
            "totals": {"value": _num(total_value), "cost": _num(total_cost),
                       "unrealized": _num(total_value - total_cost), "realized": _num(total_realized)},
            "watchlist": self.ledger.watchlist(desk.league), "alerts": self._alerts(desk),
            "fill_calibration": self.fill_calibration(desk),
            "alert_fields": ALERT_FIELDS, "alert_kinds": ALERT_KINDS,
            "gold_fees": [{"id": k, "name": desk._name(k), **v} for k, v in sorted(fees.items())],
            "orders": self._advise(desk), "sessions": self.ledger.sessions(desk.league),
            **self._recipes(desk),
        }

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

    def api_stash(self, q: dict) -> dict:
        """Your stash (or, with source=ledger, the ledger's positions) valued at today's prices."""
        desk = self.desk(q.get("league"))
        if q.get("source") == "ledger":
            holdings = {k: p["units"] for k, p in self.ledger.positions(desk.league).items() if p["units"] > 1e-9}
        else:
            holdings = self.ledger.stash(desk.league)
        return check.stash_value(desk, holdings) | {"source": q.get("source") or "stash"}

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
        """Set a line (units 0 removes it), or clear the stash; answers with the new valuation."""
        desk = self.desk(body.get("league"))
        try:
            if body.get("clear"):
                self.ledger.clear_stash(desk.league)
            else:
                self.ledger.set_stash(desk.league, check.resolve(desk, str(body["item"])), body.get("units", 0))
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        return self.api_stash({"league": desk.league})

    def _advise(self, desk) -> list[dict]:
        """Open orders with advice, from today's prices and the flip plan's own order levels."""
        prices = {r["id"]: {"fair": r["price"], "chaos": r["chaos"], "divine": r["divine"]} for r in desk.market()}
        plans: dict[str, dict] = {}
        markets = {"chaos": ("chaos", "chaos"), "divine": ("divine", "divine"), "cross": ("chaos", "divine")}
        for plan in desk.flips():  # best first, so the first plan for a market wins
            buy_market, sell_market = markets[plan["kind"]]
            sides = plans.setdefault(plan["id"], {})
            sides.setdefault("buy", {}).setdefault(buy_market, plan["buy_order"])
            sides.setdefault("sell", {}).setdefault(sell_market, plan["sell_order"])
        orders = self.ledger.advise_orders(desk.league, prices, plans, desk.bridge)
        for order in orders:
            order["name"] = desk._name(order["item"])
        return orders

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
        try:
            if path == "/api/trade":
                if body.get("delete"):
                    self.ledger.delete_trade(body["delete"])
                elif body.get("edit"):
                    fields = ("item", "side", "units", "price", "gold", "note")
                    self.ledger.edit_trade(body["edit"], **{k: body[k] for k in fields if k in body})
                else:
                    self.ledger.add_trade(league, body["item"], body["side"], body["units"], body["price"],
                                          body.get("note", ""), gold=body.get("gold"))
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
                    bridge = self.desk(league).bridge if self.ledger.order(body["fill"])["market"] == "divine" else None
                    self.ledger.fill_order(body["fill"], body["units"], bridge=bridge,
                                           record_trade=bool(body.get("record_trade", True)))
                elif body.get("status"):
                    self.ledger.set_order_status(body["id"], body["status"])
                else:
                    self.ledger.add_order(league, body["item"], body["side"], body["market"], body["currency_units"],
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
        return {"ok": True}

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


def _status(found: dict | None) -> str:
    return found["status"] if found else "unchecked"


def _flag(row: dict, checks: dict) -> None:
    """Add the latest in-game check of a row's recipe: confirmed, failed or unchecked."""
    found = checks.get(row["recipe_key"])
    row["check"] = _status(found)
    row["check_note"] = found["note"] if found else ""


GET_ROUTES = {
    "/api/status": "api_status", "/api/desk": "api_desk", "/api/item": "api_item", "/api/flips": "api_flips",
    "/api/arbitrage": "api_arbitrage", "/api/ladder": "api_ladder", "/api/league": "api_league", "/api/curve": "api_curve",
    "/api/cross_league": "api_cross_league",
    "/api/ledger": "api_ledger", "/api/stash": "api_stash", "/api/worth": "api_worth",
    "/api/alerts": "api_alerts", "/api/events": "api_events", "/api/names": "api_names",
    "/api/rehearsal": "api_rehearsal",
}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VaalStreet/2"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
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
            elif url.path == "/api/event":
                self._json(app.api_event(body))
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


def serve(port: int = 3000, root: Path | None = None, *, sync: bool = True) -> None:
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), None)
    except OSError as err:
        raise RuntimeError(f"Cannot listen on port {port} ({err.strerror}). Is the desk already running? "
                           f"Try --port {port + 1}.") from err
    app = App(root, sync=sync)
    httpd.RequestHandlerClass = make_handler(app)
    print(f"Vaal Street desk: http://localhost:{port}   (data in {app.root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app._stop.set()
        httpd.server_close()
