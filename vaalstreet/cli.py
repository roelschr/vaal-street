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
        WHERE family = 'challenge' AND last_hour >= (SELECT max(last_hour) FROM leagues) - INTERVAL 6 HOUR
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
        if not items.is_current(root, realm):
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


def _snapshot(args: argparse.Namespace) -> None:
    """Pack the indexed market history into upload-sized parts."""
    import tarfile
    from pathlib import Path

    from vaalstreet.paths import markets_dir

    out = Path(args.folder)
    out.mkdir(parents=True, exist_ok=True)
    tar_path = out / "vaal-markets.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(markets_dir(data_dir()), arcname="parquet/markets")
    size = args.part_mb * 1024 * 1024
    count = 0
    with tar_path.open("rb") as src:
        while chunk := src.read(size):
            (out / f"vaal-markets.tar.{count:03d}").write_bytes(chunk)
            count += 1
    tar_path.unlink()
    print(f"{count} parts written to {out}. Restore with: python -m vaalstreet restore {out}")


def _restore(args: argparse.Namespace) -> None:
    """Rebuild the archive from snapshot parts: the indexed history, without re-downloading it."""
    import io
    import tarfile
    from pathlib import Path

    parts = sorted(Path(args.folder).glob("vaal-markets.tar.[0-9][0-9][0-9]"))
    if not parts:
        sys.exit(f"No vaal-markets.tar.NNN parts in {args.folder}")
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(b"".join(p.read_bytes() for p in parts))) as tar:
        for member in tar.getmembers():  # refuse anything that would land outside the data directory
            if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts or not (member.isfile() or member.isdir()):
                sys.exit(f"Unexpected entry in snapshot: {member.name}")
        tar.extractall(root)
    print(f"Restored {len(parts)} parts into {root}. Deriving prices...")
    ingest(root)
    print("Done. `python -m vaalstreet serve` picks up from here and downloads only the hours since.")


def _serve(args: argparse.Namespace) -> None:
    from vaalstreet.server import serve

    serve(args.port, sync=not args.no_sync)


