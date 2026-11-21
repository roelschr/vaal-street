"""Your own state: trades, orders, sessions, recipe checks, watchlist, alerts and settings.

Market data is public and reproducible; this one small SQLite file is the only thing
worth backing up. Positions use average cost: a sale realises (price - average cost) per unit.

The file outlives the code, so the schema only ever grows: new tables are created if
missing and new columns are added if missing (`_migrate`). Nothing is rewritten or dropped.

Gold is recorded where it was paid and nowhere else. A trade typed in by hand may carry
the Gold it cost; an order carries the Gold paid to post it, and the trade its fill creates
carries none, so no Gold is ever counted twice. GGG does not publish the fee schedule, so
the ledger only learns fees from what you recorded (`gold_fees`) and says nothing otherwise.

The stash is what you say you hold, typed in by hand: it is separate from the positions the
trades build, so you can value loot you never recorded buying.

Recipe checks are game facts, not market state, so they are kept once for all leagues.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
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
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, item TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')), market TEXT NOT NULL CHECK(market IN ('chaos','divine')),
    currency_units INTEGER NOT NULL CHECK(currency_units > 0), item_units INTEGER NOT NULL CHECK(item_units > 0),
    units REAL NOT NULL CHECK(units > 0), filled REAL NOT NULL DEFAULT 0, gold REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','part-filled','filled','cancelled','closed')),
    created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS orders_league ON orders(league, status, id);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY, league TEXT NOT NULL, started INTEGER NOT NULL, stopped INTEGER,
    expected_per_hour REAL, plan TEXT NOT NULL DEFAULT '{}', gold_earned REAL);
CREATE TABLE IF NOT EXISTS recipe_checks(
    id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('confirmed','failed')),
    note TEXT NOT NULL DEFAULT '', t INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS recipe_checks_key ON recipe_checks(key, t, id);
CREATE TABLE IF NOT EXISTS stash(
    league TEXT NOT NULL, item TEXT NOT NULL, units REAL NOT NULL CHECK(units > 0), updated INTEGER NOT NULL,
    PRIMARY KEY(league, item));
"""
# Columns added to tables that existed before; `_migrate` adds any that an older file lacks.
_COLUMNS = {"trades": {"gold": "REAL", "order_id": "INTEGER"}}
ORDER_STATUSES = ("open", "part-filled", "filled", "cancelled", "closed")
# Whole-number ratios never sit exactly on a level, so an order counts as "at the plan" within this.
REPRICE_TOLERANCE = 0.04
# An order is on the wrong side of the market only when it is past that market's price by more than this.
WRONG_SIDE_TOLERANCE = 0.01


