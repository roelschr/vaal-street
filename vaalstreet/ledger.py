"""Your own state: trades, items you made, orders, sessions, recipe checks, watchlist, alerts and settings.

Market data is public and reproducible; this one small SQLite file is the only thing
worth backing up. Positions use average cost: a sale realises (price - average cost) per unit.

Not every item you sell was bought: three essences merge into one of the next tier, a card set
turns into its reward. Such an item is recorded as made (`add_made`): the items used leave your
holdings at their average cost, and that cost becomes the cost of the item made. Nothing is
realised until the made item is sold, so a merge-and-sell shows the profit of the whole round.
These are events in one league's history, like trades; they are not the recipes below.

Typing every merge in is slow, and a sale proves you had the items. So a sale that needs more
than the ledger holds is never refused. Given the recipes the game data knows (`add_trade`'s
`recipes`), the ledger first makes the missing units from what it holds, lower tiers first, and
records those merges itself, linked to the sale (`made.sale`): deleting the sale deletes them
too. What is still missing after that has no recorded cost. It is sold at zero cost, so its
whole price counts as profit, the same as loot; `uncovered` says how many units that was.
That profit is counted, and also kept apart: every position and every session says how much
of its realised profit came from such units (`loot`), so what your trading earned can be read
without what you found. Units with no cost that went into an item you made are not followed
through the merge: they lower the made item's cost, and show as trading profit when it sells.

The file outlives the code, so the schema only ever grows: new tables are created if
missing and new columns are added if missing (`_migrate`). Nothing is rewritten or dropped.

Gold is recorded where it was paid and nowhere else. A trade typed in by hand may carry
the Gold it cost; an order carries the Gold paid to post it, and the trade its fill creates
carries none, so no Gold is ever counted twice. GGG does not publish the fee schedule, so
the ledger only learns fees from what you recorded (`gold_fees`) and says nothing otherwise.

The stash is what you say you hold, typed in by hand: it is separate from the positions the
trades build, so you can value loot you never recorded buying.

Recipe checks are game facts, not market state, so they are kept once for all leagues. So are
the conversions you enter yourself (`fixed_conversions`): a recipe (N of A, and maybe more inputs,
make M of B) or a vendor price (a vendor sells B for N of A). No public source we may use carries
either, so the desk ships none and only knows the ones you type in.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Callable

from vaalstreet.paths import ledger_file

# Your own assumptions, kept in the ledger. No fee is assumed until you enter one: GGG does not
# publish the schedule. Minutes per order is a rough guess at your clicking speed.
# `ninja_prices`: 1 lets the desk fetch prices from poe.ninja for card rewards the exchange does
# not trade (uniques, gems, base types); 0 turns that off. On unless you turn it off.
SETTINGS = {"assumed_gold_fee": None, "minutes_per_order": 1.0, "ninja_prices": 1.0}

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
CREATE TABLE IF NOT EXISTS fixed_conversions(
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('recipe','vendor')),
    inputs TEXT NOT NULL, output TEXT NOT NULL, gives REAL NOT NULL CHECK(gives > 0),
    note TEXT NOT NULL DEFAULT '', t INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS made(
    id INTEGER PRIMARY KEY, t INTEGER NOT NULL, league TEXT NOT NULL, inputs TEXT NOT NULL,
    output TEXT NOT NULL, gives REAL NOT NULL CHECK(gives > 0), note TEXT NOT NULL DEFAULT '',
    after_trade INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS made_league ON made(league, t, id);
"""
# Columns added to tables that existed before; `_migrate` adds any that an older file lacks.
_COLUMNS = {"trades": {"gold": "REAL", "order_id": "INTEGER"},
            # The sale a made entry was worked out for; deleting that sale deletes the entry.
            "made": {"sale": "INTEGER"},
            # An alert's kind and its rule; `state` remembers which conditions are live and when each last fired.
            "alerts": {"kind": "TEXT NOT NULL DEFAULT 'price'", "params": "TEXT NOT NULL DEFAULT '{}'",
                       "state": "TEXT NOT NULL DEFAULT '{}'"},
            # The order this one replaced when you cancelled it and posted again at another ratio.
            "orders": {"reposted_from": "INTEGER"}}
ORDER_STATUSES = ("open", "part-filled", "filled", "cancelled", "closed")
# Whole-number ratios never sit exactly on a level, so an order counts as "at the plan" within this.
REPRICE_TOLERANCE = 0.04
# An order is on the wrong side of the market only when it is past that market's price by more than this.
WRONG_SIDE_TOLERANCE = 0.01
# Gold is not refunded on cancel (confirmed in game), so a re-price pays the full fee again. When that
# fee is known, an order is re-priced only when it is this far from the plan; nearer, it is kept.
REPRICE_WITH_GOLD = 0.08


def recipe_key(kind: str, item_ids: list[str]) -> str:
    """A stable name for a recipe or round trip: its kind and the item ids it passes through.

    "set:<input>><reward>" for a conversion, "loop:<a>><b>>..." for a round trip. The kind keeps
    a two-item round trip from colliding with a conversion between the same items.
    """
    return f"{kind}:" + ">".join(item_ids)


def fixed_key(kind: str, inputs: list[tuple[str, float]], output: str, gives: float) -> str:
    """The recipe key of a conversion you entered: "recipe:3*a+1*b>1*c" or "vendor:5*a>1*c".

    The counts are part of the key, so correcting a ratio starts the corrected recipe unchecked
    instead of letting it inherit a check of the wrong one.
    """
    left = "+".join(f"{float(n):g}*{item}" for item, n in inputs)
    return f"{kind}:{left}>{float(gives):g}*{output}"


def describe_key(key: str, name: Callable[[str], str]) -> str:
    """A recipe key in words, for a check whose recipe is no longer listed (you deleted it, or it stopped
    trading): "3 Some Card + 1 Some Shard → 2 Some Orb", "5 Chaos Orb buys 1 Some Shard", "A → B → C".
    `name` turns an item id into its name. A key that cannot be read is "a recipe you deleted"."""
    kind, _, body = key.partition(":")
    try:
        if kind in ("recipe", "vendor"):
            left, right = body.split(">")

            def side(text: str) -> str:
                return " + ".join(f"{float(n):g} {name(item)}" for n, item in (part.split("*", 1) for part in text.split("+")))

            return f"{side(left)} {'buys' if kind == 'vendor' else '→'} {side(right)}"
        if kind in ("set", "loop") and body:
            return " → ".join(name(item) for item in body.split(">"))
    except ValueError:
        pass
    return "a recipe you deleted"


ALERT_FIELDS = {"price": "Price", "chaos": "Chaos-market price", "divine": "Divine-market price",
                "premium": "Divine premium", "change_24h": "24h change", "z": "Surprise (z)"}