def _status(args: argparse.Namespace) -> None:
    con = connect(realm=args.realm)
    con.sql(
        """
        SELECT league, family, first_hour, last_hour, hours, round(value) AS traded_value
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
    from vaalstreet import calibrate
    from vaalstreet.desk import Desk
    from vaalstreet.ledger import Ledger

    con = connect()
    desk = Desk.build(con, _league(con, args))
    learned = calibrate.shares(calibrate.from_ledger(Ledger(data_dir()), desk.league, desk.panel,
                                                     desk.as_of + HOUR, args.share))
    plans = [p for p in desk.flips(share=args.share, min_margin=args.min_margin, share_by_item=learned)
             if args.kind in ("all", p["kind"])]
    for p in plans:
        b, s = p["buy_order"], p["sell_order"]
        p["buy_text"] = f"{b['currency_units']} {b['currency']} : {b['item_units']}"
        p["sell_text"] = f"{s['item_units']} : {s['currency_units']} {s['currency']}"
    print(f"{desk.league} - Divine {desk.bridge:.1f}c - plans assume you capture {args.share:.0%} of each market's flow"
          + (", or what your own orders measured (see `fills`)" if learned else ""))
    _table(plans, [("name", "Item", ""), ("kind", "Plan", ""), ("buy", "Buy", ".4g"), ("sell", "Sell", ".4g"),
                   ("margin", "Margin", ".1%"), ("reach", "Reach", ".0%"), ("buy_text", "Buy order", ""),
                   ("sell_text", "Sell order", ""), ("profit_per_hour", "c/hour", ",.0f"),
                   ("capital", "Capital", ",.0f")], args.rows)


def _fills(args: argparse.Namespace) -> None:
    from vaalstreet import calibrate
    from vaalstreet.desk import Desk
    from vaalstreet.ledger import Ledger

    con = connect()
    desk = Desk.build(con, _league(con, args))
    rows = calibrate.from_ledger(Ledger(data_dir()), desk.league, desk.panel, desk.as_of + HOUR, args.share)
    print(f"{desk.league} - how fast your orders filled, against the {args.share:.0%} share of the flow plans assume")
    if not rows:
        print("No orders to learn from yet. Track the orders you post in the desk and record their fills.")
        return
    for r in rows:
        r["name"] = desk._name(r["item"])
    _table(rows, [("name", "Item", ""), ("market", "Market", ""), ("orders", "Orders", "d"),
                  ("filled", "Filled", ",.0f"), ("eligible", "Traded at your price", ",.0f"),
                  ("measured", "Your share", ".1%"), ("share", "Share used", ".1%")], len(rows))
    print("\nShare used leans toward the default until you have several orders (the default counts as "
          f"{calibrate.PRIOR_ORDERS:g} orders).")
    for r in rows:
        print(f"  {r['name']} ({r['market'].title()}): {r['reading']}")


def _ladder(args: argparse.Namespace) -> None:
    from vaalstreet import ladder
    from vaalstreet.desk import Desk
    from vaalstreet.ledger import Ledger

    con = connect()
    desk = Desk.build(con, _league(con, args))
    ledger = Ledger(data_dir())  # the same Gold fees, recipes and vendor prices the desk in the browser uses
    out = ladder.plan(desk, args.start, hours=args.hours, target=args.target, share=args.share,
                      costs=ledger.costs(desk.league), fixed=ledger.fixed_conversions())
    print(f"{desk.league} - Divine {desk.bridge:.1f}c\n")
    rows = []
    for r in out["ladder"]:
        s = r["session"]
        rows.append({"label": r["label"], "chaos": r["chaos"], "can_run": r["can_run"],
                     "promised": s["expected_per_hour"], "replayed": s["replayed_per_hour"],
                     "hours": r["hours_to_next"], "gold": s["gold_per_hour"]})
    _table(rows, [("label", "Purse", ""), ("chaos", "Chaos", ",.0f"), ("can_run", "Can run", ""),
                  ("promised", "c/hour now", ",.2f"), ("replayed", "c/hour last week", ",.2f"),
                  ("hours", "Hours to next", ",.1f"), ("gold", "Gold/hour", ",.0f")], len(rows))
    print("Gold/hour is blank until you record the Gold your trades cost, or set an assumed fee in the desk.")
    print("\nWhat opens up at each step:")
    for r in out["ladder"]:
        if r["opens"]:
            kinds = f" (first {', '.join(r['opens_groups'])})" if r["opens_groups"] else ""
            print(f"  {r['label']}{kinds}: " + "; ".join(o["label"] for o in r["opens"]))
    print(f"\nFrom {args.start:,.0f} Chaos:")
    for line in out["schedule_text"]:
        print(f"  {line}")
    print(f"\n{out['note']}")


def _check(args: argparse.Namespace) -> None:
    from vaalstreet import check
    from vaalstreet.desk import Desk

    con = connect()
    desk = Desk.build(con, _league(con, args))
    item_units, currency_units = check.parse_ratio(args.ratio, args.market)
    r = check.check(desk, args.item, args.market, args.side, item_units, currency_units, args.stock)
    print(f"{r['name']}: {args.side} {item_units:g} for {currency_units:g} {args.market.title()} "
          f"in {desk.league} (Divine {desk.bridge:.1f}c)")
    print(f"\n  {r['verdict'].upper()}\n")
    for reason in r["reasons"]:
        print(f"  - {reason}")


def _stash(args: argparse.Namespace) -> None:
    from vaalstreet import check
    from vaalstreet.desk import Desk
    from vaalstreet.ledger import Ledger

    con = connect()
    desk = Desk.build(con, _league(con, args))
    ledger = Ledger(data_dir())
    if args.action == "set":
        if not args.item or args.units is None:
            sys.exit('Usage: stash set "<item>" <units>   (0 units removes the line)')
        ledger.set_stash(desk.league, check.resolve(desk, args.item), args.units)
    elif args.action == "clear":
        ledger.clear_stash(desk.league)
    if args.ledger:
        holdings = {k: p["units"] for k, p in ledger.positions(desk.league).items() if p["units"] > 1e-9}
    else:
        holdings = ledger.stash(desk.league)
    out = check.stash_value(desk, holdings)
    if not out["rows"]:
        print(f"Nothing held in {desk.league}. Add a line with: stash set \"<item>\" <units>")
        return
    for row in out["rows"]:
        for name in ("chaos", "divine"):
            row[f"{name}_price"] = (row.get(name) or {}).get("price")
        row["best_text"] = (f"{row['best'].title()} +{row['best_by']:.1%}" if row.get("best_by") is not None
                            else (row.get("best") or "-").title())
        row["thin_text"] = "hard to sell" if row.get("thin") else ""
    print(f"{desk.league} - {'ledger positions' if args.ledger else 'your stash'} at fair value (Divine {desk.bridge:.1f}c)")
    _table(out["rows"], [("name", "Item", ""), ("units", "Units", ",.6g"), ("value", "Value", ",.0f"),
                         ("low", "Low", ",.0f"), ("high", "High", ",.0f"), ("chaos_price", "Chaos mkt", ",.4g"),
                         ("divine_price", "Divine mkt", ",.4g"), ("best_text", "Pays more", ""),
                         ("hours", "Hours of volume", ",.1f"), ("thin_text", "", "")], 200)
    t = out["total"]
    print(f"\nTotal {t['value']:,.0f} Chaos (between {t['low']:,.0f} and {t['high']:,.0f}); "
          f"{t['best']:,.0f} if each line sells in the market that pays more.")
    if out["unpriced"]:
        print(f"{out['unpriced']} line(s) have no price in this league and are not counted.")


def _arb(args: argparse.Namespace) -> None:
    from vaalstreet.desk import Desk
    from vaalstreet.ledger import Ledger

    con = connect()
    desk = Desk.build(con, _league(con, args))
    ledger = Ledger(data_dir())
    fixed, checks = ledger.fixed_conversions(), ledger.recipe_checks()

    def check(row: dict) -> str:
        found = checks.get(row["recipe_key"])
        return found["status"] if found else "unchecked"

    rows = desk.conversions(share=args.share, fixed=fixed)
    for r in rows:
        if r.get("inputs"):
            r["recipe"] = " + ".join(f"{x['units']:g} {x['name']}" for x in r["inputs"]) + f" -> {r['gives']:g} {r['reward']}"
            r["name"] = "(yours)"
        else:
            r["recipe"] = f"{r['need']} -> {r['gives']} {r['reward']}"
        r["checked"] = check(r)
    print(f"{desk.league} - full sets against what they turn into")
    _table(rows, [("name", "Item", ""), ("recipe", "Set", ""), ("cost", "Cost", ",.4g"), ("proceeds", "Worth", ",.4g"),
                  ("margin", "Margin", ".1%"), ("margin_now", "In a hurry", ".1%"),
                  ("profit_per_hour", "c/hour", ",.0f"), ("checked", "In game", "")], args.rows)

    loops = desk.loops(share=args.share, swaps=args.swaps, fixed=fixed)
    for r in loops:
        r["route"] = " -> ".join(step["name"] for step in r["path"])
        r["checked"] = check(r)
    print(f"\nRound trips of up to {args.swaps} swaps")
    _table(loops, [("route", "Route", ""), ("edge", "Edge", ".1%"), ("value_24h", "Thinnest swap/day", ",.0f"),
                   ("resting_value", "Resting now", ",.0f"), ("profit_per_day", "c/day", ",.0f"),
                   ("checked", "In game", "")], args.rows)


def _item_id(names: dict[str, str], text: str) -> str:
    """An item ID from its name (any case) or the end of its ID."""
    found = names.get(text.lower()) or next((i for i in names.values() if i.endswith(text)), None)
    if not found:
        raise LookupError(f"No item called {text!r}. Use the name as the exchange shows it.")
    return found


def _counted(names: dict[str, str], text: str) -> tuple[str, float]:
    """"3 Test Card" -> (its ID, 3). A missing count means one."""
    head, _, rest = text.strip().partition(" ")
    try:
        units = float(head)
    except ValueError:
        return _item_id(names, text.strip()), 1.0
    return _item_id(names, rest.strip()), units


def _recipe(args: argparse.Namespace) -> None:
    from vaalstreet.ledger import Ledger

    root = data_dir()
    ledger = Ledger(root)
    meta = items.load(root, "poe1")
    name = lambda item_id: (meta.get(item_id) or {}).get("name") or items.fallback_name(item_id)
    if args.action == "add":
        if not args.give or not args.get:
            sys.exit("Say what goes in (--give) and what comes out (--get).")
        names = {(m.get("name") or "").lower(): item_id for item_id, m in meta.items()}
        inputs = [_counted(names, text) for text in args.give]
        output, gives = _counted(names, args.get)
        ledger.add_fixed("vendor" if args.vendor else "recipe", [list(x) for x in inputs], output, gives, args.note)
    elif args.action == "delete":
        if args.id is None:
            sys.exit("Say which one to delete (--id, from the list).")
        ledger.delete_fixed(args.id)
    checks = ledger.recipe_checks()
    rows = []
    for f in ledger.fixed_conversions():
        left = " + ".join(f"{n:g} {name(i)}" for i, n in f["inputs"])
        found = checks.get(f["key"])
        rows.append({"id": f["id"], "kind": "vendor price" if f["kind"] == "vendor" else "recipe",
                     "what": f"{left} {'buys' if f['kind'] == 'vendor' else '->'} {f['gives']:g} {name(f['output'])}",
                     "checked": found["status"] if found else "unchecked", "note": f["note"]})
    if not rows:
        print("No recipes or vendor prices of your own yet. Add one with: recipe add --give ... --get ...")
        return
    _table(rows, [("id", "ID", ""), ("kind", "Kind", ""), ("what", "Conversion", ""), ("checked", "In game", ""),
                  ("note", "Note", "")], len(rows))


def _playbook(args: argparse.Namespace) -> None:
    from vaalstreet.leagues import LeagueBook

    con = connect()
    book = LeagueBook(con)
    league = _league(con, args)
    if args.launch:
        _launch(book, league, args)
        return
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


def _launch(book, league: str, args: argparse.Namespace) -> None:
    out = book.launch(league, unit=args.unit, limit=10_000)
    print(f"The first {out['hours']} hours of past {out['family'].replace('_', ' ')} leagues: "
          f"{', '.join(out['precedents']) or 'none on disk'} (prices in {args.unit}, against each item's first-day value)")
    if out["shortfall"]:
        print(out["shortfall"])
    rows = [{**r, **{f"m{k}": v for k, v in r["moves"].items()}} for r in out["items"] if r["n"] >= args.min_leagues]
    if not rows:
        return
    print("\nWakes up = the league hour of its first trade. First = what it did first. Agree = leagues that did the same.")
    _table(rows, [("name", "Item", ""), ("n", "Leagues", ""), ("wake", "Wakes up", ".0f"), ("first", "First", ""),
                  ("agree", "Agree", ".0%"), ("m6", "Hour 6", "+.0%"), ("m24", "Hour 24", "+.0%"),
                  ("m72", "Hour 72", "+.0%")], args.rows)


def _divine_timing(args: argparse.Namespace) -> None:
    from vaalstreet.leagues import LeagueBook

    con = connect()
    out = LeagueBook(con).divine_timing(_league(con, args), max_day=args.max_day)
    print(out["advice"])
    if out["by_league"]:
        print("\nThe cheapest day in each past league:")
        _table(out["by_league"], [("league", "League", ""), ("day", "Day", ""), ("price", "Divine (c)", ",.4g")],
               len(out["by_league"]))
        print("\nHow much dearer the Divine was than on its cheapest day (median of past leagues):")
        _table(out["curve"], [("day", "Day", ""), ("dearer", "Dearer by", "+.0%"), ("n", "Leagues", "")],
               len(out["curve"]))


def _cross_league(args: argparse.Namespace) -> None:
    from vaalstreet.leagues import LeagueBook

    con = connect()
    league = _league(con, args)
    out = LeagueBook(con).cross_league(league, days=args.days)
    if not out["others"]:
        print(f"No Hardcore, Ruthless or Standard markets to compare {league} with.")
        return
    print(f"{league}: the same item in {', '.join(out['others'])} over the last {args.days} days, in each league's own"
          " Divines. A structural gap kept its sign and size day after day; noise flips or closes."
          " Items cannot move between leagues, so this is a guide to reading their prices, not a trade.")
    print("\nThe Divine in Chaos, against this league:")
    _table(out["divine"], [("other", "League", ""), ("gap", "Gap", "+.0%"), ("days", "Days", ""),
                           ("verdict", "Verdict", "")], len(out["divine"]))
    rows = [r for r in out["items"] if args.all or r["verdict"] == "structural"]
    print(f"\n{'Every gap' if args.all else 'Structural gaps'} (other league / this league - 1):")
    _table(rows, [("name", "Item", ""), ("market", "Market", ""), ("other", "League", ""), ("gap", "Gap", "+.0%"),
                  ("same_sign", "Same way", ".0%"), ("days", "Days", ""), ("hours", "Hours", ""),
                  ("verdict", "Verdict", "")], args.rows)


def _backtest(args: argparse.Namespace) -> None:
    con = connect()
    params = json.loads(args.params) if args.params else {}
    leagues = args.league or [_league(con, args)]
    if len(args.strategy) > 1 and not args.portfolio:
        sys.exit("Several strategies: add --portfolio to run them together on one bankroll.")
    if args.portfolio and args.params:
        sys.exit("With --portfolio, give each strategy its own settings, like 'reversion:{\"entry_z\": -2.5}'.")
    if args.export and args.portfolio:
        sys.exit("--export saves one strategy; run it without --portfolio.")
    options = dict(capital=args.capital, participation=args.participation, slippage=args.slippage,
                   start_day=args.start_day, days=args.days, attention=args.attention, compound=args.compound,
                   whole_lots=args.whole_lots, gold_per_unit=args.gold_per_unit, gold_budget=args.gold_budget,
                   schedule=args.schedule, fill_model=args.fill_model, risk_hours=args.risk_hours)
    summaries = []
    for league in leagues:
        started = time.time()
        if args.portfolio:
            result = backtest.run_portfolio(con, league, args.strategy, **options)
        else:
            spec, own = backtest.parse_spec(args.strategy[0])
            result = backtest.run(con, league, spec, params={**own, **params}, **options)
        summary = result.summary()
        summary["seconds"] = round(time.time() - started, 1)
        summaries.append(summary)
    if args.export:
        from vaalstreet import sharing

        spec, own = backtest.parse_spec(args.strategy[0])
        sharing.export(args.export, spec, {**own, **params}, options, summaries)
        print(f"Wrote {args.export}: the strategy, its settings and {len(summaries)} league result(s). Nothing was uploaded.",
              file=sys.stderr)
    if args.json:
        print(json.dumps(summaries, indent=1))
        return
    _table(summaries, [("league", "League", ""), ("strategy", "Strategy", ""), ("days", "Days", ""),
                       ("return", "Return", "+.1%"), ("divine_return", "Hold Divine", "+.1%"),
                       ("per_day", "c/day", ",.0f"), ("max_drawdown", "Max DD", ".1%"),
                       ("daily_sharpe", "Sharpe/d", ".2f"), ("fills", "Fills", ","), ("turnover", "Turnover", ".1f"),
                       ("gold_spent", "Gold", ","), ("chaos_per_1000_gold", "c/1k Gold", ",.1f"),
                       ("peak_holdings", "Most held", ",.0f"), ("peak_risk", "Worst drop", ",.0f"),
                       ("average_risk", "Avg drop", ",.0f")])
    print("  Most held: the most Chaos held in items at fair value. Worst / Avg drop: what the holdings would lose"
          f" on a bad day for these items, about 1 day in 20 over {args.risk_hours:g} hours (the peak, and the average over the run).")
    for s in summaries:
        tops = ", ".join(f"{t['name']} {t['profit']:+,.0f}" for t in s["top_items"][:5])
        print(f"  {s['league']}: {tops}")
        if s.get("strategies"):
            _table(s["strategies"], [("strategy", "  Strategy", ""), ("profit", "Profit", "+,.0f"),
                                     ("profit_share", "Share", ".0%"), ("gold_spent", "Gold", ","),
                                     ("peak_capital", "Most capital", ",.0f"),
                                     ("average_capital", "Avg capital", ",.0f"),
                                     ("peak_risk", "Worst drop", ",.0f"), ("fills", "Fills", ",")])


def _strategy_import(args: argparse.Namespace) -> None:
    from vaalstreet import sharing
    from vaalstreet.paths import strategies_dir

    doc = sharing.read(args.file)
    print(f"Strategy {doc.get('strategy')!r}, made with Vaal Street {doc.get('code_version', 'unknown')}.")
    print(f"Parameters: {json.dumps(doc.get('params') or {})}")
    for r in doc.get("results") or []:
        if isinstance(r, dict):
            print(f"  {r.get('league')}: return {r.get('return')}, {r.get('days')} days, fills {r.get('fills')}")
    if "source" not in doc:
        print("It is a built-in strategy, so there is no code to save. Run it with:")
        print("  " + sharing.run_command(doc))
        return
    print("\nWARNING: this file contains Python code. If you save it and backtest it, it runs on your computer"
          " with your permissions, and can do anything you can. Read it first, and only use code from people you trust.")
    print("\n----- " + sharing.file_name(doc) + " -----")
    print(doc["source"])
    print("----- end -----\n")
    if not args.yes:
        print("Nothing was saved. Run the same command with --yes to save it.")
        return
    target = sharing.save(doc, strategies_dir(data_dir()))
    print(f"Saved to {target}. Run it with:")
    print("  " + sharing.run_command(doc, target))


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
        if args.two_market:
            _two_market_scorecard(con, league, args)


def _two_market_scorecard(con, league: str, args: argparse.Namespace) -> None:
    """Experiment: one value plus a Divine-market premium, against each market's own fair value."""
    from vaalstreet import pricing
    from vaalstreet.panel import CORE, LEGS, Panel

    p = Panel.load(con, league, CORE + LEGS, min_value=args.min_value)
    rows = pricing.market_scorecard(p["price"], p["value"], p["r_price"], p["r_price"] * p["r_volume"],
                                    p["b_price"], p["b_price"] * p["b_volume"], horizon=args.horizon)
    print(f"{league}: experiment - one value plus a Divine-market premium, predicting each market's next "
          f"{args.horizon} hours (mean absolute log difference)")
    _table(rows, [("market", "Market", ""), ("items", "Items that", ""), ("count", "Count", ""),
                  ("two_market_mean", "One value", ".4f"), ("own_market_mean", "Own market", ".4f"),
                  ("blended_mean", "Blended", ".4f"), ("last_mean", "Last print", ".4f"),
                  ("improvement", "Error cut", "+.0%")])
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

    p = commands.add_parser("snapshot", help="pack the indexed history into parts small enough to upload")
    p.add_argument("folder")
    p.add_argument("--part-mb", type=int, default=9)
    p.set_defaults(run=_snapshot)

    p = commands.add_parser("restore", help="rebuild the archive from snapshot parts instead of downloading")
    p.add_argument("folder")
    p.set_defaults(run=_restore)

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

    p = commands.add_parser("fills", help="how fast your tracked orders filled, and the share of the flow plans now use")
    p.add_argument("--league")
    p.add_argument("--share", type=float, default=0.1, help="the share plans assume where you have no orders")
    p.set_defaults(run=_fills)

    p = commands.add_parser(
        "check", help="go / no-go for an offer you see in the in-game order window",
        description="Type an offer from the order window and see whether taking it is a good idea, "
                    "against that market's fair value, the flip plan and the market's volume.",
        epilog='The ratio is always items first, then currency: "64:1" with --market divine means 64 items for '
               '1 Divine; "1:140" with --market chaos means 1 item for 140 Chaos. You can also write '
               '"64 items = 1 divine" or "1 = 140 chaos".')
    p.add_argument("item", help='item name or ID, e.g. "Orb of Alteration"')
    p.add_argument("--market", choices=["chaos", "divine"], required=True, help="which currency the offer is priced in")
    p.add_argument("--side", choices=["buy", "sell"], required=True,
                   help="buy: you would pay currency for the items; sell: you would give items for currency")
    p.add_argument("--ratio", required=True, help='items first, e.g. "64:1" or "64 items = 1 divine"')
    p.add_argument("--stock", type=float, help="how many items are on offer at that ratio, if the window shows it")
    p.add_argument("--league")
    p.set_defaults(run=_check)

    p = commands.add_parser(
        "stash", help="value what you hold, and say which market pays more for each item",
        description="Keep a list of what you hold and value it at fair value, with a low and a high. "
                    "The list is saved in your ledger file.")
    p.add_argument("action", nargs="?", choices=["list", "set", "clear"], default="list",
                   help='list (default); set "<item>" <units> to add or change a line (0 removes it); clear to empty it')
    p.add_argument("item", nargs="?")
    p.add_argument("units", nargs="?", type=float)
    p.add_argument("--ledger", action="store_true", help="value the positions your recorded trades add up to instead")
    p.add_argument("--league")
    p.set_defaults(run=_stash)

    p = commands.add_parser(
        "ladder", help="what a small purse can run at each step up to 10 Divines, and when to switch plans",
        description="Shows, for purses from 25 Chaos up to 10 Divines, how many plans can run, what opens up "
                    "at each step, what the session plan promises now next to what it made last week, and how "
                    "long the next step takes. Then plans a session from your purse, switching plans at each "
                    "step. This is a projection from last week's prices, not a promise.")
    p.add_argument("--league")
    p.add_argument("--from", dest="start", type=float, default=25.0, help="your purse in Chaos (default 25)")
    p.add_argument("--hours", type=float, default=None, help="how long you will trade (default 6, or until --target)")
    p.add_argument("--target", type=float, default=None, help="stop the plan once the purse reaches this many Chaos")
    p.add_argument("--share", type=float, default=0.1, help="share of each market's flow you expect to capture")
    p.set_defaults(run=_ladder)

    p = commands.add_parser("arb", help="card sets, shards and your own recipes against what they turn into, "
                                        "and round trips through item-for-item markets")
    p.add_argument("--league")
    p.add_argument("--share", type=float, default=0.1)
    p.add_argument("--swaps", type=int, default=3, choices=range(1, 5),
                   help="longest round trip, in swaps (default 3; 4 can take several seconds)")
    p.add_argument("--rows", type=int, default=25)
    p.set_defaults(run=_arb)

    p = commands.add_parser(
        "recipe", help="list, add or delete your own recipes and vendor prices",
        description="The desk cannot read recipes or vendor prices from any source it may use, so you type in "
                    "the ones you know. A recipe turns what you give into what you get; a vendor price says an NPC "
                    "sells what you get for what you give, as many times as you like. Each one counts as "
                    "unchecked until you mark it confirmed in the desk after trying it in game.",
        epilog='examples:\n  recipe add --give "5 Test Card" --get "Test Orb" --note "full stack"\n'
               '  recipe add --vendor --give "3 Chaos Orb" --get "Test Shard"\n  recipe delete --id 2',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", choices=["list", "add", "delete"], default="list")
    p.add_argument("--give", action="append", metavar='"COUNT NAME"',
                   help="what goes in, e.g. \"5 Test Card\"; repeat for each input")
    p.add_argument("--get", metavar='"COUNT NAME"', help="what comes out, e.g. \"1 Test Orb\"")
    p.add_argument("--vendor", action="store_true", help="a vendor sells --get for --give (one currency)")
    p.add_argument("--note", default="", help="where it came from, or anything to remember")
    p.add_argument("--id", type=int, help="the ID to delete, as the list shows it")
    p.set_defaults(run=_recipe)

    p = commands.add_parser("playbook", help="what past leagues did next, from the current league day")
    p.add_argument("--league")
    p.add_argument("--horizon", type=int, default=7, help="days ahead (default 7)")
    p.add_argument("--day", type=int, help="league day to stand on (default: today)")
    p.add_argument("--unit", choices=["divine", "chaos"], default="divine")
    p.add_argument("--min-leagues", type=int, default=2)
    p.add_argument("--rows", type=int, default=30)
    p.add_argument("--launch", action="store_true",
                   help="instead: the first 72 hours of past leagues, when each item first traded and where it went")
    p.set_defaults(run=_playbook)

    p = commands.add_parser("divine-timing", help="on which league days Chaos bought the most Divines in past leagues")
    p.add_argument("--league")
    p.add_argument("--max-day", type=int, default=30, help="look at league days up to this one (default 30)")
    p.set_defaults(run=_divine_timing)

    p = commands.add_parser("cross-league", help="the same items in this league's Hardcore, Ruthless and Standard: "
                                                 "which price gaps are real and which close")
    p.add_argument("--league")
    p.add_argument("--days", type=int, default=7, help="how many recent days to compare (default 7)")
    p.add_argument("--all", action="store_true", help="show the noisy gaps too, not only the structural ones")
    p.add_argument("--rows", type=int, default=30)
    p.set_defaults(run=_cross_league)

    p = commands.add_parser("backtest", help="replay a strategy over one or more leagues")
    p.add_argument("strategy", nargs="+",
                   help=f"{', '.join(backtest.STRATEGIES)} or path/to/file.py:ClassName; add settings as "
                   "'reversion:{\"entry_z\": -2.5}'. Give several with --portfolio")
    p.add_argument("--portfolio", action="store_true",
                   help="run the strategies together on one bankroll; the first one listed fills first")
    p.add_argument("--fill-model", choices=backtest.FILL_MODELS, default="share",
                   help="share: a fill takes at most --participation of the hour's volume (default). "
                   "queue: an order first waits until the stock already on offer at your side has traded")
    p.add_argument("--risk-hours", type=float, default=24,
                   help="how long you expect to hold items, for the Worst drop column (default 24)")
    p.add_argument("--export", metavar="FILE.json",
                   help="save the strategy, its settings and its results per league to a local file to share")
    p.add_argument("--league", action="append", help="repeat to compare leagues (default: the live one)")
    p.add_argument("--params", help='JSON strategy parameters, e.g. \'{"reach": 0.35}\'')
    p.add_argument("--capital", type=float, default=10000)
    p.add_argument("--participation", type=float, default=0.1, help="max share of an hour's volume per fill")
    p.add_argument("--slippage", type=float, default=0.03, help="cost of crossing the spread")
    p.add_argument("--attention", type=int, default=1, help="hours between visits to re-post orders")
    p.add_argument("--start-day", type=float, default=0)
    p.add_argument("--days", type=float)
    p.add_argument("--compound", action="store_true", help="size orders on current equity instead of the stake")
    p.add_argument("--whole-lots", action="store_true",
                   help="trade like a real purse: whole items, at whole-number ratios you can afford")
    p.add_argument("--gold-per-unit", type=float, default=0.0,
                   help="Gold you pay per unit each time you post an order (the game does not publish fees)")
    p.add_argument("--gold-budget", type=float, help="stop posting new orders once this much Gold is spent")
    p.add_argument("--schedule", help='when you play, in UTC, e.g. "mon-fri 19-22; sat,sun 10-14" '
                   "(end hour not included); orders stand while you are away")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_backtest)

    p = commands.add_parser("strategy", help="strategy files shared by other players")
    sub = p.add_subparsers(dest="action", required=True)
    q = sub.add_parser("import", help="show a shared strategy file and save its code to your strategies folder")
    q.add_argument("file", help="a file written by backtest --export")
    q.add_argument("--yes", action="store_true", help="save the code after you have read it")
    q.set_defaults(run=_strategy_import)

    p = commands.add_parser("scorecard", help="how well fair value predicts upcoming prices, per league")
    p.add_argument("--league", action="append")
    p.add_argument("--horizon", type=int, default=6, help="hours ahead (default 6)")
    p.add_argument("--min-value", type=float, default=200000, help="skip items that traded less than this in total")
    p.add_argument("--two-market", action="store_true",
                   help="also try the experimental model: one value per item plus a Divine-market premium")
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