def recipe_key(kind: str, item_ids: list[str]) -> str:
    """A stable name for a recipe or round trip: its kind and the item ids it passes through.

    "set:<input>><reward>" for a conversion, "loop:<a>><b>>..." for a round trip. The kind keeps
    a two-item round trip from colliding with a conversion between the same items.
    """
    return f"{kind}:" + ">".join(item_ids)
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
            self._migrate()

    def _migrate(self) -> None:
        for table, columns in _COLUMNS.items():
            have = {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

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
                  note: str = "", t: int | None = None, gold: float | None = None,
                  order_id: int | None = None) -> int:
        units, price = float(units), float(price)
        gold = _gold(gold)
        if side not in ("buy", "sell") or not units > 0 or price < 0:
            raise ValueError("A trade needs side buy/sell, positive units and a non-negative price.")
        if side == "sell":
            held = self.positions(league).get(item, {}).get("units", 0.0)
            if units > held + 1e-9:
                raise ValueError(f"Cannot sell {units:g}: the ledger holds {held:g}.")
        return self._run(
            "INSERT INTO trades(t, league, item, side, units, price, note, gold, order_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(t or time.time()), league, item, side, units, price, str(note)[:200], gold, order_id),
        )

    def delete_trade(self, trade_id: int) -> None:
        self._run("DELETE FROM trades WHERE id = ?", (int(trade_id),))

    def trades(self, league: str) -> list[dict]:
        return self._all("SELECT * FROM trades WHERE league = ? ORDER BY t, id", (league,))

    def positions(self, league: str) -> dict[str, dict]:
        """item -> {units, cost, realized}. `cost` is what the units still held were bought for."""
        return self._replay(league)[0]

    def _replay(self, league: str) -> tuple[dict[str, dict], list[tuple[int, float]]]:
        """Positions, and (time, profit realised) for every sale, from the whole trade history."""
        out: dict[str, dict] = {}
        sales: list[tuple[int, float]] = []
        for tr in self.trades(league):
            p = out.setdefault(tr["item"], {"units": 0.0, "cost": 0.0, "realized": 0.0, "last": 0})
            if tr["side"] == "buy":
                p["units"] += tr["units"]
                p["cost"] += tr["units"] * tr["price"]
            else:
                sold = min(tr["units"], p["units"])
                average = p["cost"] / p["units"] if p["units"] else 0.0
                gain = tr["units"] * tr["price"] - sold * average
                p["realized"] += gain
                sales.append((tr["t"], gain))
                p["cost"] -= sold * average
                p["units"] -= sold
            p["last"] = tr["t"]
        return out, sales

    def gold_fees(self, league: str) -> dict[str, dict]:
        """item -> {gold_per_unit, trades}: the median Gold paid per unit over your own records.

        Both trades and orders count, each once (see the module docstring). Items you never
        recorded Gold for have no entry: the fee schedule is not published, and we do not guess it.
        """
        samples: dict[str, list[float]] = {}
        for row in self._all("SELECT item, gold, units FROM trades WHERE league = ? AND gold IS NOT NULL "
                             "UNION ALL SELECT item, gold, units FROM orders WHERE league = ? AND gold IS NOT NULL",
                             (league, league)):
            samples.setdefault(row["item"], []).append(row["gold"] / row["units"])
        return {item: {"gold_per_unit": statistics.median(values), "trades": len(values)}
                for item, values in samples.items()}

    # --------------------------------------------------------------- orders
    def add_order(self, league: str, item: str, side: str, market: str, currency_units: int,
                  item_units: int, units: float, gold: float | None = None, t: int | None = None) -> int:
        """An order as posted: `item_units` items for `currency_units` Chaos or Divine, `units` items in all."""
        if side not in ("buy", "sell") or market not in ("chaos", "divine"):
            raise ValueError("An order needs side buy/sell and market chaos/divine.")
        if int(currency_units) != float(currency_units) or int(item_units) != float(item_units):
            raise ValueError("The exchange only takes whole-number ratios.")
        if not int(currency_units) > 0 or not int(item_units) > 0 or not float(units) > 0:
            raise ValueError("An order needs a positive ratio and positive units.")
        now = int(t or time.time())
        return self._run(
            "INSERT INTO orders(league, item, side, market, currency_units, item_units, units, gold, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (league, item, side, market, int(currency_units), int(item_units), float(units), _gold(gold), now, now))

    def orders(self, league: str, include_done: bool = False) -> list[dict]:
        done = "" if include_done else " AND status NOT IN ('cancelled','closed')"
        return self._all(f"SELECT * FROM orders WHERE league = ?{done} ORDER BY id", (league,))

    def order(self, order_id: int) -> dict:
        rows = self._all("SELECT * FROM orders WHERE id = ?", (int(order_id),))
        if not rows:
            raise ValueError("No such order.")
        return rows[0]

    def fill_order(self, order_id: int, units: float, bridge: float | None = None,
                   record_trade: bool = True, t: int | None = None) -> int | None:
        """Record that `units` more of an order filled; returns the trade it created, if any.

        The trade's price is Chaos per item. A Divine-market order is converted at `bridge`, the
        Divine price now, which the caller supplies so that this file stays free of market models.
        The trade is written first: if it is refused (selling more than the ledger holds), the order
        is left as it was.
        """
        order, units, now = self.order(order_id), float(units), int(t or time.time())
        if order["status"] not in ("open", "part-filled"):
            raise ValueError("Only an open order can fill.")
        left = order["units"] - order["filled"]
        if not units > 0 or units > left + 1e-9:
            raise ValueError(f"Fill between 0 and the {left:g} units still open.")
        trade_id = None
        if record_trade:
            price = order["currency_units"] / order["item_units"]
            if order["market"] == "divine":
                if not bridge or bridge <= 0:
                    raise ValueError("A Divine-market fill needs the Divine price to record the trade.")
                price *= bridge
            trade_id = self.add_trade(order["league"], order["item"], order["side"], units, price,
                                      note=f"order {order['id']}", t=now, order_id=order["id"])
        filled = order["filled"] + units
        status = "filled" if filled >= order["units"] - 1e-9 else "part-filled"
        self._run("UPDATE orders SET filled = ?, status = ?, updated = ? WHERE id = ?", (filled, status, now, order["id"]))
        return trade_id

    def set_order_status(self, order_id: int, status: str, t: int | None = None) -> None:
        """Cancel an order, or close one you have collected from the exchange."""
        if status not in ("cancelled", "closed"):
            raise ValueError("An order can only be cancelled or closed by hand; fills go through fill_order.")
        self.order(order_id)
        self._run("UPDATE orders SET status = ?, updated = ? WHERE id = ?", (status, int(t or time.time()), int(order_id)))

    def delete_order(self, order_id: int) -> None:
        self._run("DELETE FROM orders WHERE id = ?", (int(order_id),))

    def advise_orders(self, league: str, prices: dict[str, dict], plans: dict[str, dict],
                      bridge: float) -> list[dict]:
        """Each open, part-filled or filled order with advice: keep, re-price, cancel or collect.

        `prices` is item -> {"fair": combined fair value, "chaos": Chaos-market price, "divine":
        Divine-market price}, all in Chaos; a market price may be None when that market is quiet.
        `plans` is item -> side -> market -> {"currency_units", "item_units", "price"}: where the
        flip plan would post today. Both come from the desk, so this file stays free of model code.

        The two markets of an item are different prices, so an order is judged against its own
        market's price, never the combined fair value when the market's own price is known.
        Filled orders say "collect" until you close them.
        """
        out = []
        for order in self.orders(league):
            price = order["currency_units"] / order["item_units"] * (bridge if order["market"] == "divine" else 1.0)
            row = dict(order, price=price, advice="keep", reason="", suggest=None)
            out.append(row)
            if order["status"] == "filled":
                row["advice"], row["reason"] = "collect", "It has filled: take what you bought or earned from the exchange, then close it."
                continue
            market_name = "Divine" if order["market"] == "divine" else "Chaos"
            known = prices.get(order["item"]) or {}
            market = known.get(order["market"]) or known.get("fair")
            level = ((plans.get(order["item"]) or {}).get(order["side"]) or {}).get(order["market"])
            near_plan = bool(level and level.get("price")
                             and abs(math.log(price / level["price"])) <= REPRICE_TOLERANCE)
            if not market:
                row["reason"] = "The desk has no recent price for this item, so it cannot judge this order."
                continue
            buying = order["side"] == "buy"
            past = price / market - 1 if buying else 1 - price / market
            if past > WRONG_SIDE_TOLERANCE and not near_plan:
                row["advice"] = "cancel"
                row["reason"] = (f"You are {'paying' if buying else 'asking'} {abs(past):.0%} "
                                 f"{'more' if buying else 'less'} than the {market_name} market's price now.")
                continue
            if level and not near_plan:
                row["advice"], row["suggest"] = "re-price", level
                row["reason"] = (f"The plan would now post {level['item_units']} for {level['currency_units']} "
                                 f"{market_name}, {abs(price / level['price'] - 1):.0%} "
                                 f"{'below' if price > level['price'] else 'above'} your price.")
            elif level:
                row["reason"] = "Your price is where the plan would post it."
            else:
                row["reason"] = "No flip plan for this order today; it is on the right side of the market."
        return out

    # ------------------------------------------------------------- sessions
    def start_session(self, league: str, expected_per_hour: float | None = None, plan: dict | None = None,
                      t: int | None = None) -> int:
        """Start a play session. `plan` is a snapshot of what you intend to run, kept as you sent it."""
        if self.running_session(league):
            raise ValueError("A session is already running in this league.")
        expected = float(expected_per_hour) if expected_per_hour not in (None, "") else None
        return self._run("INSERT INTO sessions(league, started, expected_per_hour, plan) VALUES (?,?,?,?)",
                         (league, int(t or time.time()), expected, json.dumps(plan or {})))

    def stop_session(self, session_id: int, gold_earned: float | None = None, t: int | None = None) -> None:
        self._run("UPDATE sessions SET stopped = ?, gold_earned = ? WHERE id = ? AND stopped IS NULL",
                  (int(t or time.time()), _gold(gold_earned), int(session_id)))

    def delete_session(self, session_id: int) -> None:
        self._run("DELETE FROM sessions WHERE id = ?", (int(session_id),))

    def running_session(self, league: str) -> dict | None:
        rows = self._all("SELECT id FROM sessions WHERE league = ? AND stopped IS NULL ORDER BY id DESC LIMIT 1",
                         (league,))
        return rows[0] if rows else None

    def sessions(self, league: str, now: int | None = None, limit: int = 20) -> list[dict]:
        """Recent sessions with their figures; a running session is reported up to `now`.

        Profit is what sales inside the session realised against the average cost of everything
        bought before, so stock carried in from earlier counts at what you paid for it.
        Gold spent is the Gold on trades made and orders posted inside the session.
        """
        now = int(now or time.time())
        _, sales = self._replay(league)
        out = []
        for s in self._all("SELECT * FROM sessions WHERE league = ? ORDER BY id DESC LIMIT ?", (league, limit)):
            end = s["stopped"] or now
            spent = self._all(
                "SELECT (SELECT COALESCE(SUM(gold), 0) FROM trades WHERE league = ?1 AND t BETWEEN ?2 AND ?3)"
                " + (SELECT COALESCE(SUM(gold), 0) FROM orders WHERE league = ?1 AND created BETWEEN ?2 AND ?3) AS g",
                (league, s["started"], end))[0]["g"]
            profit = sum(gain for t, gain in sales if s["started"] <= t <= end)
            hours = (end - s["started"]) / 3600
            out.append(dict(s, plan=json.loads(s["plan"]), running=s["stopped"] is None, hours=hours,
                            profit=profit, gold_spent=spent,
                            per_hour=profit / hours if hours > 0 else None))
        return out

    # -------------------------------------------------------- recipe checks
    def check_recipe(self, key: str, status: str, note: str = "", t: int | None = None) -> int:
        """Record that a recipe worked (or did not) when you tried it in game. Kept for every league."""
        if status not in ("confirmed", "failed") or not key:
            raise ValueError("A recipe check needs a recipe and confirmed or failed.")
        return self._run("INSERT INTO recipe_checks(key, status, note, t) VALUES (?,?,?,?)",
                         (str(key)[:300], status, str(note)[:200], int(t or time.time())))

    def delete_recipe_check(self, check_id: int) -> None:
        self._run("DELETE FROM recipe_checks WHERE id = ?", (int(check_id),))

    def recipe_checks(self) -> dict[str, dict]:
        """key -> the latest check of that recipe. Older checks stay in the file as history."""
        out: dict[str, dict] = {}
        for row in self._all("SELECT * FROM recipe_checks ORDER BY t, id"):
            out[row["key"]] = row
        return out

    # ---------------------------------------------------------------- stash
    def stash(self, league: str) -> dict[str, float]:
        """item -> units you entered as held in this league."""
        rows = self._all("SELECT item, units FROM stash WHERE league = ? ORDER BY item", (league,))
        return {r["item"]: r["units"] for r in rows}

    def set_stash(self, league: str, item: str, units: float, t: int | None = None) -> None:
        """Set how many of an item you hold; zero removes the line."""
        units = float(units)
        if not math.isfinite(units) or units < 0:
            raise ValueError("Units must be zero or more.")
        if units == 0:
            self._run("DELETE FROM stash WHERE league = ? AND item = ?", (league, item))
            return
        self._run("INSERT INTO stash VALUES (?, ?, ?, ?) ON CONFLICT(league, item) DO UPDATE SET "
                  "units = excluded.units, updated = excluded.updated", (league, item, units, int(t or time.time())))

    def clear_stash(self, league: str) -> None:
        self._run("DELETE FROM stash WHERE league = ?", (league,))

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


def _gold(value) -> float | None:
    """Gold as recorded: None when not given (unknown, never zero), else a non-negative number."""
    if value in (None, ""):
        return None
    value = float(value)
    if not value >= 0:
        raise ValueError("Gold cannot be negative.")
    return value
