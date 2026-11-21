"""The league report: one static "state of the economy" page per league day.

`vaalstreet report` writes a self-contained HTML file that a player can read
without the desk, and that the user may publish wherever they like. The page is
built for sharing, so it holds only market data: no ledger, stash, watchlist or
alerts, no item icons or other GGG assets, no fonts or scripts from elsewhere.
Its only links are the project and the data credits. The command writes a local
folder and never uploads anything.

Causality. A report for league day d must not know what happened after day d,
or a page written later would look wiser than one written on the day. Rather
than pass "as of" to every model, the report opens the archive through
`db.connect(until=...)`, which hides every hour after the last hour of day d.
League ends, families, precedents, fair value and the playbook then see the
archive exactly as it stood that evening, and the page for day d is the same
byte for byte whenever it is written. The page carries no clock time for the
same reason: only the last hour of data it used.
"""

from __future__ import annotations

import datetime as dt
import html
import math
import re
import warnings
from pathlib import Path

from vaalstreet.db import connect
from vaalstreet.desk import Desk
from vaalstreet.ingest import ANCHORS
from vaalstreet.leagues import LeagueBook

HOUR = 3600
PROJECT_URL = "https://github.com/roelschr/vaal-street"
API_URL = "https://www.pathofexile.com/developer/docs/reference"
REPOE_URL = "https://repoe-fork.github.io/"

MOVER_POOL = 50  # movers are picked among this many most traded items, so a thin item cannot top the list
MOVER_MIN = 0.01  # a 24-hour move smaller than this is not news
ROWS = 8
TRADED_ROWS = 12
GAP_ROWS = 6
PLAYBOOK_HORIZON = 7
PLAYBOOK_AGREE = 0.75  # a playbook row is shown when at least this share of past leagues moved the same way


