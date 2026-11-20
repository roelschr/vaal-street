"""The local desk: a loopback HTTP server over the analytics, plus a background sync.

Nothing leaves the machine except read-only requests to the public exchange
history, the item-name export and (optionally) item icons. The server binds to
127.0.0.1 only and has no accounts or tokens.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from vaalstreet import archive, backtest, db, items
from vaalstreet.desk import Desk, _num
from vaalstreet.ingest import ingest
from vaalstreet.leagues import LeagueBook
from vaalstreet.ledger import ALERT_FIELDS, Ledger
from vaalstreet.paths import data_dir, icons_dir

WEB = Path(__file__).parent / "web"
HOUR = 3600
REALM = "poe1"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class App:
    """Shared state: one DuckDB over the Parquet files, cached desks, the ledger, the sync thread."""

    def __init__(self, root: Path | None = None, *, sync: bool = True, bootstrap_days: int = 14) -> None:
        self.root = root or data_dir()
        self.ledger = Ledger(self.root)
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
    def cursor(self):
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

    def leagues(self) -> list[dict]:
        cur = self.cursor()
        rows = cur.execute(
            """
            SELECT league, kind, epoch(first_hour)::BIGINT, epoch(last_hour)::BIGINT, hours, value
            FROM leagues WHERE kind <> 'private' AND value > 0 ORDER BY last_hour DESC, value DESC
            """
        ).fetchall()
        return [dict(zip(("league", "kind", "first_hour", "last_hour", "hours", "value"), r)) for r in rows]

    def default_league(self) -> str:
        """The live challenge league with the most trade; the user's choice wins if it still exists."""
        leagues = self.leagues()
        if not leagues:
            raise ApiError("No leagues in the archive yet.", 503)
        chosen = self.ledger.get("league")
        if chosen and any(l["league"] == chosen for l in leagues):
            return chosen
        newest = max(l["last_hour"] for l in leagues)
        live = [l for l in leagues if l["last_hour"] >= newest - 6 * HOUR]
        challenge = [l for l in live if l["kind"] == "challenge"] or live
        return max(challenge, key=lambda l: l["value"] / max(l["hours"], 1))["league"]

    def desk(self, league: str | None = None) -> Desk:
        league = league or self.default_league()
        with self._lock:
            if league not in self._desks:
                self._desks[league] = Desk.build(self.cursor(), league, realm=REALM, root=self.root)
            return self._desks[league]

    def book(self) -> LeagueBook:
        with self._lock:
            if self._book is None:
                self._book = LeagueBook(self.cursor(), REALM)
            return self._book

    # ----------------------------------------------------------------- sync
    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
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
            if not items.load(self.root, REALM):
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
            self.ledger.evaluate_alerts(desk.league, rows, desk.as_of)
        except ApiError:
            pass

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

    def api_flips(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        plans = desk.flips(share=float(q.get("share", 0.1)), min_margin=float(q.get("min_margin", 0.02)))
        return {"summary": desk.summary(), "flips": plans[: int(q.get("limit", 300))]}

    def api_arbitrage(self, q: dict) -> dict:
        desk = self.desk(q.get("league"))
        return {"summary": desk.summary(), "conversions": desk.conversions(share=float(q.get("share", 0.1))),
                "swaps": desk.triangles()[:60]}

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
        return out

    def api_curve(self, q: dict) -> dict:
        return {"curve": self.book().curve(q["id"], max_day=int(q.get("max_day", 90)))}

    def api_backtest(self, q: dict, body: dict) -> dict:
        league = body.get("league") or self.default_league()
        spec = body.get("strategy", "swing")
        if spec not in backtest.STRATEGIES:
            raise ApiError("The desk runs built-in strategies only; use the CLI for your own modules.")
        params = {k: v for k, v in (body.get("params") or {}).items() if v not in ("", None)}
        try:
            result = backtest.run(
                self.cursor(), league, spec, params=params,
                capital=float(body.get("capital", 10000)), participation=float(body.get("participation", 0.1)),
                slippage=float(body.get("slippage", 0.03)), start_day=float(body.get("start_day", 0)),
                days=float(body["days"]) if body.get("days") else None,
                attention=int(body.get("attention", 1)), compound=bool(body.get("compound", False)),
            )
        except (TypeError, ValueError, LookupError) as err:
            raise ApiError(str(err)) from err
        step = max(1, len(result.hours) // 600)
        return {
            "summary": result.summary(),
            "t": result.hours[::step].tolist(),
            "equity": [_num(v) for v in result.equity[::step]],
            "divine": [_num(v * result.start_capital) for v in result.divine[::step]],
        }

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
        return {
            "league": desk.league, "bridge": _num(desk.bridge), "positions": positions, "trades": trades[::-1][:200],
            "totals": {"value": _num(total_value), "cost": _num(total_cost),
                       "unrealized": _num(total_value - total_cost), "realized": _num(total_realized)},
            "watchlist": self.ledger.watchlist(desk.league), "alerts": self.ledger.alerts(desk.league),
            "alert_fields": ALERT_FIELDS,
        }

    def api_post(self, path: str, body: dict) -> dict:
        league = body.get("league") or self.default_league()
        try:
            if path == "/api/trade":
                if body.get("delete"):
                    self.ledger.delete_trade(body["delete"])
                else:
                    self.ledger.add_trade(league, body["item"], body["side"], body["units"], body["price"],
                                          body.get("note", ""))
            elif path == "/api/watch":
                self.ledger.watch(league, body["item"], bool(body.get("on", True)))
            elif path == "/api/alert":
                if body.get("delete"):
                    self.ledger.delete_alert(body["delete"])
                else:
                    self.ledger.add_alert(league, body["item"], body["field"], body["op"], body["threshold"])
            elif path == "/api/league":
                self.ledger.set("league", body["league"])
            elif path == "/api/sync":
                self.request_sync(backfill=bool(body.get("backfill")))
            else:
                raise ApiError("Unknown endpoint.", 404)
        except (KeyError, ValueError, TypeError) as err:
            raise ApiError(f"Bad request: {err}") from err
        return {"ok": True}

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
                                             headers={"User-Agent": "vaal-street/2.0"})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read()
        except Exception:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data


GET_ROUTES = {
    "/api/status": "api_status", "/api/desk": "api_desk", "/api/item": "api_item", "/api/flips": "api_flips",
    "/api/arbitrage": "api_arbitrage", "/api/league": "api_league", "/api/curve": "api_curve",
    "/api/ledger": "api_ledger",
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

        def _json(self, payload, status: int = 200) -> None:
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
