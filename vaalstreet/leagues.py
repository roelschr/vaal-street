"""League-start analytics: what the same day of earlier leagues says about this one.

Every challenge league replays a similar economy - a scramble in the first days,
Chaos losing value against Divines for weeks, items peaking and decaying on their
own schedules. `LeagueBook` lines all leagues up on "days since the first trade"
and answers three questions:

    curve(item)       how an item was priced on each league day, per league
    playbook(...)     what happened next, in past leagues, to items bought on the
                      current league day - median move, range, and how often it was up
    standing(...)     how today's prices compare with past leagues at the same day

Prices can be read in Chaos or in Divine. Chaos inflates through a league, so
"in Divine" is the honest unit for anything held longer than a few days.
A handful of leagues is a small sample: every figure comes with its `n`.
"""

from __future__ import annotations

import math

import duckdb

from vaalstreet.archive import FIRST_HOUR
from vaalstreet.ingest import ANCHORS

MIN_DAYS = 10  # a league needs this much history to be used as a precedent


def _num(x, digits: int = 5):
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


class LeagueBook:
    def __init__(self, con: duckdb.DuckDBPyConnection, realm: str = "poe1") -> None:
        self.con = con
        self.realm = realm
        self.ref, self.bridge = ANCHORS[realm]
        # Daily VWAP per league and item; the bridge (Divine) itself supplies the conversion rate.
        con.execute(
            """
            CREATE OR REPLACE TABLE league_daily AS
            WITH base AS (
                SELECT p.league, p.item, date_diff('hour', l.first_hour, p.hour) // 24 AS day,
                       p.value, p.volume
                FROM prices p JOIN leagues l USING (league)
                WHERE l.kind = 'challenge'
            ), daily AS (
                SELECT league, item, day, sum(value) / nullif(sum(volume), 0) AS price, sum(value) AS value
                FROM base GROUP BY league, item, day
            )
            SELECT d.*, b.price AS bridge, d.price / b.price AS price_div
            FROM daily d
            LEFT JOIN daily b ON b.league = d.league AND b.day = d.day AND b.item = $bridge
            WHERE d.price IS NOT NULL
            """,
            {"bridge": self.bridge},
        )

    def _rows(self, sql: str, params: dict | None = None) -> list[dict]:
        cur = self.con.cursor().execute(sql, params or {})  # own cursor: safe from any thread
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------- leagues
    def leagues(self) -> list[dict]:
        """Challenge leagues, newest first, with their size."""
        rows = self._rows(
            """
            SELECT l.league, epoch(l.first_hour)::BIGINT AS first_hour, epoch(l.last_hour)::BIGINT AS last_hour,
                   date_diff('hour', l.first_hour, l.last_hour) // 24 AS days, l.value
            FROM leagues l WHERE l.kind = 'challenge' AND l.value > 0
            ORDER BY l.first_hour DESC
            """
        )
        top = max((r["value"] for r in rows), default=0) or 1
        archive_start = self._rows("SELECT epoch(min(hour))::BIGINT AS t FROM prices")[0]["t"]
        for r in rows:
            # A league already running at the first archived hour has an unknown start: its "day 0" is not
            # day 0, so it cannot be lined up with the others until the archive reaches back further.
            r["truncated"] = bool(r["first_hour"] <= archive_start and archive_start > FIRST_HOUR[self.realm])
            # Events and side leagues are tiny next to a real challenge league.
            r["main"] = bool(r["value"] >= 0.02 * top and r["days"] >= MIN_DAYS and not r["truncated"])
            r["value"] = _num(r["value"])
        return rows

    def precedents(self, current: str) -> list[str]:
        """Earlier leagues with enough history to learn from."""
        rows = self.leagues()
        start = next((r["first_hour"] for r in rows if r["league"] == current), None)
        return [
            r["league"] for r in rows
            if r["league"] != current and r["main"] and (start is None or r["first_hour"] < start)
        ]

    def day_of(self, league: str) -> int:
        row = self._rows(
            "SELECT date_diff('hour', first_hour, last_hour) // 24 AS day FROM leagues WHERE league = $l", {"l": league}
        )
        if not row:
            raise LookupError(f"Unknown league {league!r}")
        return int(row[0]["day"])

    # --------------------------------------------------------------- curves
    def curve(self, item: str, leagues: list[str] | None = None, max_day: int = 120) -> dict:
        """Daily price of one item (id or name) in every league: {league: [{day, price, price_div, value}]}."""
        rows = self._rows(
            """
            SELECT d.league, d.day, d.price, d.price_div, d.value
            FROM league_daily d JOIN items i USING (item)
            WHERE (d.item = $item OR lower(i.name) = lower($item)) AND d.day <= $max_day
            ORDER BY d.league, d.day
            """,
            {"item": item, "max_day": max_day},
        )
        out: dict[str, list] = {}
        aligned = {r["league"] for r in self.leagues() if not r["truncated"]}
        for r in rows:
            if (leagues and r["league"] not in leagues) or r["league"] not in aligned:
                continue
            out.setdefault(r["league"], []).append(
                {"day": int(r["day"]), "price": _num(r["price"]), "price_div": _num(r["price_div"]),
                 "value": _num(r["value"])}
            )
        return out

    def divine_curve(self, max_day: int = 120) -> dict:
        return self.curve(self.bridge, max_day=max_day)

    # ------------------------------------------------------------- playbook
    def playbook(
        self,
        current: str,
        horizon: int = 7,
        *,
        day: int | None = None,
        unit: str = "divine",
        min_leagues: int = 2,
        min_value: float = 2000.0,
    ) -> dict:
        """What past leagues did over the next `horizon` days to items bought on league day `day`.

        Entry is the average of days [day-1, day]; exit the average of [day+horizon-1, day+horizon+1],
        so a single odd day does not decide the outcome. `unit` is "divine" or "chaos".
        """
        day = self.day_of(current) if day is None else day
        past = self.precedents(current)
        min_leagues = max(1, min(min_leagues, len(past)))
        column = "price_div" if unit == "divine" else "price"
        rows = self._rows(
            f"""
            WITH entry AS (
                SELECT league, item, exp(avg(ln({column}))) AS p0, sum(value) AS value
                FROM league_daily WHERE day BETWEEN $d0 - 1 AND $d0 AND {column} > 0 AND league IN $past
                GROUP BY league, item
            ), exit_ AS (
                SELECT league, item, exp(avg(ln({column}))) AS p1, sum(value) AS value
                FROM league_daily WHERE day BETWEEN $d1 - 1 AND $d1 + 1 AND {column} > 0 AND league IN $past
                GROUP BY league, item
            ), moves AS (
                SELECT e.item, e.league, x.p1 / e.p0 - 1 AS move, e.value
                FROM entry e JOIN exit_ x USING (league, item)
                WHERE e.value >= $min_value AND x.value >= $min_value
            ), now AS (
                SELECT item, exp(avg(ln({column}))) AS price_now, exp(avg(ln(price))) AS chaos_now, sum(value) AS value_now
                FROM league_daily WHERE league = $current AND day BETWEEN $d0 - 1 AND $d0 AND {column} > 0
                GROUP BY item
            )
            SELECT m.item, i.name, i.category,
                   count(*) AS n, median(m.move) AS median, min(m.move) AS worst, max(m.move) AS best,
                   avg(CASE WHEN m.move > 0 THEN 1.0 ELSE 0.0 END) AS up_share,
                   list(struct_pack(league := m.league, move := m.move) ORDER BY m.league) AS by_league,
                   any_value(n.price_now) AS price_now, any_value(n.chaos_now) AS chaos_now,
                   any_value(n.value_now) AS value_now
            FROM moves m JOIN items i USING (item) LEFT JOIN now n USING (item)
            GROUP BY m.item, i.name, i.category
            HAVING count(*) >= $min_leagues
            """,
            {"d0": day, "d1": day + horizon, "past": past, "current": current, "min_value": min_value,
             "min_leagues": min_leagues},
        )
        items = []
        for r in rows:
            if r["item"] == self.bridge and unit == "divine":
                continue
            # Consistency first: an item that moved the same way every time ranks above a bigger but mixed record.
            agreement = max(r["up_share"], 1 - r["up_share"])
            items.append({
                "id": r["item"], "name": r["name"], "category": r["category"], "n": int(r["n"]),
                "median": _num(r["median"], 4), "worst": _num(r["worst"], 4), "best": _num(r["best"], 4),
                "up_share": _num(r["up_share"], 3),
                "by_league": [{"league": m["league"], "move": _num(m["move"], 4)} for m in r["by_league"]],
                "price_now": _num(r["price_now"]), "chaos_now": _num(r["chaos_now"]),
                "value_now": _num(r["value_now"]),
                "traded_now": r["price_now"] is not None,
                "score": _num(r["median"] * (2 * agreement - 1) * math.sqrt(r["n"]), 4),
            })
        items.sort(key=lambda row: -(row["score"] or 0))
        return {"league": current, "day": day, "horizon": horizon, "unit": unit, "precedents": past, "items": items}

    def divine_outlook(self, current: str, horizons: tuple[int, ...] = (3, 7, 14, 30)) -> list[dict]:
        """What the Divine price (in Chaos) did from today's league day in past leagues, per horizon."""
        out = []
        for horizon in horizons:
            pb = self.playbook(current, horizon, unit="chaos", min_leagues=1, min_value=0)
            row = next((r for r in pb["items"] if r["id"] == self.bridge), None)
            if row:
                out.append({"horizon": horizon, **{k: row[k] for k in ("n", "median", "worst", "best", "by_league")}})
        return out

    # ------------------------------------------------------------- standing
    def standing(self, current: str, *, unit: str = "divine", min_leagues: int = 2, min_value: float = 2000.0) -> dict:
        """Today's price of each item against the same league day in past leagues."""
        day = self.day_of(current)
        past = self.precedents(current)
        min_leagues = max(1, min(min_leagues, len(past)))
        column = "price_div" if unit == "divine" else "price"
        rows = self._rows(
            f"""
            WITH now AS (
                SELECT item, exp(avg(ln({column}))) AS price_now, sum(value) AS value_now
                FROM league_daily WHERE league = $current AND day BETWEEN $d - 1 AND $d AND {column} > 0
                GROUP BY item
            ), then_ AS (
                SELECT league, item, exp(avg(ln({column}))) AS p
                FROM league_daily WHERE league IN $past AND day BETWEEN $d - 1 AND $d AND {column} > 0
                GROUP BY league, item HAVING sum(value) >= $min_value
            )
            SELECT n.item, i.name, i.category, n.price_now, n.value_now,
                   count(*) AS n, median(t.p) AS typical, min(t.p) AS low, max(t.p) AS high
            FROM now n JOIN then_ t USING (item) JOIN items i USING (item)
            WHERE n.value_now >= $min_value
            GROUP BY n.item, i.name, i.category, n.price_now, n.value_now
            HAVING count(*) >= $min_leagues
            """,
            {"current": current, "d": day, "past": past, "min_leagues": min_leagues, "min_value": min_value},
        )
        items = [
            {"id": r["item"], "name": r["name"], "category": r["category"], "n": int(r["n"]),
             "price_now": _num(r["price_now"]), "typical": _num(r["typical"]),
             "low": _num(r["low"]), "high": _num(r["high"]),
             "vs_typical": _num(r["price_now"] / r["typical"] - 1, 4), "value_now": _num(r["value_now"])}
            for r in rows if not (r["item"] == self.bridge and unit == "divine")
        ]
        items.sort(key=lambda row: row["vs_typical"] or 0)
        return {"league": current, "day": day, "unit": unit, "precedents": past, "items": items}