# ------------------------------------------------------------------ data
def slug(league: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", league).strip("-").lower() or "league"


def league_days(root: Path | None, league: str, realm: str = "poe1") -> tuple[int, int]:
    """(first hour, last league day on disk) of a league."""
    con = connect(root, realm)
    try:
        row = con.execute("SELECT epoch(first_hour)::BIGINT, date_diff('hour', first_hour, last_hour) // 24 "
                          "FROM leagues WHERE league = ?", [league]).fetchall()
    finally:
        con.close()
    if not row:
        raise LookupError(f"Unknown league {league!r}")
    return int(row[0][0]), int(row[0][1])


def build(root: Path | None, league: str, day: int | None = None, realm: str = "poe1") -> dict:
    """Everything one day's page shows, from data up to the last hour of that league day only."""
    first, last_day = league_days(root, league, realm)
    day = last_day if day is None else day
    if not 0 <= day <= last_day:
        raise ValueError(f"{league} has league days 0 to {last_day} on disk, not {day}.")
    cutoff = first + (24 * day + 23) * HOUR
    con = connect(root, realm, until=cutoff)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # empty slices of thin items, already handled as NaN
            return _build(con, root, league, day, realm)
    finally:
        con.close()


def _build(con, root, league: str, day: int, realm: str) -> dict:
    book = LeagueBook(con, realm)
    desk = Desk.build(con, league, realm=realm, root=root)
    me = next((r for r in book.leagues() if r["league"] == league), None)
    summary = desk.summary()
    rows = desk.market()
    out = {
        "league": league, "day": day, "as_of": summary["as_of"], "realm": realm,
        "truncated": bool(me and me["truncated"]),
        "summary": summary,
        "divine": _divine(con, book, league, day, desk),
    }
    by_id = {r["id"]: r for r in rows}
    # The Divine has a section of its own, priced from its raw hourly average rather than fair value.
    traded = sorted((r for r in rows if r["value_24h"] and r["id"] != ANCHORS[realm][1]),
                    key=lambda r: (-r["value_24h"], r["id"]))
    pool = [r for r in traded[:MOVER_POOL] if r["change_24h"] is not None and abs(r["change_24h"]) >= MOVER_MIN]
    out["risers"] = sorted((r for r in pool if r["change_24h"] > 0), key=lambda r: (-r["change_24h"], r["id"]))[:ROWS]
    out["fallers"] = sorted((r for r in pool if r["change_24h"] < 0), key=lambda r: (r["change_24h"], r["id"]))[:ROWS]
    out["traded"] = traded[:TRADED_ROWS]
    out["playbook"] = _playbook(book, league, day, by_id)
    out["standing"] = _standing(book, league)
    meta = book.meta(league, limit=ROWS)
    out["meta"] = sorted(meta["items"], key=lambda r: (-(r["vs_past_high"] or 0), r["id"]))
    cross = book.cross_league(league)
    # One short table per other league, Hardcore first: Standard's old economy has huge gaps that would
    # otherwise fill the whole list.
    out["gaps"] = {}
    for other in sorted(cross["others"], key=lambda o: ("standard" in _family(cross, o), o)):
        rows_ = sorted((r for r in cross["items"] + cross["divine"] if r["verdict"] == "structural" and r["other"] == other),
                       key=lambda r: (-abs(math.log1p(r["gap"] or 0)), r["id"], r["market"]))[:GAP_ROWS]
        if rows_:
            out["gaps"][other] = rows_
    return out


def _family(cross: dict, other: str) -> str:
    return next((r["family"] for r in cross["items"] + cross["divine"] if r["other"] == other), "")


def _divine(con, book: LeagueBook, league: str, day: int, desk: Desk) -> dict | None:
    bridge = ANCHORS[book.realm][1]
    s = desk.summary()
    if s["bridge"] is None:
        return None
    # This league's own daily Divine price, read straight from the daily table so a league whose start is
    # not on disk still gets its line (its days are then counted from the first archived hour).
    own = [(int(d), float(p)) for d, p in con.execute(
        "SELECT day, price FROM league_daily WHERE league = ? AND item = ? AND price > 0 ORDER BY day",
        [league, bridge]).fetchall()]
    past = []
    precedents = book.precedents(league)
    curves = book.curve(bridge, leagues=precedents) if precedents else {}
    for name in sorted(curves):
        c = {r["day"]: r["price"] for r in curves[name]}
        if day in c:
            prev = c.get(day - 1)
            past.append({"league": name, "price": c[day], "change": c[day] / prev - 1 if prev else None})
    timing = book.divine_timing(league) if precedents else None
    return {"price": s["bridge"], "change": s["bridge_change_24h"], "own": own, "past": past,
            "advice": timing["advice"] if timing and timing["by_league"] else None}


def _playbook(book: LeagueBook, league: str, day: int, by_id: dict) -> dict:
    pb = book.playbook(league, PLAYBOOK_HORIZON, day=day)
    rows = []
    for r in pb["items"]:
        if not r["traded_now"] or r["median"] is None:
            continue
        agree = max(r["up_share"], 1 - r["up_share"])
        if agree < PLAYBOOK_AGREE:
            continue
        k = round(agree * r["n"])
        rows.append({**r, "agree_k": k, "price": (by_id.get(r["id"]) or {}).get("price")})
    up = sorted((r for r in rows if r["median"] > 0), key=lambda r: (-r["score"], r["id"]))[:ROWS]
    down = sorted((r for r in rows if r["median"] < 0), key=lambda r: (r["score"], r["id"]))[:ROWS]
    return {"precedents": pb["precedents"], "up": up, "down": down}


def _standing(book: LeagueBook, league: str) -> dict:
    st = book.standing(league)
    items = sorted(st["items"], key=lambda r: (r["vs_typical"], r["id"]))
    cheap = [r for r in items if r["vs_typical"] < 0][:ROWS]
    dear = [r for r in reversed(items) if r["vs_typical"] > 0][:ROWS]
    return {"precedents": st["precedents"], "cheap": cheap, "dear": dear}


# ---------------------------------------------------------------- format
def _e(x) -> str:
    return html.escape(str(x), quote=True)


def chaos(x) -> str:
    if x is None or not math.isfinite(x):
        return "–"
    if abs(x) >= 100:
        return f"{x:,.0f}"
    if abs(x) >= 10:
        return f"{x:.1f}"
    return f"{x:.3g}"


def pct(x, signed: bool = True) -> str:
    if x is None or not math.isfinite(x):
        return "–"
    return f"{x:+.1%}" if signed else f"{x:.0%}"


def _tone(x) -> str:
    return "" if x is None else "up" if x > 0 else "down" if x < 0 else ""


def spark(values, w: int = 84, h: int = 22) -> str:
    pts = [(i, v) for i, v in enumerate(values) if v is not None and math.isfinite(v) and v > 0]
    if len(pts) < 2:
        return ""
    ys = [math.log(v) for _, v in pts]
    lo, hi = min(ys), max(ys)
    span, n = (hi - lo) or 1.0, max(len(values) - 1, 1)
    path = " ".join(f"{i / n * (w - 2) + 1:.1f},{h - 2 - (y - lo) / span * (h - 4):.1f}" for (i, _), y in zip(pts, ys))
    color = "var(--up)" if pts[-1][1] >= pts[0][1] else "var(--down)"
    # No xmlns: inline SVG in HTML needs none, and the page links nowhere but the credits.
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.4" points="{path}"/></svg>')


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(rows_cell for rows_cell in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _td(text: str, cls: str = "") -> str:
    return f'<td class="{cls}">{text}</td>' if cls else f"<td>{text}</td>"


def _section(title: str, intro: str, body: str) -> str:
    return f'<section><h2>{_e(title)}</h2><p class="intro">{intro}</p>{body}</section>'


def _hour(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:00 UTC")


def _leagues_phrase(names: list[str]) -> str:
    names = [_e(n) for n in names]
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# ---------------------------------------------------------------- render
CSS = """
:root{color-scheme:dark;--bg:#0c0e11;--panel:#14181d;--line:#232931;--text:#e6eaef;--muted:#939ca8;--dim:#6b7480;
--accent:#b2ef78;--up:#5fd3a2;--down:#f0787f;--divine:#8fb8f2;
--mono:ui-monospace,SFMono-Regular,Consolas,monospace;--sans:system-ui,'Segoe UI',sans-serif}
@media (prefers-color-scheme:light){:root{color-scheme:light;--bg:#f7f8fa;--panel:#fff;--line:#e2e5ea;--text:#14181d;
--muted:#59626e;--dim:#7c8592;--accent:#3d7a12;--up:#14865a;--down:#c23a43;--divine:#2c64b8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 var(--sans)}
main{max-width:980px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}h2{font-size:16px;margin:0 0 4px}
header p,.intro{color:var(--muted);margin:2px 0 12px}
a{color:var(--accent)}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:16px 0}
.stats{display:flex;flex-wrap:wrap;gap:12px 32px;margin:8px 0 12px}
.stat b{display:block;font:600 20px var(--mono)}.stat span{color:var(--dim);font-size:12px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:right;color:var(--dim);font-weight:500;font-size:12px;padding:4px 8px;border-bottom:1px solid var(--line)}
td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--line);font-family:var(--mono);white-space:nowrap}
th:first-child,td:first-child{text-align:left;font-family:var(--sans);white-space:normal}
tr:last-child td{border-bottom:0}
.up{color:var(--up)}.down{color:var(--down)}.muted{color:var(--muted)}
.spark{vertical-align:middle}
h3{font-size:13px;margin:14px 0 4px;color:var(--muted);font-weight:600}
footer{color:var(--muted);font-size:13px;margin-top:28px}footer p{margin:6px 0}
"""


def render(data: dict) -> str:
    league, day = data["league"], data["day"]
    s = data["summary"]
    parts = [
        f'<header><h1>{_e(league)}, league day {day}</h1>',
        f'<p>The state of the Currency Exchange economy, from GGG\'s public hourly data up to {_hour(data["as_of"])}. '
        "Prices are in Chaos Orbs unless a column says otherwise.</p>",
    ]
    if data["truncated"]:
        parts.append('<p>This league was already running when the archive begins, so its days are counted from '
                     "the first hour on disk, not from launch, and it is not compared with past leagues.</p>")
    parts.append('<div class="stats">'
                 f'<div class="stat"><b>{chaos(s["bridge"])}</b><span>Divine Orb, in Chaos</span></div>'
                 f'<div class="stat"><b class="{_tone(s["bridge_change_24h"])}">{pct(s["bridge_change_24h"])}</b>'
                 "<span>Divine, last 24 hours</span></div>"
                 f'<div class="stat"><b>{chaos(s["value_24h"])}</b><span>Chaos traded, last 24 hours</span></div>'
                 f'<div class="stat"><b>{s["items"]:,}</b><span>items traded, last 24 hours</span></div>'
                 "</div></header>")
    for section in (_render_divine, _render_movers, _render_traded, _render_playbook, _render_standing,
                    _render_meta, _render_gaps):
        html_ = section(data)
        if html_:
            parts.append(html_)
    parts.append(_footer())
    title = f"{_e(league)} day {day} — Vaal Street league report"
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{title}</title><style>{CSS}</style></head><body><main>"
            + "\n".join(parts) + "</main></body></html>\n")


def _render_divine(data: dict) -> str:
    d = data["divine"]
    if not d:
        return ""
    body = []
    if len(d["own"]) >= 2:
        body.append(f'<p>Divine price by league day so far: {spark([p for _, p in d["own"]], 240, 40)}</p>')
    if d["past"]:
        rows = [[_td(_e(r["league"])), _td(chaos(r["price"])), _td(pct(r["change"]), _tone(r["change"]))]
                for r in d["past"]]
        rows.insert(0, [_td(f"<b>{_e(data['league'])}</b> (this league)"), _td(chaos(d["price"])),
                        _td(pct(d["change"]), _tone(d["change"]))])
        body.append(_table(["League", f"Divine on day {data['day']}", "Move that day"], rows))
    if d["advice"]:
        body.append(f'<p class="muted">{_e(d["advice"])}</p>')
    intro = (f"One Divine Orb costs {chaos(d['price'])} Chaos, {pct(d['change'])} over the last 24 hours."
             + (" Past leagues on the same league day:" if d["past"] else ""))
    return _section("The Divine Orb", intro, "".join(body))


def _market_rows(rows: list[dict]) -> list[list[str]]:
    return [[_td(_e(r["name"])), _td(chaos(r["price"])), _td(pct(r["change_24h"]), _tone(r["change_24h"])),
             _td(chaos(r["chaos"])), _td(chaos(r["divine"])), _td(chaos(r["value_24h"])), _td(spark(r["spark"]))]
            for r in rows]


MARKET_HEAD = ["Item", "Fair value", "24 hours", "Chaos market", "Divine market, in Chaos", "Chaos traded, 24 h",
               "Last 3 days"]


def _render_movers(data: dict) -> str:
    if not data["risers"] and not data["fallers"]:
        return ""
    body = ""
    if data["risers"]:
        body += "<h3>Up</h3>" + _table(MARKET_HEAD, _market_rows(data["risers"]))
    if data["fallers"]:
        body += "<h3>Down</h3>" + _table(MARKET_HEAD, _market_rows(data["fallers"]))
    intro = (f"The biggest price moves of the last 24 hours, of at least {MOVER_MIN:.0%}, among the {MOVER_POOL} most traded items. Fair value is "
             "the desk's estimate of what an item is worth, smoothed over the noise of single trades. An item's "
             "Chaos market and Divine market are separate prices; the Divine market is usually a few percent dearer.")
    return _section("Biggest movers", intro, body)


def _render_traded(data: dict) -> str:
    if not data["traded"]:
        return ""
    return _section("Most traded", "Where the Chaos went over the last 24 hours, apart from the Divine Orb itself.",
                    _table(MARKET_HEAD, _market_rows(data["traded"])))


def _render_playbook(data: dict) -> str:
    pb = data["playbook"]
    if not pb["up"] and not pb["down"]:
        return ""
    n = len(pb["precedents"])

    def rows(items):
        return [[_td(_e(r["name"])), _td(pct(r["median"]), _tone(r["median"])),
                 _td(f'{pct(r["worst"])} to {pct(r["best"])}'), _td(f'{r["agree_k"]} of {r["n"]}'),
                 _td(chaos(r["price"]))] for r in items]

    head = ["Item", "Typical move", "Range", "Leagues that agree", "Fair value now"]
    body = ""
    if pb["up"]:
        body += "<h3>Rose next</h3>" + _table(head, rows(pb["up"]))
    if pb["down"]:
        body += "<h3>Fell next</h3>" + _table(head, rows(pb["down"]))
    intro = (f"What the same items did over the next {PLAYBOOK_HORIZON} days from league day {data['day']} in "
             f"{n} past league{'s' if n != 1 else ''} ({_leagues_phrase(pb['precedents'])}), measured in Divine Orbs. "
             "Only items that moved the same way in most of them are shown. A few leagues are a small sample, and "
             "the past is not a promise.")
    return _section("What past leagues did next", intro, body)


def _render_standing(data: dict) -> str:
    st = data["standing"]
    if not st["cheap"] and not st["dear"]:
        return ""

    def rows(items):
        return [[_td(_e(r["name"])), _td(pct(r["vs_typical"]), _tone(r["vs_typical"])),
                 _td(f'{r["n"]}')] for r in items]

    head = ["Item", "Against past leagues", "Leagues compared"]
    body = ""
    if st["cheap"]:
        body += "<h3>Cheaper than usual</h3>" + _table(head, rows(st["cheap"]))
    if st["dear"]:
        body += "<h3>Dearer than usual</h3>" + _table(head, rows(st["dear"]))
    intro = ("Today's price in Divine Orbs against the typical price on the same league day in past leagues "
             f"({_leagues_phrase(st['precedents'])}).")
    return _section("Cheap or dear against past leagues", intro, body)


def _render_meta(data: dict) -> str:
    if not data["meta"]:
        return ""
    rows = [[_td(_e(r["name"])), _td(f'{r["growth"]:.1f}×'), _td(f'{r["past_high"]:.1f}×'), _td(f'{r["n"]}')]
            for r in data["meta"]]
    intro = ("Items bought in far greater numbers than on league day 1, more than in any past league by the same "
             "point. A sign that players found a new use for them.")
    return _section("Meta shifts", intro, _table(["Item", "Demand since day 1", "Highest in past leagues",
                                                  "Leagues compared"], rows))


def _render_gaps(data: dict) -> str:
    if not data["gaps"]:
        return ""
    body = ""
    for other, items in data["gaps"].items():
        rows = [[_td(_e(r["name"])), _td("Chaos market" if r["market"] == "chaos" else "Divine market"),
                 _td(pct(r["gap"]), _tone(r["gap"])), _td(f'{r["days"]}')] for r in items]
        body += f"<h3>{_e(other)}</h3>" + _table(["Item", "Market", "Price there, against here", "Days"], rows)
    intro = ("Prices that sat steadily higher or lower in this league's Hardcore and Standard markets over the last "
             "week, each counted in its own league's Divine Orbs. Items cannot move between leagues, so these are "
             "not trades; they say which differences are real when you read prices from the other leagues. "
             "Standard has run for years, so its prices are often far from a new league's.")
    return _section("Steady gaps against Hardcore and Standard", intro, body)


def _footer() -> str:
    return (
        "<footer><h2>How these numbers are made</h2>"
        "<p>Every figure comes from the public hourly Currency Exchange digests that Grinding Gear Games publishes. "
        "Fair value is a smoothed, volume-weighted estimate of each item's price. Past leagues are lined up on the "
        "number of days since their first trade. A report for a league day uses only data published up to the end "
        "of that day.</p>"
        f'<p>Made with <a href="{PROJECT_URL}">Vaal Street</a>, a free local trading desk. Exchange history comes from '
        f'GGG\'s documented <a href="{API_URL}">Currency Exchange API</a>; item metadata from the community '
        f'<a href="{REPOE_URL}">RePoE</a> export of the game\'s data files. Path of Exile and its item names are the '
        "property of Grinding Gear Games.</p>"
        "<p>This product isn't affiliated with or endorsed by Grinding Gear Games in any way.</p></footer>"
    )


# ----------------------------------------------------------------- write
def write(root: Path | None, out: Path, league: str, day: int | None = None, *, all_days: bool = False,
          realm: str = "poe1", log=print) -> list[Path]:
    """Write one day's page (or every day's, with `all_days`) and refresh the folder's index. Local only."""
    out.mkdir(parents=True, exist_ok=True)
    _first, last_day = league_days(root, league, realm)
    days = range(last_day + 1) if all_days else [last_day if day is None else day]
    written = []
    for d in days:
        target = out / f"{slug(league)}-day-{d}.html"
        target.write_text(render(build(root, league, d, realm)), encoding="utf-8")
        written.append(target)
        if log:
            log(f"Wrote {target}")
    index = out / "index.html"
    index.write_text(render_index(out), encoding="utf-8")
    written.append(index)
    return written


_PAGE = re.compile(r"^(.+)-day-(\d+)\.html$")


def render_index(out: Path) -> str:
    pages: dict[str, list[tuple[int, str]]] = {}
    for f in out.glob("*-day-*.html"):
        m = _PAGE.match(f.name)
        if m:
            pages.setdefault(m.group(1), []).append((int(m.group(2)), f.name))
    body = ""
    for league in sorted(pages):
        links = " ".join(f'<a href="{_e(name)}">day {d}</a>' for d, name in sorted(pages[league]))
        body += f"<section><h2>{_e(league)}</h2><p>{links}</p></section>"
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Vaal Street league reports</title><style>{CSS}</style></head><body><main>"
            "<header><h1>League reports</h1><p>One page per league day.</p></header>"
            + body + _footer() + "</main></body></html>\n")
