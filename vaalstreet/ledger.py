"""Your own state: trades, watchlist, alerts and settings, in one small SQLite file.

Market data is public and reproducible; this file is the only thing worth backing
up. Positions use average cost: a sale realises (price - average cost) per unit.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from vaalstreet.paths import ledger_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, item TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')), units REAL NOT NULL CHECK(units > 0),
    price REAL NOT NULL CHECK(price >= 0), note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS trades_league ON trades(league, t, id);
CREATE TABLE IF NOT EXISTS watchlist(league TEXT NOT NULL, item TEXT NOT NULL, PRIMARY KEY(league, item));
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
    field TEXT NOT NULL, op TEXT NOT NULL CHECK(op IN ('above','below')), threshold REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, hot INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
ALERT_FIELDS = {"price": "Price", "chaos": "Chaos-market price", "divine": "Divine-market price",
                "premium": "Divine premium", "change_24h": "24h change", "z": "Surprise (z)"}


class Ledger:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(ledger_file(root), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _run(self, sql: str, args: tuple = ()) -> int:
        with self._lock, self._db:
            return self._db.execute(sql, args).lastrowid

    # ------------------------------------------------------------- settings
    def get(self, key: str, default=None):
        rows = self._all("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value) -> None:
        self._run("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, json.dumps(value)))

    # --------------------------------------------------------------- trades
    def add_trade(self, league: str, item: str, side: str, units: float, price: float,
                  note: str = "", t: int | None = None) -> int:
        units, price = float(units), float(price)
        if side not in ("buy", "sell") or not units > 0 or price < 0:
            raise ValueError("A trade needs side buy/sell, positive units and a non-negative price.")
        if side == "sell":
            held = self.positions(league).get(item, {}).get("units", 0.0)
            if units > held + 1e-9:
                raise ValueError(f"Cannot sell {units:g}: the ledger holds {held:g}.")
        return self._run(
            "INSERT INTO trades(t, league, item, side, units, price, note) VALUES (?,?,?,?,?,?,?)",
            (int(t or time.time()), league, item, side, units, price, str(note)[:200]),
        )

    def delete_trade(self, trade_id: int) -> None:
        self._run("DELETE FROM trades WHERE id = ?", (int(trade_id),))

    def trades(self, league: str) -> list[dict]:
        return self._all("SELECT * FROM trades WHERE league = ? ORDER BY t, id", (league,))

    def positions(self, league: str) -> dict[str, dict]:
        """item -> {units, cost, realized}. `cost` is what the units still held were bought for."""
        out: dict[str, dict] = {}
        for tr in self.trades(league):
            p = out.setdefault(tr["item"], {"units": 0.0, "cost": 0.0, "realized": 0.0, "last": 0})
            if tr["side"] == "buy":
                p["units"] += tr["units"]
                p["cost"] += tr["units"] * tr["price"]
            else:
                sold = min(tr["units"], p["units"])
                average = p["cost"] / p["units"] if p["units"] else 0.0
                p["realized"] += tr["units"] * tr["price"] - sold * average
                p["cost"] -= sold * average
                p["units"] -= sold
            p["last"] = tr["t"]
        return out

    # ------------------------------------------------------------ watchlist
    def watchlist(self, league: str) -> list[str]:
        return [r["item"] for r in self._all("SELECT item FROM watchlist WHERE league = ? ORDER BY item", (league,))]

    def watch(self, league: str, item: str, on: bool) -> None:
        if on:
            self._run("INSERT OR IGNORE INTO watchlist VALUES (?, ?)", (league, item))
        else:
            self._run("DELETE FROM watchlist WHERE league = ? AND item = ?", (league, item))

    # --------------------------------------------------------------- alerts
    def alerts(self, league: str) -> list[dict]:
        return self._all("SELECT * FROM alerts WHERE league = ? ORDER BY id", (league,))

    def add_alert(self, league: str, item: str, field: str, op: str, threshold: float) -> int:
        if field not in ALERT_FIELDS or op not in ("above", "below"):
            raise ValueError("Unknown alert field or direction.")
        return self._run("INSERT INTO alerts(league, item, field, op, threshold) VALUES (?,?,?,?,?)",
                         (league, item, field, op, float(threshold)))

    def delete_alert(self, alert_id: int) -> None:
        self._run("DELETE FROM alerts WHERE id = ?", (int(alert_id),))

    def evaluate_alerts(self, league: str, rows: dict[str, dict], now: int) -> list[dict]:
        """Fire alerts that newly crossed their threshold; they re-arm once the value comes back."""
        fired = []
        for alert in self.alerts(league):
            row = rows.get(alert["item"])
            value = row.get(alert["field"]) if row else None
            if value is None or not alert["active"]:
                continue
            crossed = value > alert["threshold"] if alert["op"] == "above" else value < alert["threshold"]
            if crossed and not alert["hot"]:
                title = f"{row['name']}: {ALERT_FIELDS[alert['field']].lower()} {alert['op']} {alert['threshold']:g}"
                body = f"Now {value:g}"
                self._run("INSERT INTO events(t, league, title, body) VALUES (?,?,?,?)", (now, league, title, body))
                fired.append({"title": title, "body": body})
            if bool(alert["hot"]) != crossed:
                self._run("UPDATE alerts SET hot = ? WHERE id = ?", (int(crossed), alert["id"]))
        return fired

    def events(self, league: str, limit: int = 30) -> list[dict]:
        return self._all("SELECT * FROM events WHERE league = ? ORDER BY id DESC LIMIT ?", (league, limit))
