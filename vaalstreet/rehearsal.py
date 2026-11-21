"""Rehearsal: play a past league start again, one hour at a time, as practice.

You pick a past league and a starting hour. Every page then shows what the desk would
have shown at that hour, built only from data published up to it, and the clock moves
forward one hour per step: by hand, or at a chosen speed (game hours per real minute).

The time bound sits on the database connection (`db.connect(until=hour)`), so the desk,
the league analytics and backtests all see the archive as it stood then, including which
leagues had ended. A new connection is opened when the hour changes; it reads the same
Parquet files, so the server's real sync can keep running beside it.

Practice trades, orders, sessions and alerts go to their own ledger file,
`rehearsal.sqlite`, emptied when a rehearsal starts, and stamped with the rehearsal's
hour. The real ledger, `desk.sqlite`, is never opened for writing by a rehearsal.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Callable

import duckdb
import numpy as np

from vaalstreet import db
from vaalstreet.desk import Desk, _num
from vaalstreet.leagues import LeagueBook
from vaalstreet.ledger import Ledger

HOUR = 3600
DAY = 24 * HOUR
PRACTICE_FILE = "rehearsal.sqlite"
DEFAULT_BANKROLL = 1000.0  # Chaos; only used to size the "just hold Divines" comparison


def league_hours(con: duckdb.DuckDBPyConnection, league: str) -> tuple[int, int]:
    """First and last hour (unix seconds) the league traded in the archive."""
    row = con.execute("SELECT epoch(min(hour))::BIGINT, epoch(max(hour))::BIGINT FROM prices WHERE league = ?",
                      [league]).fetchall()[0]
    if row[0] is None:
        raise LookupError(f"No prices for league {league!r}")
    return int(row[0]), int(row[1])


class PracticeLedger:
    """The ledger the server uses during a rehearsal: practice activity in one file, game facts in the other.

    Trades, orders, fills, sessions, alerts and their events, and the stash are practice: they go to
    `rehearsal.sqlite`. Your settings, the Gold fees your real trades taught the desk, your own recipes and
    vendor prices, recipe checks and the watchlist are facts about the game and about you, not practice: they
    are read from the real ledger, and a change to them made during a rehearsal is written there too.
    """

    FACTS = frozenset({
        "get", "set", "settings", "costs", "gold_fees", "fixed_conversions", "add_fixed", "delete_fixed",
        "recipe_checks", "check_recipe", "delete_recipe_check", "watchlist", "watch",
    })

    def __init__(self, practice: Ledger, real: Ledger) -> None:
        self.practice, self.real = practice, real

    def __getattr__(self, name: str):
        return getattr(self.real if name in self.FACTS else self.practice, name)


class Rehearsal:
    """One practice session over a past league. Thread-safe; the server shares one between requests."""

    def __init__(self, root: Path, league: str, first: int, last: int, start: int, *, realm: str = "poe1",
                 speed: float = 0.0, bankroll: float = DEFAULT_BANKROLL, ledger_path: Path | None = None,
                 wall: Callable[[], float] = time.time) -> None:
        if not first <= start <= last:
            raise ValueError("The start must be within the league's hours on disk.")
        if not (math.isfinite(speed) and speed >= 0):
            raise ValueError("speed must be zero (step by hand) or a number of game hours per real minute.")
        if not (math.isfinite(bankroll) and bankroll > 0):
            raise ValueError("bankroll must be a positive number of Chaos.")
        self.root, self.realm, self.league = root, realm, league
        self.first, self.last, self.start = first, last, start - (start - first) % HOUR
        self.speed, self.bankroll = float(speed), float(bankroll)
        self._wall = wall
        self._lock = threading.RLock()
        self._hour, self._since = self.start, wall()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._con_hour: int | None = None
        self._desks: dict[str, Desk] = {}
        self._book: LeagueBook | None = None
        path = ledger_path or root / PRACTICE_FILE
        path.unlink(missing_ok=True)  # every rehearsal starts with an empty practice ledger
        # Stamped inside the hour just published, so fills line up with the hour's volume.
        self.ledger = Ledger(path.parent, path=path, clock=lambda: self.hour + HOUR - 1)
        self.start_bridge = self.desk().bridge

    # ------------------------------------------------------------------ clock
    @property
    def hour(self) -> int:
        """The last published hour the rehearsal can see. With a speed, it moves with the wall clock."""
        with self._lock:
            if self.speed <= 0:
                return self._hour
            ahead = int((self._wall() - self._since) / 60 * self.speed)
            return min(self.last, self._hour + ahead * HOUR)

    def step(self, hours: int = 1) -> int:
        """Move forward; never back, since practice trades are already stamped with the hours passed."""
        with self._lock:
            self._hour = min(self.last, self.hour + max(1, int(hours)) * HOUR)
            self._since = self._wall()
            return self._hour

    def set_speed(self, speed: float) -> None:
        if not (math.isfinite(speed) and speed >= 0):
            raise ValueError("speed must be zero or more game hours per real minute.")
        with self._lock:
            self._hour, self._since, self.speed = self.hour, self._wall(), float(speed)

    @property
    def done(self) -> bool:
        return self.hour >= self.last

    # ------------------------------------------------------------------- data
    def _sync_to(self, hour: int) -> None:
        if self._con_hour != hour:
            # The old connection is dropped, not closed: a request may still be reading from it.
            self._con = db.connect(self.root, self.realm, until=hour)
            self._con_hour, self._desks, self._book = hour, {}, None

    def cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            self._sync_to(self.hour)
            return self._con.cursor()

    def desk(self, league: str | None = None) -> Desk:
        league = league or self.league
        with self._lock:
            hour = self.hour
            self._sync_to(hour)
            if league not in self._desks:
                self._desks[league] = Desk.build(self._con.cursor(), league, realm=self.realm, root=self.root,
                                                 as_of=hour)
            return self._desks[league]

    def book(self) -> LeagueBook:
        with self._lock:
            self._sync_to(self.hour)
            if self._book is None:
                self._book = LeagueBook(self._con.cursor(), self.realm)
            return self._book

    def reset_data(self) -> None:
        """Drop cached connections and desks, after the archive itself changed (a backfill)."""
        with self._lock:
            self._con, self._con_hour, self._desks, self._book = None, None, {}, None

    # ---------------------------------------------------------------- reports
    def league_day(self) -> int:
        return (self.hour - self.first) // DAY

    def banner(self) -> dict:
        hour = self.hour
        return {"league": self.league, "hour": hour, "league_day": (hour - self.first) // DAY,
                "league_hour": (hour - self.first) // HOUR, "start": self.start, "last_hour": self.last,
                "speed": self.speed, "done": hour >= self.last}

    def result(self) -> dict:
        """Practice profit, marked at the current hour's fair value, against holding Divines over the same hours.

        Profit is realised profit plus what open positions are worth at fair value minus what they cost;
        a position with no fair value is counted at cost and listed in `unpriced`. The Divine line is what
        the bankroll would have made sitting in Divines from the start hour to now.
        """
        desk = self.desk()
        realized = value = cost = 0.0
        unpriced = []
        for item_id, p in self.ledger.positions(self.league).items():
            realized += p["realized"]
            cost += p["cost"]
            mark = desk.fair(item_id)
            if mark is None:
                value += p["cost"]
                unpriced.append(item_id)
            else:
                value += p["units"] * mark
        profit = realized + value - cost
        if not math.isfinite(self.start_bridge):
            # No Divine price yet at the start hour: compare from the first one the rehearsal saw.
            seen = desk.panel.bridge[(desk.panel.hours >= self.start) & np.isfinite(desk.panel.bridge)]
            if len(seen):
                self.start_bridge = float(seen[0])
        divine = self.bankroll * (desk.bridge / self.start_bridge - 1)
        hours = (self.hour - self.start) // HOUR
        return {
            "league": self.league, "start": self.start, "hour": self.hour, "hours": hours,
            "bankroll": self.bankroll, "profit": _num(profit), "realized": _num(realized),
            "unrealized": _num(value - cost), "return": _num(profit / self.bankroll, 4),
            "divine_then": _num(self.start_bridge), "divine_now": _num(desk.bridge),
            "hold_divines": _num(divine), "vs_divines": _num(profit - divine),
            "trades": len(self.ledger.trades(self.league)), "unpriced": unpriced,
        }

    def close(self) -> None:
        self.ledger.close()
        self.reset_data()
