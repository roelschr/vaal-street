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
import re

import duckdb

from vaalstreet.archive import FIRST_HOUR
from vaalstreet.ingest import ANCHORS

MIN_DAYS = 10  # a league needs this much history to be used as a precedent
DAY = 24 * 3600

# --------------------------------------------------------------- families
# Family rules (see `classify`). Names only, never lists of past leagues.
PRIVATE = re.compile(r"\(PL\d+\)$")
EVENT_NAME = re.compile(r"(?i)\bevent\b")
# Hardcore / Ruthless prefixes, longest first so "HC Ruthless X" is not read as "HC" + "Ruthless X".
VARIANTS = (
    (re.compile(r"^(?:HC|Hardcore) Ruthless (.+)$"), "hc_ruthless"),
    (re.compile(r"^(?:HC|Hardcore) (.+)$"), "hardcore"),
    (re.compile(r"^Ruthless (.+)$"), "ruthless"),
    (re.compile(r"^(?:SSF|Solo) (.+)$"), "ssf"),
)
PERMANENT = {"Standard": "standard", "Hardcore": "standard_hardcore", "Ruthless": "standard_ruthless",
             "Hardcore Ruthless": "standard_hc_ruthless", "HC Ruthless": "standard_hc_ruthless"}
EVENT_AFTER_DAYS = 7  # an event starts at least this long after the league it runs alongside...
EVENT_OVERLAP_DAYS = 3  # ...and that league is still trading this long after the event began
SOFTCORE = ("challenge", "event")  # the families the league page lines up

# A cross-league gap is "structural" when it lasted this many days, kept its sign this often, and is this big.
STRUCTURAL_DAYS = 3
STRUCTURAL_SIGN = 0.8
STRUCTURAL_SIZE = 0.03
STRUCTURAL_HOURS = 12  # ...on at least this many hours that both leagues traded

LAUNCH_HOURS = 72
LAUNCH_CHECKS = (6, 24, 72)  # league hours at which the launch playbook reports a move
FIRST_MOVE = 0.05  # a launch price "moves first" when it leaves its opening price by this much