class Ledger:
    def __init__(self, root: Path, *, path: Path | None = None, clock: Callable[[], float] | None = None) -> None:
        """The ledger in `root` (desk.sqlite), or in `path`. `clock` stamps new rows: a rehearsal's
        practice ledger runs on the rehearsal's hour, not the wall clock."""
        root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self.path = path or ledger_file(root)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

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
                  order_id: int | None = None, recipes: dict[str, list[dict]] | None = None) -> int:
        """Record a trade; returns its id.

        A sale is never refused for selling more than the ledger holds: you had the items, or you could
        not have sold them. `recipes` is what each item can be made from, {output: [{"inputs": [(item,
        units), ...], "gives": n}, ...]}. With it, a sale the holdings do not cover first makes the missing
        units from items the ledger holds and records those merges (see `_plan_merges`); `merges_for`
        lists them. What is still missing is sold at zero cost (`uncovered`).
        """
        units, price = float(units), float(price)
        gold = _gold(gold)
        if side not in ("buy", "sell") or not units > 0 or price < 0:
            raise ValueError("A trade needs side buy/sell, positive units and a non-negative price.")
        when = int(t or self.clock())  # read before taking the lock: a rehearsal's clock takes a lock of its own
        # Planning, the merges and the sale are one step, so two sales that arrive together cannot use the same items.
        with self._lock, self._db:
            before = _history(self._db, league)[2]
            trade_id = _next_trade_id(self._db)
            merges = []
            if side == "sell" and recipes:
                # The holdings at the sale's own place in the history, which is not today for a back-dated sale.
                merges = _plan_merges(_history(self._db, league, until=(when, trade_id))[0], item, units, recipes)
            made_ids = [self._insert_made(league, when, inputs, output, gives, "", trade_id - 1, sale=trade_id)
                        for inputs, output, gives in merges]
            self._db.execute(
                "INSERT INTO trades(id, t, league, item, side, units, price, note, gold, order_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (trade_id, when, league, item, side, units, price, str(note)[:200], gold, order_id))
            new = _newly_short(self._db, league, before) - {("trade", trade_id)}
            if new and made_ids:
                # A back-dated sale whose merges used items that later records needed: sell without them instead.
                self._db.executemany("DELETE FROM made WHERE id = ?", [(made_id,) for made_id in made_ids])
                new = _newly_short(self._db, league, before) - {("trade", trade_id)}
            # Only for a sale dated before others: it takes units that a later sale or item you made needed.
            _refuse_short(self._db, "A sale at that time", new)
            return trade_id

    def merges_for(self, trade_id: int) -> list[dict]:
        """The merges the ledger worked out and recorded for a sale, lowest tier first."""
        rows = self._all("SELECT * FROM made WHERE sale = ? ORDER BY id", (int(trade_id),))
        for row in rows:
            row["inputs"] = [(item, units) for item, units in json.loads(row["inputs"])]
        return rows

    def uncovered(self, league: str) -> dict[tuple[str, int], float]:
        """("trade" or "made", id) -> units that entry took beyond what the ledger held: sold or used at zero cost."""
        with self._lock:
            return _history(self._db, league)[2]

    def edit_trade(self, trade_id: int, **changes) -> None:
        """Correct a recorded trade: its item, side, units, price, Gold or note. Its time stays.

        The edit is checked against the whole history, not only today's holdings, so turning an
        old buy into a sale cannot leave a later sale selling units you never had, nor leave an
        item you made short of the items it used. The edited sale itself may sell more than was
        held, as a new one may. A trade an order fill created keeps the order's item, side and
        units, so the order and its fills still agree; so does a sale the ledger recorded merges
        for, since the merges were worked out for that item and count.
        """
        fields = ("item", "side", "units", "price", "gold", "note")
        unknown = set(changes) - set(fields)
        if unknown:
            raise ValueError(f"A trade has no field {', '.join(sorted(unknown))}.")
        with self._lock, self._db:  # one transaction: a refused edit rolls back
            row = self._db.execute("SELECT * FROM trades WHERE id = ?", (int(trade_id),)).fetchone()
            if row is None:
                raise ValueError(f"No trade {trade_id}.")
            new = dict(row) | changes
            new["units"], new["price"], new["gold"] = float(new["units"]), float(new["price"]), _gold(new["gold"])
            new["note"] = str(new["note"] or "")[:200]
            if new["side"] not in ("buy", "sell") or not new["units"] > 0 or new["price"] < 0 or not new["item"]:
                raise ValueError("A trade needs an item, side buy/sell, positive units and a non-negative price.")
            if row["order_id"] is not None:
                if any(new[k] != row[k] for k in ("item", "side", "units")):
                    raise ValueError("This trade came from an order fill: its item, side and units follow the order. "
                                     "Delete it and record the trade by hand instead.")
                new["gold"] = row["gold"]  # the order carries the Gold, never its fill (see the module docstring)
            if (any(new[k] != row[k] for k in ("item", "side", "units"))
                    and self._db.execute("SELECT 1 FROM made WHERE sale = ?", (int(trade_id),)).fetchone()):
                raise ValueError("The ledger recorded merges for this sale: they were worked out for its item and units. "
                                 "Delete the sale and record it again instead.")
            before = _history(self._db, row["league"])[2]
            self._db.execute("UPDATE trades SET item = ?, side = ?, units = ?, price = ?, gold = ?, note = ? WHERE id = ?",
                             (new["item"], new["side"], new["units"], new["price"], new["gold"], new["note"], int(trade_id)))
            _refuse_short(self._db, "That edit", _newly_short(self._db, row["league"], before) - {("trade", int(trade_id))})

    def delete_trade(self, trade_id: int) -> None:
        """Delete a trade, with the merges recorded for it. Refused when a later sale or an item you made
        needed the units it bought."""
        self._delete("trades", trade_id)

    def _delete(self, table: str, row_id: int) -> None:
        """Delete a trade or a made entry, unless the history would then take units the ledger never held.

        Without this, deleting the entry that made an item you already sold would put its inputs back in
        your holdings and keep the sale's profit: the same units counted twice. A sale takes the merges
        recorded for it along, so a merge the ledger worked out wrongly is undone by deleting its sale.
        """
        with self._lock, self._db:  # one transaction: a refused delete rolls back
            row = self._db.execute(f"SELECT * FROM {table} WHERE id = ?", (int(row_id),)).fetchone()
            if row is None:
                return
            if table == "made" and row["sale"] is not None:
                raise ValueError("The ledger recorded this merge for a sale. Delete the sale to undo it, "
                                 "and record the sale again with \"Made from what I hold\" unticked.")
            before = _history(self._db, row["league"])[2]
            if table == "trades":
                self._db.execute("DELETE FROM made WHERE sale = ?", (int(row_id),))
            self._db.execute(f"DELETE FROM {table} WHERE id = ?", (int(row_id),))
            _refuse_short(self._db, "Deleting this", _newly_short(self._db, row["league"], before))

    def trades(self, league: str) -> list[dict]:
        return self._all("SELECT * FROM trades WHERE league = ? ORDER BY t, id", (league,))

    def positions(self, league: str) -> dict[str, dict]:
        """item -> {units, cost, realized}. `cost` is what the units still held were bought for."""
        return self._replay(league)[0]

    def _replay(self, league: str) -> tuple[dict[str, dict], list[tuple[int, float, float]]]:
        """Positions, and (time, profit realised, of which loot) for every sale, from the whole history."""
        with self._lock:
            positions, sales, _ = _history(self._db, league)
        return positions, sales

    # ------------------------------------------------------ items you made
    def add_made(self, league: str, inputs: list, output: str, gives: float = 1, note: str = "",
                 t: int | None = None, name: Callable[[str], str] = str) -> int:
        """Record that you turned items you hold into another item: 3 of one essence into 1 of the next tier.

        `inputs` is [[item id, units], ...]. What the used items cost moves to the item made; nothing is
        realised. As with a sale, using more than the ledger holds is not refused: the units it does not
        hold come at zero cost (`uncovered`). Only an entry dated before others that needed those items
        is refused. `name` is kept for callers that name items in messages.
        """
        used: dict[str, float] = {}
        for item, units in inputs or []:
            units = float(units)
            if not item or not (math.isfinite(units) and units > 0):
                raise ValueError("Every item you used needs a name and a positive count.")
            used[str(item)] = used.get(str(item), 0.0) + units
        gives = float(gives)
        if not used or not output or not (math.isfinite(gives) and gives > 0):
            raise ValueError("Say at least one item you used, the item you got and a positive count of it.")
        output = str(output)
        if output in used:
            raise ValueError("The item you got cannot also be an item you used.")
        when = int(t or self.clock())  # read before taking the lock: a rehearsal's clock takes a lock of its own
        with self._lock, self._db:  # one transaction: a refused entry rolls back
            before = _history(self._db, league)[2]
            # Where this entry sits among records of the same time: after every trade recorded so far, and not
            # before an earlier made entry whose newest trade was deleted since (see `_history`, `_next_trade_id`).
            made_id = self._insert_made(league, when, list(used.items()), output, gives, note,
                                        _next_trade_id(self._db) - 1)
            _refuse_short(self._db, "An item made at that time", _newly_short(self._db, league, before) - {("made", made_id)})
            return made_id

    def _insert_made(self, league: str, t: int, inputs: list, output: str, gives: float, note: str,
                     after_trade: int, sale: int | None = None) -> int:
        """Write one made entry. The caller holds the lock and the transaction."""
        return self._db.execute(
            "INSERT INTO made(t, league, inputs, output, gives, note, after_trade, sale) VALUES (?,?,?,?,?,?,?,?)",
            (t, league, json.dumps([[item, float(units)] for item, units in inputs]),
             output, float(gives), str(note)[:200], int(after_trade), sale)).lastrowid

    def delete_made(self, made_id: int) -> None:
        """Delete a made entry you recorded. Refused when a later sale or made item needed the item it made,
        and for a merge the ledger recorded with a sale, which goes only with its sale."""
        self._delete("made", made_id)

    def made(self, league: str) -> list[dict]:
        """What you recorded making in a league, oldest first; `inputs` is [(item, units), ...]."""
        rows = self._all("SELECT * FROM made WHERE league = ? ORDER BY t, after_trade, id", (league,))
        # `sale` is the trade the ledger worked this entry out for, or None for one you recorded.
        for row in rows:
            row["inputs"] = [(item, units) for item, units in json.loads(row["inputs"])]
        return rows

    def settings(self) -> dict:
        return {key: self.get(key, default) for key, default in SETTINGS.items()}

    def costs(self, league: str):
        """Gold fees learned from your records, your assumed fee and minutes per order, as `desk.Costs`.
        Read fresh on every call, so a newly recorded trade counts at once."""
        from vaalstreet.desk import Costs  # desk pulls in duckdb and numpy; the ledger stays light without it

        settings = self.settings()
        return Costs(self.gold_fees(league), settings["assumed_gold_fee"], settings["minutes_per_order"])

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
        ratio = _ratio(currency_units, item_units, units)
        now = int(t or self.clock())
        return self._run(
            "INSERT INTO orders(league, item, side, market, currency_units, item_units, units, gold, created, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (league, item, side, market, *ratio, _gold(gold), now, now))

    def repost_order(self, order_id: int, currency_units: int, item_units: int, units: float,
                     gold: float | None = None, t: int | None = None) -> int:
        """Record that you cancelled an order in game and posted it again; returns the new order's id.

        The new order takes the old one's league, item, side and market, and points back at it
        (`reposted_from`), so the desk can say how long the item has been listed and what all the
        re-posts cost in Gold. Cancelling the old order and writing the new one happen together: if
        the new order is refused, the old one stays exactly as it was.
        """
        ratio, gold = _ratio(currency_units, item_units, units), _gold(gold)
        now = int(t or self.clock())
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM orders WHERE id = ?", (int(order_id),)).fetchone()
            if row is None:
                raise ValueError("No such order.")
            if row["status"] not in ("open", "part-filled"):
                raise ValueError(f"That order is {row['status']}: only an order still on the exchange can be re-posted.")
            self._db.execute("UPDATE orders SET status = 'cancelled', updated = ? WHERE id = ?", (now, row["id"]))
            return self._db.execute(
                "INSERT INTO orders(league, item, side, market, currency_units, item_units, units, gold, created, updated,"
                " reposted_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["league"], row["item"], row["side"], row["market"], *ratio, gold, now, now, row["id"])).lastrowid

    def orders(self, league: str, include_done: bool = False) -> list[dict]:
        done = "" if include_done else " AND status NOT IN ('cancelled','closed')"
        return self._all(f"SELECT * FROM orders WHERE league = ?{done} ORDER BY id", (league,))

    def order(self, order_id: int) -> dict:
        rows = self._all("SELECT * FROM orders WHERE id = ?", (int(order_id),))
        if not rows:
            raise ValueError("No such order.")
        return rows[0]

    def fill_order(self, order_id: int, units: float, bridge: float | None = None,
                   record_trade: bool = True, t: int | None = None,
                   recipes: dict[str, list[dict]] | None = None) -> int | None:
        """Record that `units` more of an order filled; returns the trade it created, if any.

        The trade's price is Chaos per item. A Divine-market order is converted at `bridge`, the
        Divine price now, which the caller supplies so that this file stays free of market models.
        The trade is written first: if it is refused, the order is left as it was. A sell order's
        fill makes what it needs from `recipes`, as a sale typed in by hand does (`add_trade`).
        """
        order, units, now = self.order(order_id), float(units), int(t or self.clock())
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
                                      note=f"order {order['id']}", t=now, order_id=order["id"], recipes=recipes)
        filled = order["filled"] + units
        status = "filled" if filled >= order["units"] - 1e-9 else "part-filled"
        self._run("UPDATE orders SET filled = ?, status = ?, updated = ? WHERE id = ?", (filled, status, now, order["id"]))
        return trade_id

    def set_order_status(self, order_id: int, status: str, t: int | None = None) -> None:
        """Cancel an order, or close one you have collected from the exchange."""
        if status not in ("cancelled", "closed"):
            raise ValueError("An order can only be cancelled or closed by hand; fills go through fill_order.")
        self.order(order_id)
        self._run("UPDATE orders SET status = ?, updated = ? WHERE id = ?", (status, int(t or self.clock()), int(order_id)))

    def delete_order(self, order_id: int) -> None:
        self._run("DELETE FROM orders WHERE id = ?", (int(order_id),))

    def advise_orders(self, league: str, prices: dict[str, dict], plans: dict[str, dict],
                      bridge: float) -> list[dict]:
        """Each open, part-filled or filled order with advice: keep, re-price, cancel or collect.

        `prices` is item -> {"fair": combined fair value, "chaos": Chaos-market price, "divine":
        Divine-market price}, all in Chaos; a market price may be None when that market is quiet.
        `plans` is item -> side -> market -> [{"currency_units", "item_units", "price"}, ...]: where
        each flip plan would post today, the best plan first (one level alone may be given without
        the list). Both come from the desk, so this file stays free of model code.

        An item can have more than one plan on one side of one market: the Chaos → Divine plan and
        the Divine-market plan both sell for Divines, at different levels. An order belongs to the
        plan whose level is nearest to its price, and is judged against that one. Judged against the
        best plan only, the other plan's own order would be told to re-price or cancel the moment
        it was posted.

        The two markets of an item are different prices, so an order is judged against its own
        market's price, never the combined fair value when the market's own price is known.
        Filled orders say "collect" until you close them.

        Gold is not refunded on cancel (confirmed in game): re-pricing means cancelling and posting
        again, which pays the Gold fee on every unit still open once more. So an order more than
        `REPRICE_TOLERANCE` from the plan is re-priced only when the fee is unknown or free, or when
        it is more than `REPRICE_WITH_GOLD` off; in between it is kept, and the reason says what a
        re-post would cost. An order on the wrong side of its market is cancelled whatever the Gold:
        every fill would lose money, and the Gold it cost is gone either way.
        """
        costs = self.costs(league)
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
            levels = ((plans.get(order["item"]) or {}).get(order["side"]) or {}).get(order["market"]) or []
            levels = [lv for lv in ([levels] if isinstance(levels, dict) else levels) if lv and lv.get("price")]
            # The plan this order belongs to: the one posting nearest to it (the first of equals, so the best).
            level = min(levels, key=lambda lv: abs(math.log(price / lv["price"])), default=None)
            near_plan = bool(level and abs(math.log(price / level["price"])) <= REPRICE_TOLERANCE)
            if not market:
                row["reason"] = "The desk has no recent price for this item, so it cannot judge this order."
                continue
            buying = order["side"] == "buy"
            past = price / market - 1 if buying else 1 - price / market
            fee, _ = costs.fee(order["item"])
            open_units = max(float(order["units"]) - float(order["filled"] or 0), 0.0)
            repost = None if fee is None else fee * open_units
            if past > WRONG_SIDE_TOLERANCE and not near_plan:
                row["advice"] = "cancel"
                row["reason"] = (f"You are {'paying' if buying else 'asking'} {abs(past):.0%} "
                                 f"{'more' if buying else 'less'} than the {market_name} market's price now. "
                                 "The Gold it cost to post is not refunded, but every fill would lose money.")
                continue
            if level and not near_plan:
                off = abs(price / level["price"] - 1)
                row["repost_gold"] = repost
                if repost and off <= REPRICE_WITH_GOLD:
                    row["reason"] = (f"Re-pricing costs about {repost:,.0f} Gold again (Gold is not refunded on "
                                     f"cancel); the order is only {off:.0%} off the plan, so keep it.")
                    continue
                row["advice"], row["suggest"] = "re-price", level
                cost = ("Gold unknown for a re-post" if repost is None
                        else "a re-post costs no Gold" if repost == 0
                        else f"worth the {repost:,.0f} Gold a re-post costs again")
                row["reason"] = (f"The plan would now post {level['item_units']} for {level['currency_units']} "
                                 f"{market_name}, {off:.0%} "
                                 f"{'below' if price > level['price'] else 'above'} your price ({cost}).")
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
                         (league, int(t or self.clock()), expected, json.dumps(plan or {})))

    def stop_session(self, session_id: int, gold_earned: float | None = None, t: int | None = None) -> None:
        self._run("UPDATE sessions SET stopped = ?, gold_earned = ? WHERE id = ? AND stopped IS NULL",
                  (int(t or self.clock()), _gold(gold_earned), int(session_id)))

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

        Units sold that the ledger never saw bought or made cost nothing, so their whole price is
        profit: loot, mostly. That is income, but it is not what your orders earned, and the Flips
        plan a session is compared with knows nothing of it. So `loot` says how much of the profit
        it was, and `trading` and `trading_per_hour` are the profit without it.
        """
        now = int(now or self.clock())
        _, sales = self._replay(league)
        out = []
        for s in self._all("SELECT * FROM sessions WHERE league = ? ORDER BY id DESC LIMIT ?", (league, limit)):
            end = s["stopped"] or now
            spent = self._all(
                "SELECT (SELECT COALESCE(SUM(gold), 0) FROM trades WHERE league = ?1 AND t BETWEEN ?2 AND ?3)"
                " + (SELECT COALESCE(SUM(gold), 0) FROM orders WHERE league = ?1 AND created BETWEEN ?2 AND ?3) AS g",
                (league, s["started"], end))[0]["g"]
            inside = [(gain, loot) for t, gain, loot in sales if s["started"] <= t <= end]
            profit, loot = sum(gain for gain, _ in inside), sum(loot for _, loot in inside)
            hours = (end - s["started"]) / 3600
            out.append(dict(s, plan=json.loads(s["plan"]), running=s["stopped"] is None, hours=hours,
                            profit=profit, loot=loot, trading=profit - loot, gold_spent=spent,
                            per_hour=profit / hours if hours > 0 else None,
                            trading_per_hour=(profit - loot) / hours if hours > 0 else None))
        return out

    # -------------------------------------------------------- recipe checks
    def check_recipe(self, key: str, status: str, note: str = "", t: int | None = None) -> int:
        """Record that a recipe worked (or did not) when you tried it in game. Kept for every league."""
        if status not in ("confirmed", "failed") or not key:
            raise ValueError("A recipe check needs a recipe and confirmed or failed.")
        return self._run("INSERT INTO recipe_checks(key, status, note, t) VALUES (?,?,?,?)",
                         (str(key)[:300], status, str(note)[:200], int(t or self.clock())))

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

    def stash_counted(self, league: str) -> dict[str, int]:
        """item -> when its stash line was last set. Setting a line to the units it already holds counts too:
        you looked and it was still right. A list is only as fresh as its oldest line, and loot since then
        is not in it, so the desk shows this next to every line."""
        return {r["item"]: r["updated"] for r in self._all("SELECT item, updated FROM stash WHERE league = ?", (league,))}

    def set_stash(self, league: str, item: str, units: float, t: int | None = None) -> None:
        """Set how many of an item you hold; zero removes the line."""
        units = float(units)
        if not math.isfinite(units) or units < 0:
            raise ValueError("Units must be zero or more.")
        if units == 0:
            self._run("DELETE FROM stash WHERE league = ? AND item = ?", (league, item))
            return
        self._run("INSERT INTO stash VALUES (?, ?, ?, ?) ON CONFLICT(league, item) DO UPDATE SET "
                  "units = excluded.units, updated = excluded.updated", (league, item, units, int(t or self.clock())))

    def clear_stash(self, league: str) -> None:
        self._run("DELETE FROM stash WHERE league = ?", (league,))

    def set_stash_many(self, league: str, holdings: dict[str, float], replace: bool = False,
                       t: int | None = None) -> None:
        """Set many stash lines in one step, for a pasted list: all of them are written, or none.

        Zero units removes a line, as in `set_stash`. With `replace`, the lines the list does not
        name are removed too, so the stash becomes exactly the list.
        """
        clean = {str(item): float(units) for item, units in holdings.items()}
        if any(not math.isfinite(units) or units < 0 for units in clean.values()):
            raise ValueError("Units must be zero or more.")
        now = int(t or self.clock())  # read before taking the lock: a rehearsal's clock takes a lock of its own
        with self._lock, self._db:
            if replace:
                self._db.execute("DELETE FROM stash WHERE league = ?", (league,))
            for item, units in clean.items():
                if units == 0:
                    self._db.execute("DELETE FROM stash WHERE league = ? AND item = ?", (league, item))
                else:
                    self._db.execute("INSERT INTO stash VALUES (?, ?, ?, ?) ON CONFLICT(league, item) DO UPDATE SET "
                                     "units = excluded.units, updated = excluded.updated", (league, item, units, now))

    # ---------------------------------------------------- your conversions
    def add_fixed(self, kind: str, inputs: list, output: str, gives: float = 1, note: str = "",
                  t: int | None = None) -> int:
        """Store a recipe ("recipe") or a vendor price ("vendor") you entered. Kept for every league.

        `inputs` is [[item id, units], ...]; a vendor price has exactly one input, the currency it asks for.
        """
        if kind not in ("recipe", "vendor"):
            raise ValueError("A conversion is a recipe or a vendor price.")
        clean = []
        for item, units in inputs or []:
            units = float(units)
            if not item or not (math.isfinite(units) and units > 0):
                raise ValueError("Every input needs an item and a positive count.")
            clean.append([str(item), units])
        gives = float(gives)
        if not clean or not output or not (math.isfinite(gives) and gives > 0):
            raise ValueError("A conversion needs at least one input, an output and a positive count.")
        if kind == "vendor" and len(clean) != 1:
            raise ValueError("A vendor price takes one currency.")
        if any(item == output for item, _ in clean):
            raise ValueError("The output cannot also be an input.")
        return self._run("INSERT INTO fixed_conversions(kind, inputs, output, gives, note, t) VALUES (?,?,?,?,?,?)",
                         (kind, json.dumps(clean), str(output), gives, str(note)[:200], int(t or self.clock())))

    def delete_fixed(self, fixed_id: int) -> None:
        self._run("DELETE FROM fixed_conversions WHERE id = ?", (int(fixed_id),))

    def fixed_conversions(self) -> list[dict]:
        """Your recipes and vendor prices, oldest first, each with its recipe key."""
        out = []
        for row in self._all("SELECT * FROM fixed_conversions ORDER BY id"):
            row["inputs"] = [(item, units) for item, units in json.loads(row["inputs"])]
            row["key"] = fixed_key(row["kind"], row["inputs"], row["output"], row["gives"])
            out.append(row)
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
        out = []
        for row in self._all("SELECT * FROM alerts WHERE league = ? ORDER BY id", (league,)):
            row["params"] = json.loads(row.get("params") or "{}")
            row["state"] = json.loads(row.get("state") or "{}")
            out.append(row)
        return out

    def add_alert(self, league: str, item: str = "", field: str = "price", op: str = "above",
                  threshold: float = 0.0, kind: str = "price", params: dict | None = None) -> int:
        """Store one alert. A price alert is item + field + op + threshold; the other kinds keep their rule in `params`.

        `params` is checked and cleaned by `alert_params`, so what is stored is always a rule the
        evaluation understands. Percentages are kept as you type them: 5 means 5%.
        """
        if kind == "price":
            if field not in ALERT_FIELDS or op not in ("above", "below"):
                raise ValueError("Unknown alert field or direction.")
            if not item:
                raise ValueError("A price alert needs an item.")
            return self._run("INSERT INTO alerts(league, item, field, op, threshold) VALUES (?,?,?,?,?)",
                             (league, item, field, op, float(threshold)))
        clean = alert_params(kind, params or {})
        return self._run("INSERT INTO alerts(league, item, field, op, threshold, kind, params) VALUES (?,?,?,?,?,?,?)",
                         (league, clean.get("item") or "", kind, "above", 0.0, kind, json.dumps(clean)))

    def delete_alert(self, alert_id: int) -> None:
        self._run("DELETE FROM alerts WHERE id = ?", (int(alert_id),))

    def evaluate_alerts(self, league: str, rows: dict[str, dict], now: int,
                        context: dict | None = None) -> list[dict]:
        """Fire alerts whose condition newly holds, and write one event per alert that fires.

        `rows` is item -> market row, as before. `context` holds what the other kinds look at, all
        plain dicts the server builds from the desk, so this file stays free of model code:
        "flips" (flip plans), "loops" (round trips), "orders" (orders with advice), "standing"
        (standing rows) and "playbook" (horizon -> playbook rows). A kind whose list is missing is
        skipped and keeps its state, so a failed model call never re-arms an alert.

        No repeats: each alert tracks the conditions that hold now by key (an item, a round trip,
        an order and its advice). A key fires when it starts to hold; it can fire again only after
        it has cleared and come back, and not within ALERT_COOLDOWN seconds of its last firing,
        so a value hovering on the threshold does not ring every hour.
        """
        context = context or {}
        fired = []
        for alert in self.alerts(league):
            if not alert["active"]:
                continue
            kind = alert.get("kind") or "price"
            if kind == "price":
                fired += self._evaluate_price(league, alert, rows, now)
                continue
            matches = _ALERT_MATCHERS[kind](alert["params"], context)
            if matches is None:
                continue
            state = alert["state"]
            was_hot = set(state.get("hot", []))
            last = {k: t for k, t in state.get("fired", {}).items() if now - t < ALERT_COOLDOWN}
            new = [m for m in matches if m["key"] not in was_hot and m["key"] not in last]
            if new:
                title = new[0]["title"]
                body = new[0]["body"]
                if len(new) > 1:
                    body += f" And {len(new) - 1} more."
                self._run("INSERT INTO events(t, league, title, body) VALUES (?,?,?,?)",
                          (now, league, _speakable(title), body))
                fired.append({"alert": alert["id"], "title": _speakable(title), "body": body})
                for m in new:
                    last[m["key"]] = now
            state = {"hot": sorted(m["key"] for m in matches), "fired": last}
            self._run("UPDATE alerts SET state = ?, hot = ? WHERE id = ?",
                      (json.dumps(state), int(bool(matches)), alert["id"]))
        return fired

    def _evaluate_price(self, league: str, alert: dict, rows: dict[str, dict], now: int) -> list[dict]:
        row = rows.get(alert["item"])
        value = row.get(alert["field"]) if row else None
        if value is None:
            return []
        crossed = value > alert["threshold"] if alert["op"] == "above" else value < alert["threshold"]
        fired = []
        if crossed and not alert["hot"]:
            title = _speakable(f"{row['name']} {ALERT_FIELDS[alert['field']].lower()} {alert['op']} {alert['threshold']:g}")
            body = f"Now {value:g}"
            self._run("INSERT INTO events(t, league, title, body) VALUES (?,?,?,?)", (now, league, title, body))
            fired.append({"alert": alert["id"], "title": title, "body": body})
        if bool(alert["hot"]) != crossed:
            self._run("UPDATE alerts SET hot = ? WHERE id = ?", (int(crossed), alert["id"]))
        return fired

    def events(self, league: str, limit: int = 30) -> list[dict]:
        return self._all("SELECT * FROM events WHERE league = ? ORDER BY id DESC LIMIT ?", (league, limit))

    def add_event(self, league: str, title: str, body: str = "") -> dict:
        """An event from outside the alert rules, such as a screen check's verdict."""
        title, body = _speakable(str(title)), " ".join(str(body).split())[:EVENT_BODY]
        if not title:
            raise ValueError("An event needs a title.")
        now = int(time.time())
        self._run("INSERT INTO events(t, league, title, body) VALUES (?,?,?,?)", (now, league, title, body))
        return self._all("SELECT * FROM events WHERE league = ? ORDER BY id DESC LIMIT 1", (league,))[0]

    def events_since(self, league: str, since: int = 0, limit: int = 50) -> list[dict]:
        """Events newer than event id `since`, oldest first: a cheap poll for notifications."""
        return self._all("SELECT * FROM events WHERE league = ? AND id > ? ORDER BY id LIMIT ?",
                         (league, int(since), int(limit)))


# ------------------------------------------------------------------ alert rules
# Once a condition has fired, it stays quiet for this long even if it clears and comes back.
ALERT_COOLDOWN = 6 * 3600
# Event titles are read aloud and shown in a notification, so they stay short.
TITLE_LENGTH = 60
EVENT_BODY = 600
ALERT_KINDS = {"price": "Price", "plan": "Flip plan", "trip": "Round trip", "order": "Your orders",
               "precedent": "League precedent"}


def _speakable(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip(" ,:") + "…"


def _short(name: str, room: int = 28) -> str:
    return name if len(name) <= room else name[: room - 1].rstrip() + "…"


def _number(params: dict, key: str, default: float | None, what: str, low: float = 0.0) -> float | None:
    value = params.get(key, default)
    if value in (None, ""):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number.") from None
    if not (math.isfinite(value) and value >= low):
        raise ValueError(f"{what} must be {low:g} or more.")
    return value


def alert_params(kind: str, params: dict) -> dict:
    """Check and clean an alert rule; errors are written for the player who typed it."""
    item = str(params.get("item") or "").strip()
    if kind == "plan":
        return {"item": item, "min_margin": _number(params, "min_margin", 2.0, "The margin"),
                "min_profit": _number(params, "min_profit", 0.0, "The profit per hour")}
    if kind == "trip":
        return {"item": item, "min_edge": _number(params, "min_edge", 3.0, "The edge"),
                "min_resting": _number(params, "min_resting", 0.0, "The value waiting in orders")}
    if kind == "order":
        return {}
    if kind == "precedent":
        if not item:
            raise ValueError("A league precedent alert needs an item.")
        mode = params.get("mode", "standing")
        direction = params.get("direction", "cheap" if mode == "standing" else "up")
        if mode == "standing":
            if direction not in ("cheap", "dear"):
                raise ValueError("Choose cheap or dear against past leagues.")
            return {"item": item, "mode": mode, "direction": direction,
                    "min_gap": _number(params, "min_gap", 20.0, "The gap to past leagues")}
        if mode == "playbook":
            if direction not in ("up", "down"):
                raise ValueError("Choose whether the price should go up or down.")
            horizon = _number(params, "horizon", 7, "The number of days", low=1)
            if horizon != int(horizon) or horizon > 60:
                raise ValueError("The number of days must be a whole number from 1 to 60.")
            return {"item": item, "mode": mode, "direction": direction, "horizon": int(horizon),
                    "min_move": _number(params, "min_move", 20.0, "The move")}
        raise ValueError("A league precedent alert looks at either today's standing or the playbook.")
    raise ValueError("Unknown alert kind. Choose price, plan, trip, order or precedent.")


def describe_alert(alert: dict, names: dict[str, str] | None = None) -> str:
    """One plain sentence for an alert's rule, for the alert list."""
    names = names or {}
    kind = alert.get("kind") or "price"
    p = alert.get("params") or {}
    name = names.get(p.get("item") or alert.get("item"), p.get("item") or alert.get("item"))
    if kind == "price":
        return f"When the {ALERT_FIELDS.get(alert['field'], alert['field']).lower()} of {name} goes {alert['op']} {alert['threshold']:g}."
    if kind == "plan":
        what = f"a flip plan for {name}" if p.get("item") else "a flip plan for any item"
        return f"When {what} shows a margin of {p['min_margin']:g}% or more and makes {p['min_profit']:g} Chaos an hour or more."
    if kind == "trip":
        start = f" starting with {name}" if p.get("item") else ""
        return (f"When a round trip{start} shows an edge of {p['min_edge']:g}% or more and its thinnest swap "
                f"has orders worth {p['min_resting']:g} Chaos or more waiting.")
    if kind == "order":
        return "When one of your open orders should be re-priced, cancelled or collected."
    if p.get("mode") == "playbook":
        return (f"When past leagues moved {name} {p['direction']} by a median of {p['min_move']:g}% or more "
                f"over the next {p['horizon']} days from today's league day.")
    return f"When {name} is {p['min_gap']:g}% or more {p['direction']}er than on the same day of past leagues."


def _match_plan(p: dict, ctx: dict) -> list[dict] | None:
    plans = ctx.get("flips")
    if plans is None:
        return None
    out = []
    for plan in plans:
        if p["item"] and plan["id"] != p["item"]:
            continue
        margin, profit = (plan.get("margin") or 0) * 100, plan.get("profit_per_hour") or 0
        if margin >= p["min_margin"] and profit >= p["min_profit"]:
            market = {"chaos": "Chaos market", "divine": "Divine market", "cross": "both markets"}.get(plan["kind"], plan["kind"])
            out.append({"key": f"{plan['id']}:{plan['kind']}",
                        "title": f"Flip {_short(plan['name'])}, {margin:.0f}% margin",
                        "body": f"Flip in the {market}: buy at {plan['buy']:g}, sell at {plan['sell']:g}, "
                                f"about {profit:.0f} Chaos an hour."})
    return out


def _match_trip(p: dict, ctx: dict) -> list[dict] | None:
    loops = ctx.get("loops")
    if loops is None:
        return None
    out = []
    for loop in loops:
        path = loop.get("path") or []
        if not path or (p["item"] and path[0]["id"] != p["item"]):
            continue
        edge, resting = (loop.get("edge") or 0) * 100, loop.get("resting_value") or 0
        if edge >= p["min_edge"] and resting >= p["min_resting"]:
            out.append({"key": loop.get("recipe_key") or ">".join(s["id"] for s in path),
                        "title": f"Round trip from {_short(path[0]['name'], 24)}, {edge:.0f}% edge",
                        "body": f"{' → '.join(s['name'] for s in path)}: {edge:.1f}% back per trip, "
                                f"{resting:.0f} Chaos of orders waiting at the thinnest swap."})
    return out


def _match_order(p: dict, ctx: dict) -> list[dict] | None:
    orders = ctx.get("orders")
    if orders is None:
        return None
    out = []
    for o in orders:
        advice = o.get("advice")
        if advice in (None, "keep"):
            continue
        name = _short(o.get("name") or o["item"])
        if advice == "collect":
            title = f"{name} filled, post the sell" if o["side"] == "buy" else f"{name} sold, collect the currency"
        elif advice == "re-price":
            title = f"Re-price your {name} {o['side']}"
        else:
            title = f"Cancel your {name} {o['side']}"
        out.append({"key": f"{o['id']}:{advice}", "title": title, "body": o.get("reason") or ""})
    return out


def _match_precedent(p: dict, ctx: dict) -> list[dict] | None:
    if p["mode"] == "standing":
        rows = ctx.get("standing")
        if rows is None:
            return None
        row = next((r for r in rows if r["id"] == p["item"]), None)
        gap = row and row.get("vs_typical")
        if gap is None:
            return []
        gap *= 100
        if (-gap if p["direction"] == "cheap" else gap) < p["min_gap"]:
            return []
        word = "cheaper" if gap < 0 else "dearer"
        return [{"key": p["item"], "title": f"{_short(row['name'])} {abs(gap):.0f}% {word} than past leagues",
                 "body": f"On this league day, past leagues paid about {row['typical']:g}; it is {row['price_now']:g} now."}]
    rows = (ctx.get("playbook") or {}).get(p["horizon"])
    if rows is None:
        return None
    row = next((r for r in rows if r["id"] == p["item"]), None)
    move = row and row.get("median")
    if move is None:
        return []
    move *= 100
    if (move if p["direction"] == "up" else -move) < p["min_move"]:
        return []
    return [{"key": p["item"],
             "title": f"{_short(row['name'])} {'rose' if move > 0 else 'fell'} {abs(move):.0f}% from here last leagues",
             "body": f"From this league day, the median move over {p['horizon']} days was {move:+.0f}% "
                     f"across {row['n']} past leagues."}]


_ALERT_MATCHERS = {"plan": _match_plan, "trip": _match_trip, "order": _match_order, "precedent": _match_precedent}

def _gold(value) -> float | None:
    """Gold as recorded: None when not given (unknown, never zero), else a non-negative number."""
    if value in (None, ""):
        return None
    value = float(value)
    if not value >= 0:
        raise ValueError("Gold cannot be negative.")
    return value


def _ratio(currency_units, item_units, units) -> tuple[int, int, float]:
    """An order's ratio and size as the exchange takes them, or a ValueError that says what is wrong."""
    try:
        cu, iu, n = float(currency_units), float(item_units), float(units)
    except (TypeError, ValueError):
        raise ValueError("An order needs a ratio and a number of items.") from None
    if not (math.isfinite(cu) and math.isfinite(iu)) or int(cu) != cu or int(iu) != iu:
        raise ValueError("The exchange only takes whole-number ratios.")
    if not cu > 0 or not iu > 0 or not (n > 0 and math.isfinite(n)):
        raise ValueError("An order needs a positive ratio and positive units.")
    return int(cu), int(iu), n


def _next_trade_id(db: sqlite3.Connection) -> int:
    """The id for a new trade: above every trade, and above every trade id a made entry remembers.

    SQLite gives the id of a deleted newest trade to the next trade. A made entry that remembered the
    deleted id would then sort after a sale recorded later at the same time, and the sale would count
    as selling units not yet held: it would realise its whole price, and the made item would still be
    in your holdings to sell again. So a remembered id is never handed out again.
    """
    return db.execute("SELECT MAX((SELECT COALESCE(MAX(id), 0) FROM trades), "
                      "(SELECT COALESCE(MAX(after_trade), 0) FROM made)) + 1").fetchone()[0]


def _newly_short(db: sqlite3.Connection, league: str, before: dict) -> set[tuple[str, int]]:
    """The entries a change leaves taking more beyond the holdings than before it (see `_history`).

    By how much, not only whether: a sale that sold 2 looted units on top of 10 bought ones is short
    already, and deleting the buy would make it 12 short and move its profit just the same.
    """
    return {entry for entry, units in _history(db, league)[2].items() if units > before.get(entry, 0.0) + 1e-9}


def _plan_merges(positions: dict[str, dict], item: str, units: float, recipes: dict[str, list[dict]],
                 max_steps: int = 3) -> list[tuple[list[tuple[str, float]], str, float]]:
    """The merges that make what a sale of `units` needs beyond the holdings, from items held.

    Returns [(inputs, output, gives)], the lowest tier first: selling 5 of an essence while holding 15
    of the tier below gives one merge of 15 into 5, and holding 45 of the tier below that gives two.
    Only whole batches are made (29 held make 9, not 9.67), and the rest stays uncovered. A batch
    that makes more than the sale needs leaves the extra in the holdings, as it would in game.

    When the holdings cannot make all of it, the plan is made again for exactly what they can make,
    so the lower tiers merge only what that needs: 15 of the lowest tier make one of the tier two
    above from 9 of them, and 6 stay held, rather than all 15 merging into 5 of which 2 sit unused.

    When the sold item cannot be made at all, nothing is merged: 3 of the lowest tier make one of the
    tier above, but not the tier two above that the sale wanted, so they stay as they are.

    The ledger does not guess. An item that more than one recipe could make from what is held is not
    made at all, and nothing is made from the item being sold or from a step further up its chain.
    """
    start = {k: v["units"] for k, v in positions.items()}

    def plan(want: float) -> tuple[list[tuple[list[tuple[str, float]], str, float]], float]:
        held = dict(start)
        steps: list[tuple[list[tuple[str, float]], str, float]] = []

        def reachable(thing: str, depth: int, path: frozenset) -> bool:
            if thing in path:
                return False
            if held.get(thing, 0.0) > 1e-9:
                return True
            return depth < max_steps and any(all(reachable(i, depth + 1, path | {thing}) for i, _ in r["inputs"])
                                             for r in recipes.get(thing, ()))

        def make(thing: str, want: float, depth: int, path: frozenset) -> None:
            if depth >= max_steps:
                return
            path = path | {thing}
            usable = [r for r in recipes.get(thing, ())
                      if r["inputs"] and all(reachable(i, depth + 1, path) for i, _ in r["inputs"])]
            if len(usable) != 1:
                return
            recipe = usable[0]
            gives = float(recipe["gives"])
            batches = math.ceil(want / gives - 1e-9)
            for i, n in recipe["inputs"]:
                missing = batches * float(n) - held.get(i, 0.0)
                if missing > 1e-9:
                    make(i, missing, depth + 1, path)
            batches = min([batches] + [math.floor(held.get(i, 0.0) / float(n) + 1e-9) for i, n in recipe["inputs"]])
            if batches <= 0:
                return
            for i, n in recipe["inputs"]:
                held[i] = held.get(i, 0.0) - batches * float(n)
            held[thing] = held.get(thing, 0.0) + batches * gives
            steps.append(([(i, batches * float(n)) for i, n in recipe["inputs"]], thing, batches * gives))

        make(item, want, 0, frozenset())
        return steps, sum(gives for _, output, gives in steps if output == item)

    want = float(units) - start.get(item, 0.0)
    if want <= 1e-9:
        return []
    steps, made = plan(want)
    for _ in range(3):  # each pass asks for no more than the one before could make
        if made <= 1e-9 or made >= want - 1e-9:
            break
        want = made
        steps, made = plan(want)
    # Merges below an item that is not made at all would only turn items held into others nobody sold.
    return steps if made > 1e-9 else []


def _refuse_short(db: sqlite3.Connection, action: str, short: set[tuple[str, int]]) -> None:
    """Refuse a change that newly leaves these entries taking more than the ledger held (see `_history`)."""
    made = [row_id for kind, row_id in short if kind == "made"]
    if made and db.execute(f"SELECT 1 FROM made WHERE sale IS NOT NULL AND id IN ({','.join('?' * len(made))})",
                           made).fetchone():
        raise ValueError(f"{action} would leave a merge the ledger recorded for a sale using more than it held at "
                         "the time. Delete that sale first, and record it again afterwards.")
    if made:
        raise ValueError(f"{action} would leave an item you made using more than the ledger held at the time. "
                         "Fix or delete that entry under \"Items you made\" first.")
    if short:
        raise ValueError(f"{action} would leave a sale selling more than the ledger held at the time. "
                         "Fix or delete that sale first.")


def _history(db: sqlite3.Connection, league: str, until: tuple[int, int] | None = None
             ) -> tuple[dict[str, dict], list[tuple[int, float]], dict[tuple[str, int], float]]:
    """One walk over a league's trades and made items, in the order they happened.

    Returns the positions (item -> {units, cost, realized, loot, last}), (time, profit realised, of
    which loot) for every sale, and the entries that took more than the ledger held at their time, as ("trade", id) or
    ("made", id) -> the units beyond the holdings, which come at zero cost. Everything that needs
    holdings reads this one walk, so the positions and the checks cannot disagree. The caller holds
    the ledger's lock. `until` = (time, trade id) stops the walk where a trade with that id and time
    would sit.

    A new entry may be short itself, but neither it nor an edit or a delete may make another entry
    short, or short by more than it was: that would change what an earlier record realised. A sale of
    loot is short by design, and an unrelated correction must not be blocked by it, so callers compare
    each entry's units before and after (`_newly_short`).

    Trades and made items live in two tables, and a rehearsal stamps everything recorded in one
    practice hour with the same time, so the time alone cannot order "bought, merged, sold". A made
    item therefore remembers the newest trade that existed when it was recorded (`after_trade`) and
    sorts right after it. That only holds while every later trade gets a higher id, which SQLite
    does not promise by itself (see `_next_trade_id`).
    """
    steps = [((tr["t"], tr["id"], 0, 0), "trade", tr)
             for tr in db.execute("SELECT * FROM trades WHERE league = ?", (league,))]
    steps += [((m["t"], m["after_trade"], 1, m["id"]), "made", m)
              for m in db.execute("SELECT * FROM made WHERE league = ?", (league,))]
    steps.sort(key=lambda step: step[0])
    if until is not None:
        steps = [step for step in steps if step[0] < (until[0], until[1], 0, 0)]
    out: dict[str, dict] = {}
    sales: list[tuple[int, float, float]] = []
    short: dict[tuple[str, int], float] = {}

    def position(item: str) -> dict:
        return out.setdefault(item, {"units": 0.0, "cost": 0.0, "realized": 0.0, "loot": 0.0, "last": 0})

    def take(item: str, units: float, entry: tuple[str, int]) -> float:
        """Remove up to `units` at average cost; returns the cost that left with them."""
        p = position(item)
        if units > p["units"] + 1e-9:
            short[entry] = short.get(entry, 0.0) + units - p["units"]
        taken = min(units, p["units"])
        cost = taken * (p["cost"] / p["units"]) if p["units"] else 0.0
        p["cost"] -= cost
        p["units"] -= taken
        return cost

    for _, kind, row in steps:
        if kind == "made":
            moved = sum(take(item, units, ("made", row["id"])) for item, units in json.loads(row["inputs"]))
            for item, _units in json.loads(row["inputs"]):
                position(item)["last"] = row["t"]
            p = position(row["output"])
            p["units"] += row["gives"]
            p["cost"] += moved
        elif row["side"] == "buy":
            p = position(row["item"])
            p["units"] += row["units"]
            p["cost"] += row["units"] * row["price"]
        else:
            # The units sold beyond the holdings cost nothing, so their price is all profit: `loot`.
            beyond = max(0.0, row["units"] - position(row["item"])["units"])
            loot = beyond * row["price"] if beyond > 1e-9 else 0.0
            gain = row["units"] * row["price"] - take(row["item"], row["units"], ("trade", row["id"]))
            p = position(row["item"])
            p["realized"] += gain
            p["loot"] += loot
            sales.append((row["t"], gain, loot))
        p["last"] = row["t"]
    return out, sales, short
