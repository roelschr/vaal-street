"""Vaal Street: pricing, flips, arbitrage, league-start analytics and backtests for the
Path of Exile Currency Exchange.

    python -m vaalstreet serve          open the desk at http://localhost:3000
    python -m vaalstreet sync --all     mirror the full exchange history (resumable)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from vaalstreet import archive, backtest, items
from vaalstreet.db import connect
from vaalstreet.ingest import ingest
from vaalstreet.paths import REALMS, data_dir

HOUR = 3600


def _table(rows: list[dict], columns: list[tuple[str, str, str]], limit: int = 25) -> None:
    """Print dict rows as an aligned table. columns: (key, header, format)."""
    def cell(row: dict, key: str, fmt: str) -> str:
        value = row.get(key)
        if value is None:
            return "-"
        return format(value, fmt) if fmt else str(value)

    body = [[cell(r, k, f) for k, _, f in columns] for r in rows[:limit]]
    widths = [max(len(h), *(len(line[i]) for line in body)) if body else len(h) for i, (_, h, _) in enumerate(columns)]
    print("  ".join(h.ljust(w) for (_, h, _), w in zip(columns, widths)))
    for line in body:
        print("  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(line, widths))))


def _league(con, args) -> str:
    if getattr(args, "league", None):
        return args.league
    rows = con.execute(
        """
        SELECT league FROM leagues
        WHERE kind = 'challenge' AND last_hour >= (SELECT max(last_hour) FROM leagues) - INTERVAL 6 HOUR
        ORDER BY value / hours DESC LIMIT 1
        """
    ).fetchall()
    if not rows:
        sys.exit("No live challenge league in the archive; pass --league.")
    return rows[0][0]


def _sync(args: argparse.Namespace) -> None:
    root = data_dir()
    base_url = os.environ.get("ARCHIVE_BASE_URL", archive.DEFAULT_BASE_URL)
    for realm in args.realm or ["poe1"]:
        if not items.load(root, realm):
            print(f"[{realm}] {items.download(root, realm):,} item names saved")
        start = None if args.all else archive.latest_published() - args.days * 24 * HOUR
        archive.archive_realm(root, realm, start=start, base_url=base_url, delay=args.delay, workers=args.workers)
    ingest(root)


def _items(args: argparse.Namespace) -> None:
    for realm in REALMS:
        print(f"[{realm}] {items.download(data_dir(), realm):,} item names saved")


def _ingest(args: argparse.Namespace) -> None:
    added = ingest(data_dir())
    if not added:
        print("No raw data found. Run `python -m vaalstreet sync` first.")
    elif not any(added.values()):
        print("Parquet is already up to date.")


def _serve(args: argparse.Namespace) -> None:
    from vaalstreet.server import serve

    serve(args.port, sync=not args.no_sync)


def _status(args: argparse.Namespace) -> None:
    con = connect(realm=args.realm)
    con.sql(
        """
        SELECT league, kind, first_hour, last_hour, hours, round(value) AS traded_value
        FROM leagues WHERE kind <> 'private' ORDER BY last_hour DESC, value DESC LIMIT 40
        """
    ).show(max_rows=40, max_width=160)


def _price(args: argparse.Namespace) -> None:
    con = connect(realm=args.realm)
    con.sql(
        """
        SELECT hour, name, round(price, 4) AS price, round(r_price, 4) AS vs_chaos, round(b_price, 4) AS vs_divine,
               volume, round(value) AS value, round(bridge, 1) AS divine
        FROM league_prices
        WHERE league = $league AND (name ILIKE $item OR item ILIKE '%' || $item)
        ORDER BY hour DESC LIMIT $rows
        """,
        params={"league": _league(con, args), "item": args.item, "rows": args.rows},
    ).show(max_rows=args.rows, max_width=200)


def _flips(args: argparse.Namespace) -> None:
    from vaalstreet.desk import Desk

    con = connect()
    desk = Desk.build(con, _league(con, args))
    plans = [p for p in desk.flips(share=args.share, min_margin=args.min_margin) if args.kind in ("all", p["kind"])]
    for p in plans:
        b, s = p["buy_order"], p["sell_order"]
        p["buy_text"] = f"{b['currency_units']} {b['currency']} : {b['item_units']}"
        p["sell_text"] = f"{s['item_units']} : {s['currency_units']} {s['currency']}"
    print(f"{desk.league} - Divine {desk.bridge:.1f}c - plans assume you capture {args.share:.0%} of each market's flow")
    _table(plans, [("name", "Item", ""), ("kind", "Plan", ""), ("buy", "Buy", ".4g"), ("sell", "Sell", ".4g"),
                   ("margin", "Margin", ".1%"), ("reach", "Reach", ".0%"), ("buy_text", "Buy order", ""),
                   ("sell_text", "Sell order", ""), ("profit_per_hour", "c/hour", ",.0f"),
                   ("capital", "Capital", ",.0f")], args.rows)


def _arb(args: argparse.Namespace) -> None:
    from vaalstreet.desk import Desk

    con = connect()
    desk = Desk.build(con, _league(con, args))
    rows = desk.conversions(share=args.share)
    for r in rows:
        r["recipe"] = f"{r['need']} -> {r['gives']} {r['reward']}"
    print(f"{desk.league} - full sets against what they turn into")
    _table(rows, [("name", "Item", ""), ("recipe", "Set", ""), ("cost", "Cost", ",.4g"), ("proceeds", "Worth", ",.4g"),
                  ("margin", "Margin", ".1%"), ("margin_now", "In a hurry", ".1%"),
                  ("profit_per_hour", "c/hour", ",.0f")], args.rows)


def _playbook(args: argparse.Namespace) -> None:
    from vaalstreet.leagues import LeagueBook

    con = connect()
    book = LeagueBook(con)
    league = _league(con, args)
    pb = book.playbook(league, args.horizon, unit=args.unit, day=args.day, min_leagues=args.min_leagues)
    print(f"{league} day {pb['day']}: what the next {args.horizon} days did in {', '.join(pb['precedents']) or 'no earlier leagues'}"
          f" (prices in {args.unit})")
    rows = [r for r in pb["items"] if r["traded_now"]]
    half = max(1, args.rows // 2)
    columns = [("name", "Item", ""), ("n", "Leagues", ""), ("median", "Median", "+.0%"), ("worst", "Worst", "+.0%"),
               ("best", "Best", "+.0%"), ("up_share", "Up", ".0%"), ("chaos_now", "Now (c)", ",.4g")]
    print("\nHistorically rose from here:")
    _table(rows, columns, half)
    print("\nHistorically fell from here:")
    _table(rows[::-1], columns, half)


def _backtest(args: argparse.Namespace) -> None:
    con = connect()
    params = json.loads(args.params) if args.params else {}
    leagues = args.league or [_league(con, args)]
    summaries = []
    for league in leagues:
        started = time.time()
        result = backtest.run(
            con, league, args.strategy, params=params, capital=args.capital, participation=args.participation,
            slippage=args.slippage, start_day=args.start_day, days=args.days, attention=args.attention,
            compound=args.compound,
        )
        summary = result.summary()
        summary["seconds"] = round(time.time() - started, 1)
        summaries.append(summary)
    if args.json:
        print(json.dumps(summaries, indent=1))
        return
    _table(summaries, [("league", "League", ""), ("strategy", "Strategy", ""), ("days", "Days", ""),
                       ("return", "Return", "+.1%"), ("divine_return", "Hold Divine", "+.1%"),
                       ("per_day", "c/day", ",.0f"), ("max_drawdown", "Max DD", ".1%"),
                       ("daily_sharpe", "Sharpe/d", ".2f"), ("fills", "Fills", ","), ("turnover", "Turnover", ".1f")])
    for s in summaries:
        tops = ", ".join(f"{t['name']} {t['profit']:+,.0f}" for t in s["top_items"][:5])
        print(f"  {s['league']}: {tops}")


def _scorecard(args: argparse.Namespace) -> None:
    from vaalstreet import pricing
    from vaalstreet.panel import Panel

    con = connect()
    for league in args.league or [_league(con, args)]:
        panel = Panel.load(con, league, ("price", "value"), min_value=args.min_value)
        rows = pricing.scorecard(panel["price"], panel["value"], horizon=args.horizon)
        print(f"{league}: error predicting the value-weighted price of the next {args.horizon} hours "
              f"(absolute log difference; 0.05 is about 5%)")
        _table(rows, [("items", "Items that", ""), ("count", "Count", ""), ("fair_mean", "Fair value, mean", ".4f"),
                      ("last_mean", "Last print, mean", ".4f"), ("improvement", "Error cut", "+.0%"),
                      ("fair_median", "Fair, median", ".4f"), ("last_median", "Last, median", ".4f")])
        print()


def _sql(args: argparse.Namespace) -> None:
    connect(realm=args.realm).sql(args.query).show(max_rows=200, max_width=220)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vaalstreet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("serve", help="run the desk in your browser")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3000)))
    p.add_argument("--no-sync", action="store_true", help="do not download new hours in the background")
    p.set_defaults(run=_serve)

    p = commands.add_parser("sync", help="download exchange hours not yet on disk (newest first) and index them")
    p.add_argument("--days", type=int, default=14, help="how far back to reach (default 14)")
    p.add_argument("--all", action="store_true", help="the whole history since July 2024")
    p.add_argument("--realm", action="append", choices=REALMS, help="default: poe1")
    p.add_argument("--delay", type=float, default=0.5, help="pause after each request, per worker (default 0.5s)")
    p.add_argument("--workers", type=int, default=2)
    p.set_defaults(run=_sync)

    commands.add_parser("items", help="refresh item names and metadata").set_defaults(run=_items)
    commands.add_parser("ingest", help="index raw hours already on disk").set_defaults(run=_ingest)

    p = commands.add_parser("status", help="leagues and coverage in the archive")
    p.add_argument("--realm", choices=REALMS, default="poe1")
    p.set_defaults(run=_status)

    p = commands.add_parser("price", help="latest hourly prices for an item")
    p.add_argument("item", help='item name or ID ending, e.g. "Divine Orb"')
    p.add_argument("--league")
    p.add_argument("--realm", choices=REALMS, default="poe1")
    p.add_argument("--rows", type=int, default=24)
    p.set_defaults(run=_price)

    p = commands.add_parser("flips", help="ranked maker plans for the live league")
    p.add_argument("--league")
    p.add_argument("--kind", choices=["all", "chaos", "divine", "cross"], default="all")
    p.add_argument("--share", type=float, default=0.1, help="share of each market's flow you expect to capture")
    p.add_argument("--min-margin", type=float, default=0.02)
    p.add_argument("--rows", type=int, default=25)
    p.set_defaults(run=_flips)

    p = commands.add_parser("arb", help="card sets and shards against what they turn into")
    p.add_argument("--league")
    p.add_argument("--share", type=float, default=0.1)
    p.add_argument("--rows", type=int, default=25)
    p.set_defaults(run=_arb)

    p = commands.add_parser("playbook", help="what past leagues did next, from the current league day")
    p.add_argument("--league")
    p.add_argument("--horizon", type=int, default=7, help="days ahead (default 7)")
    p.add_argument("--day", type=int, help="league day to stand on (default: today)")
    p.add_argument("--unit", choices=["divine", "chaos"], default="divine")
    p.add_argument("--min-leagues", type=int, default=2)
    p.add_argument("--rows", type=int, default=30)
    p.set_defaults(run=_playbook)

    p = commands.add_parser("backtest", help="replay a strategy over one or more leagues")
    p.add_argument("strategy", help=f"{', '.join(backtest.STRATEGIES)} or path/to/file.py:ClassName")
    p.add_argument("--league", action="append", help="repeat to compare leagues (default: the live one)")
    p.add_argument("--params", help='JSON strategy parameters, e.g. \'{"reach": 0.35}\'')
    p.add_argument("--capital", type=float, default=10000)
    p.add_argument("--participation", type=float, default=0.1, help="max share of an hour's volume per fill")
    p.add_argument("--slippage", type=float, default=0.03, help="cost of crossing the spread")
    p.add_argument("--attention", type=int, default=1, help="hours between visits to re-post orders")
    p.add_argument("--start-day", type=float, default=0)
    p.add_argument("--days", type=float)
    p.add_argument("--compound", action="store_true", help="size orders on current equity instead of the stake")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_backtest)

    p = commands.add_parser("scorecard", help="how well fair value predicts upcoming prices, per league")
    p.add_argument("--league", action="append")
    p.add_argument("--horizon", type=int, default=6, help="hours ahead (default 6)")
    p.add_argument("--min-value", type=float, default=200000, help="skip items that traded less than this in total")
    p.set_defaults(run=_scorecard)

    p = commands.add_parser("sql", help="run a query against the views")
    p.add_argument("query")
    p.add_argument("--realm", choices=REALMS, default="poe1")
    p.set_defaults(run=_sql)

    args = parser.parse_args(argv)
    try:
        args.run(args)
    except (FileNotFoundError, RuntimeError, LookupError, ValueError) as err:
        sys.exit(str(err))
    except KeyboardInterrupt:
        sys.exit("Stopped. Run the same command again to resume.")


if __name__ == "__main__":
    main()