def classify(rows: list[dict], end: int | None = None) -> dict[str, tuple[str, str]]:
    """League name -> (family, base league), from the names and the dates each league traded.

    Rules, in order:

    1. A name ending in "(PLnnnn)" is a private league: family "private".
    2. The permanent leagues: "Standard" is "standard"; "Hardcore", "Ruthless" and "Hardcore Ruthless"
       are "standard_hardcore", "standard_ruthless" and "standard_hc_ruthless".
    3. A name starting with "Hardcore "/"HC ", "Ruthless ", "HC Ruthless "/"Hardcore Ruthless ", or
       "SSF "/"Solo " is a variant of the league named by the rest (its base). It takes its base's
       family with "_hardcore", "_ruthless", "_hc_ruthless" or "_ssf" added; a base not in the archive
       counts as a challenge league.
    4. Every other league is a base league: a challenge league, unless it is an event. A base league is
       an event when its name says "Event", or when it runs inside another base league's life: it
       started at least EVENT_AFTER_DAYS after that league did, the older league kept trading at least
       EVENT_OVERLAP_DAYS more, and it outlasted the newcomer (or both are still live). GGG starts a
       challenge league only when the previous one ends, while events (Phrecia, the Ancestor event)
       run for a few weeks next to a live challenge league. So the rule needs no list of past events,
       and a young live league is called an event only if it really started mid-league.

    Duration alone is not used: a live league's length is only a lower bound, and so is the length of
    a league already running when the archive starts. `end` is the archive's last hour (epoch seconds),
    used to tell a live league from an ended one.
    """
    end = max((r["last_hour"] for r in rows), default=0) if end is None else end
    out: dict[str, tuple[str, str]] = {}
    variants = []
    for r in rows:
        name = r["league"]
        if PRIVATE.search(name):
            out[name] = ("private", name)
        elif name in PERMANENT:
            out[name] = (PERMANENT[name], "Standard")
        else:
            for pattern, suffix in VARIANTS:
                m = pattern.match(name)
                if m:
                    variants.append((name, m.group(1), suffix))
                    break
    bases = sorted((r for r in rows if r["league"] not in out and all(v[0] != r["league"] for v in variants)),
                   key=lambda r: r["first_hour"])
    challenges: list[dict] = []
    for r in bases:
        live = r["last_hour"] >= end - DAY
        inside = any(
            r["first_hour"] - a["first_hour"] >= EVENT_AFTER_DAYS * DAY
            and a["last_hour"] - r["first_hour"] >= EVENT_OVERLAP_DAYS * DAY
            and (a["last_hour"] >= r["last_hour"] - DAY or (live and a["last_hour"] >= end - DAY))
            for a in challenges
        )
        if EVENT_NAME.search(r["league"]) or inside:
            out[r["league"]] = ("event", r["league"])
        else:
            out[r["league"]] = ("challenge", r["league"])
            challenges.append(r)
    for name, base, suffix in variants:
        family = out.get(base, ("challenge", base))[0]
        out[name] = (f"{family}_{suffix}", base)
    return out


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
                WHERE l.family IN ('challenge', 'event')
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
        """Softcore challenge and event leagues, newest first, with their size and family."""
        rows = self._rows(
            """
            SELECT l.league, l.family, epoch(l.first_hour)::BIGINT AS first_hour, epoch(l.last_hour)::BIGINT AS last_hour,
                   date_diff('hour', l.first_hour, l.last_hour) // 24 AS days, l.value
            FROM leagues l WHERE l.family IN ('challenge', 'event') AND l.value > 0
            ORDER BY l.first_hour DESC
            """
        )
        archive = self._rows("SELECT epoch(min(hour))::BIGINT AS t0, epoch(max(hour))::BIGINT AS t1 FROM prices")[0]
        archive_start = archive["t0"]
        for r in rows:
            # Events and side leagues are tiny next to a real challenge league: size is judged within the family.
            top = max(x["value"] for x in rows if x["family"] == r["family"]) or 1
            r["ended"] = bool(r["last_hour"] < archive["t1"] - DAY)
            # A league already running at the first archived hour has an unknown start: its "day 0" is not
            # day 0, so it cannot be lined up with the others until the archive reaches back further.
            r["truncated"] = bool(r["first_hour"] <= archive_start and archive_start > FIRST_HOUR[self.realm])
            r["main"] = bool(r["value"] >= 0.02 * top and r["days"] >= MIN_DAYS and not r["truncated"])
            r["value"] = _num(r["value"])
        return rows

    def precedents(self, current: str) -> list[str]:
        """Earlier leagues of the same family, ended and with enough history to learn from.

        A challenge league learns only from past challenge leagues and an event only from past events.
        Only leagues that started before `current` and have since ended count: never `current` itself,
        and never one still running, whose later days are not known yet.
        """
        rows = self.leagues()
        me = next((r for r in rows if r["league"] == current), None)
        family = me["family"] if me else "challenge"
        start = me["first_hour"] if me else None
        return [
            r["league"] for r in rows
            if r["league"] != current and r["main"] and r["ended"] and r["family"] == family
            and (start is None or r["first_hour"] < start)
        ]

    def family_of(self, league: str) -> str:
        row = self._rows("SELECT family FROM leagues WHERE league = $l", {"l": league})
        return row[0]["family"] if row else "challenge"

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

    # --------------------------------------------------------------- launch
    def launch(self, current: str | None = None, *, family: str | None = None, unit: str = "chaos",
               min_leagues: int = 1, limit: int = 300) -> dict:
        """The first 72 hours of each past league in the family, item by item.

        Per league and item:
            wake       the league hour of its first trade
            moves      its price at hours 6, 24 and 72 (the volume-weighted average of the three hours up to
                       each) against its first-day value (the volume-weighted average of league day 0)
            first      what it did first: "rise" or "fall" once it left its opening price (its first three
                       traded hours) by FIRST_MOVE, or "flat" if it never did within 72 hours
            path       its price every hour against its first-day value
        Across leagues: the median wake-up hour, the median move at each check, the median hourly path,
        and the share of leagues whose first move agrees with the most common one.

        Precedents are those of `current` (same family, started earlier, ended), or with no `current`
        every ended league of `family`. Leagues whose start is not on disk are never used. With fewer than
        two, `shortfall` says so in words.
        """
        past, family = self._past(current, family)
        out = {"league": current, "family": family, "unit": unit, "hours": LAUNCH_HOURS, "checks": list(LAUNCH_CHECKS),
               "precedents": past, "shortfall": None, "items": []}
        if len(past) < 2:
            out["shortfall"] = (
                f"Only {len(past)} past {family.replace('_', ' ')} league{'' if len(past) == 1 else 's'} "
                f"{'has its' if len(past) == 1 else 'have their'} first {LAUNCH_HOURS} hours on disk; at least 2 are needed "
                "to say what usually happens at a league start. Download the full history to add more."
            )
        if not past:
            return out
        column = "p.price / d.price" if unit == "divine" else "p.price"
        rows = self._rows(
            f"""
            SELECT p.league, p.item, date_diff('hour', l.first_hour, p.hour) AS h, {column} AS price, p.volume
            FROM prices p JOIN leagues l USING (league)
            LEFT JOIN prices d ON d.league = p.league AND d.hour = p.hour AND d.item = $bridge
            WHERE p.league IN $past AND p.hour <= l.first_hour + INTERVAL {LAUNCH_HOURS} HOUR
              AND p.volume > 0 AND p.price > 0
            ORDER BY p.league, p.item, h
            """,
            {"past": past, "bridge": self.bridge},
        )
        series: dict[tuple[str, str], list[tuple[int, float, float]]] = {}
        for r in rows:
            if r["price"] and r["price"] > 0:
                series.setdefault((r["item"], r["league"]), []).append((int(r["h"]), float(r["price"]), float(r["volume"])))
        names = {r["item"]: r for r in self._rows("SELECT item, name, category FROM items")}

        def vwap(points):
            vol = sum(v for _, _, v in points)
            return sum(p * v for _, p, v in points) / vol if vol else None

        per_item: dict[str, list[dict]] = {}
        for (item, league), points in series.items():
            day0 = vwap([x for x in points if x[0] < 24])
            if not day0:
                continue
            opening = vwap(points[:3])
            first = "flat"
            for _, price, _ in points[3:]:
                if abs(price / opening - 1) >= FIRST_MOVE:
                    first = "rise" if price > opening else "fall"
                    break
            moves = {}
            for check in LAUNCH_CHECKS:
                at = vwap([x for x in points if check - 2 <= x[0] <= check])
                moves[str(check)] = None if at is None else at / day0 - 1
            path = [None] * (LAUNCH_HOURS + 1)
            for h, price, _ in points:
                path[h] = price / day0 - 1
            per_item.setdefault(item, []).append({
                "league": league, "wake": points[0][0], "first": first, "moves": moves, "path": path,
                "day0_value": sum(p * v for h, p, v in points if h < 24)})

        def median(xs):
            xs = sorted(x for x in xs if x is not None)
            if not xs:
                return None
            m = len(xs) // 2
            return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2

        for item, runs in per_item.items():
            if len(runs) < min_leagues:
                continue
            firsts = [r["first"] for r in runs]
            common = max(("rise", "fall", "flat"), key=firsts.count)
            meta = names.get(item, {})
            out["items"].append({
                "id": item, "name": meta.get("name", item), "category": meta.get("category"), "n": len(runs),
                "wake": median([r["wake"] for r in runs]), "first": common,
                "agree": _num(firsts.count(common) / len(runs), 3),
                "moves": {k: _num(median([r["moves"][k] for r in runs]), 4) for k in map(str, LAUNCH_CHECKS)},
                "path": [_num(median([r["path"][h] for r in runs]), 4) for h in range(LAUNCH_HOURS + 1)],
                "by_league": [{"league": r["league"], "wake": r["wake"], "first": r["first"],
                               "moves": {k: _num(v, 4) for k, v in r["moves"].items()}} for r in runs],
                "day0_value": _num(median([r["day0_value"] for r in runs])),
            })
        out["items"].sort(key=lambda r: (-r["n"], -(r["day0_value"] or 0)))
        out["items"] = out["items"][:limit]
        return out

    # --------------------------------------------------------- divine timing
    def _past(self, current: str | None, family: str | None) -> tuple[list[str], str]:
        if current is not None:
            return self.precedents(current), self.family_of(current)
        family = family or "challenge"
        return [r["league"] for r in self.leagues() if r["family"] == family and r["main"] and r["ended"]], family

    def divine_timing(self, current: str | None = None, *, family: str | None = None, max_day: int = 30,
                      window: int = 3) -> dict:
        """On which league days Chaos bought the most Divines in past leagues, and how consistently.

        For each past league in the family, the day (up to `max_day`, or as far as every league reaches)
        on which the Divine's daily average price in Chaos was lowest. The advice names the `window`-day
        stretch that holds the most of those days, as "in k of n past leagues". `curve` gives, per league
        day, how much dearer the Divine was than on its cheapest day (median across leagues).
        Only ended leagues before `current` count; today's league day is used only to say where it stands.
        """
        past, family = self._past(current, family)
        label = family.replace("_", " ")
        out = {"league": current, "family": family, "precedents": past, "n": len(past), "by_league": [],
               "window": None, "curve": [], "advice": None, "day": self.day_of(current) if current else None}
        rows = self._rows(
            "SELECT league, day, price FROM league_daily WHERE item = $bridge AND league IN $past AND day <= $max_day "
            "AND price > 0 ORDER BY league, day",
            {"bridge": self.bridge, "past": past or [""], "max_day": max_day},
        )
        curves: dict[str, dict[int, float]] = {}
        for r in rows:
            curves.setdefault(r["league"], {})[int(r["day"])] = float(r["price"])
        if not curves:
            out["advice"] = f"No past {label} league is on disk to time the Divine with."
            return out
        last = min(max(c) for c in curves.values())  # judge every league on the same stretch of days
        for league in past:
            c = {d: p for d, p in curves.get(league, {}).items() if d <= last}
            if c:
                cheapest = min(c, key=c.get)
                out["by_league"].append({"league": league, "day": cheapest, "price": _num(c[cheapest])})
        days = [r["day"] for r in out["by_league"]]
        n = len(days)
        best = max(range(0, last + 1), key=lambda a: (sum(a <= d < a + window for d in days), -a))
        inside = sorted(d for d in days if best <= d < best + window)
        lo, hi = inside[0], inside[-1]
        out["n"] = n
        out["window"] = {"from": lo, "to": hi, "leagues": len(inside), "days_checked": last}
        when = f"on day {lo}" if lo == hi else f"between day {lo} and day {hi}"
        if n == 1:
            out["advice"] = (f"In the one past {label} league on disk ({out['by_league'][0]['league']}), the Divine was "
                             f"cheapest in Chaos {when} of the first {last + 1} days. One league is not a pattern.")
        else:
            out["advice"] = (f"In {len(inside)} of {n} past {label} leagues the Divine was cheapest in Chaos {when} "
                             f"(looking at the first {last + 1} days).")
        if current is not None:
            today = out["day"]
            if today < lo:
                out["advice"] += f" Today is day {today}, {lo - today} day{'s' if lo - today > 1 else ''} before that."
            elif today <= hi:
                out["advice"] += f" Today is day {today}, inside that stretch."
            else:
                out["advice"] += f" Today is day {today}, past that stretch."
        for d in range(last + 1):
            rel = [c[d] / min(v for k, v in c.items() if k <= last) - 1 for c in curves.values() if d in c]
            rel.sort()
            if rel:
                m = len(rel) // 2
                out["curve"].append({"day": d, "dearer": _num(rel[m] if len(rel) % 2 else (rel[m - 1] + rel[m]) / 2, 4),
                                     "n": len(rel)})
        return out

    # --------------------------------------------------------- cross-league
    def cross_league(self, current: str, *, days: int = 7, min_value: float = 50.0) -> dict:
        """The same item, the same hour, in this league's Hardcore, Ruthless and Standard markets.

        Each league has its own Chaos, worth a different number of Divines, so a raw Chaos gap mostly
        repeats the gap in the Divine rate. Items are therefore compared in each league's own Divines:
        Chaos-market prices divided by that hour's Divine price in the same league, and Divine-market
        prices as they trade. The Chaos and Divine markets are compared separately, never mixed. The
        Divine rate itself gets a row of its own.

        Per item, market and other league: the hourly gap (other / this - 1) on hours both traded at
        least `min_value` Chaos, its median per day over the last `days` days, and then:
            gap        the median of those daily gaps
            same_sign  the share of days whose gap had the sign of `gap`
            spread     how much the daily gap wanders (standard deviation of its log)
        A gap is "structural" when it held on at least STRUCTURAL_DAYS days (and STRUCTURAL_HOURS hours), kept its sign on at least
        STRUCTURAL_SIGN of them, is at least STRUCTURAL_SIZE, and wanders less than its own size.
        It is "none" when it stayed within STRUCTURAL_SIZE of zero, and "noise" otherwise: it flips or closes. Items cannot move between leagues, so a gap is not
        a trade; it says which differences are real when you read prices from the other leagues.
        """
        others = self._rows(
            """
            SELECT league, family FROM leagues
            WHERE (base = $l AND league <> $l AND family NOT LIKE '%_ssf') OR family = 'standard'
            ORDER BY family
            """,
            {"l": current},
        )
        end = self._rows("SELECT epoch(last_hour)::BIGINT AS t, epoch(first_hour)::BIGINT AS f FROM leagues "
                         "WHERE league = $l", {"l": current})
        if not end:
            raise LookupError(f"Unknown league {current!r}")
        out = {"league": current, "days": days, "others": [r["league"] for r in others], "items": [],
               "divine": []}
        if not others:
            return out
        rows = self._rows(
            """
            WITH win AS (
                SELECT p.*, d.price AS div
                FROM prices p JOIN prices d ON d.league = p.league AND d.hour = p.hour AND d.item = $bridge
                WHERE p.league IN $leagues AND p.hour > to_timestamp($end) - to_days($days)
                  AND p.hour <= to_timestamp($end)
            ), sides AS (
                SELECT league, hour, item, 'chaos' AS market,
                       CASE WHEN item = $bridge THEN r_price ELSE r_price / div END AS p, r_volume * r_price AS v
                FROM win WHERE r_price > 0
                UNION ALL
                SELECT league, hour, item, 'divine', b_price / div, b_volume * b_price
                FROM win WHERE b_price > 0 AND item <> $bridge
            ), pairs AS (
                SELECT o.league AS other, h.item, h.market,
                       date_diff('hour', to_timestamp($start), h.hour) // 24 AS day, ln(o.p / h.p) AS g
                FROM sides h JOIN sides o ON o.hour = h.hour AND o.item = h.item AND o.market = h.market
                WHERE h.league = $l AND o.league <> $l AND h.v >= $min_value AND o.v >= $min_value
            ), daily AS (
                SELECT other, item, market, day, median(g) AS g, count(*) AS hours
                FROM pairs GROUP BY ALL
            )
            SELECT d.other, d.item, i.name, d.market, count(*) AS n_days, sum(hours) AS hours,
                   median(g) AS g, stddev_samp(g) AS spread,
                   avg(CASE WHEN sign(g) = sign(m.mg) THEN 1.0 ELSE 0.0 END) AS same_sign
            FROM daily d JOIN items i USING (item)
            JOIN (SELECT other, item, market, median(g) AS mg FROM daily GROUP BY ALL) m USING (other, item, market)
            GROUP BY d.other, d.item, i.name, d.market
            """,
            {"bridge": self.bridge, "leagues": [current] + out["others"], "end": end[0]["t"], "start": end[0]["f"],
             "days": days, "l": current, "min_value": min_value},
        )
        family = {r["league"]: r["family"] for r in others}
        for r in rows:
            g, spread = r["g"], r["spread"] or 0.0
            structural = (r["n_days"] >= STRUCTURAL_DAYS and r["hours"] >= STRUCTURAL_HOURS and r["same_sign"] >= STRUCTURAL_SIGN
                          and abs(g) >= math.log1p(STRUCTURAL_SIZE) and spread < abs(g))
            quiet = r["n_days"] >= STRUCTURAL_DAYS and abs(g) < math.log1p(STRUCTURAL_SIZE) and spread < STRUCTURAL_SIZE
            row = {"other": r["other"], "family": family[r["other"]], "id": r["item"], "name": r["name"],
                   "market": r["market"], "gap": _num(math.expm1(g), 4), "same_sign": _num(r["same_sign"], 3),
                   "spread": _num(spread, 3), "days": int(r["n_days"]), "hours": int(r["hours"]),
                   "verdict": "structural" if structural else "none" if quiet else "noise"}
            (out["divine"] if r["item"] == self.bridge else out["items"]).append(row)
        out["items"].sort(key=lambda x: (["structural", "noise", "none"].index(x["verdict"]), -abs(x["gap"] or 0)))
        return out
