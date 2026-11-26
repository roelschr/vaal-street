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
from vaalstreet.db import connect, has_data
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


def _app(args: argparse.Namespace) -> None:
    from vaalstreet import desktop

    options = {"hotkey": args.hotkey, "record_hotkey": args.record_hotkey,
               "stacks": args.stacks or args.stash, "stash": args.stash}
    try:
        desktop.run(args.port, sync=not args.no_sync, screen_check=not args.no_screen, options=options)
    except RuntimeError as err:
        sys.exit(str(err))


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
                   ("sell_text", "Sell order", ""), ("hours_per_lot", "Hours/lot", ".2g"),
                   ("profit_per_hour", "c/hour", ",.0f"), ("capital", "Capital", ",.0f")], args.rows)


def _rehearse(args: argparse.Namespace) -> None:
    """Step through a past league hour by hour, seeing only what was published by then."""
    from vaalstreet.desk import Desk
    from vaalstreet.rehearsal import league_hours

    con = connect()
    try:
        first, last = league_hours(con, args.league)
    except LookupError as err:
        sys.exit(str(err))
    hour = first + int(args.start_day * 24) * HOUR
    if not first <= hour <= last:
        sys.exit(f"{args.league} has {(last - first) // HOUR // 24 + 1} days on disk; pick a start day inside them.")
    print(f"Rehearsing {args.league}: each step is one hour later. Enter for the next hour, a number to jump "
          "that many hours, q to quit. Nothing after the hour shown is used.")
    import warnings

    warnings.simplefilter("ignore", RuntimeWarning)  # empty early-league columns; the table shows "-" for them
    while True:
        desk = Desk.build(con, args.league, as_of=hour)
        h = (hour - first) // HOUR
        when = time.strftime("%Y-%m-%d %H:00 UTC", time.gmtime(hour))
        print(f"\n== {args.league}  day {h // 24}, hour {h % 24}  ({when})  Divine {desk.bridge:.1f}c ==")
        plans = desk.flips(min_margin=args.min_margin)
        print("Top flips:")
        _table(plans, [("name", "Item", ""), ("kind", "Plan", ""), ("buy", "Buy", ".4g"), ("sell", "Sell", ".4g"),
                       ("margin", "Margin", ".1%"), ("profit_per_hour", "c/hour", ",.0f")], args.rows)
        movers = sorted((r for r in desk.market() if r.get("change_24h") is not None),
                        key=lambda r: -abs(r["change_24h"]))
        print("Movers over 24 hours:")
        _table(movers, [("name", "Item", ""), ("price", "Price", ".4g"), ("change_24h", "24h", "+.1%")], args.rows)
        if hour >= last:
            print(f"\nThat was the last hour of {args.league} on disk.")
            return
        try:
            answer = input("> ").strip().lower()
        except EOFError:
            return
        if answer in ("q", "quit", "exit"):
            return
        hour = min(last, hour + (int(answer) if answer.isdigit() and int(answer) > 0 else 1) * HOUR)


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
    from vaalstreet import fees, ladder
    from vaalstreet.desk import Desk
    from vaalstreet.ledger import Ledger

    con = connect()
    desk = Desk.build(con, _league(con, args))
    ledger = Ledger(data_dir())  # the same Gold fees, recipes and vendor prices the desk in the browser uses
    out = ladder.plan(desk, args.start, hours=args.hours, target=args.target, share=args.share,
                      costs=ledger.costs(desk.league, fees.by_item(data_dir())), fixed=ledger.fixed_conversions())
    print(f"{desk.league} - Divine {desk.bridge:.1f}c\n")
    rows = []
    for r in out["ladder"]:
        s = r["session"]
        rows.append({"label": r["label"], "chaos": r["chaos"], "can_run": r["can_run"],
                     "against": s["replayed_vs_divine_per_hour"], "replayed": s["replayed_per_hour"],
                     "promised": s["expected_per_hour"], "hours": r["hours_to_next"], "gold": s["gold_per_hour"]})
    _table(rows, [("label", "Purse", ""), ("chaos", "Chaos", ",.0f"), ("can_run", "Can run", ""),
                  ("against", "Last week vs Divines", ",.2f"), ("replayed", "Last week", ",.2f"),
                  ("promised", "Promised", ",.2f"),
                  ("hours", "Hours to next", ",.1f"), ("gold", "Gold/hour", ",.0f")], len(rows))
    divines = next((r["session"]["divines"] for r in out["ladder"] if r["session"].get("divines") is not None), None)
    print("Chaos an hour. Last week: what each step's plans made over the last week at their own stakes. "
          "Vs Divines: the same, less what holding Divines would have made"
          + (f" ({divines:+.0%} over those hours)." if divines is not None else ".")
          + " Promised: what today's levels earn if the last two days repeat; it has run well ahead of last week.")
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
    if r.get("ladder"):
        print("\n  Where it sits (Chaos each, dearest first):")
        for row in r["ladder"]:
            note = f"  ({row['note']})" if row["note"] else ""
            print(f"  {'>' if row['offer'] else ' '} {row['price']:>10.4g}  {row['label']}{note}")


def _ratio(args: argparse.Namespace) -> None:
    from vaalstreet import check

    try:
        r = check.ratio_helper(args.price, args.currency, args.max_items, args.max_currency)
    except ValueError as err:
        sys.exit(str(err))
    word = args.currency.title()

    def line(c: dict) -> str:
        return (f"buying: {c['currency_units']} {word} for {c['item_units']}   selling: {c['item_units']} for "
                f"{c['currency_units']} {word}   = {c['price']:.6g} {word} each ({c['error']:+.1%})")

    print(f"{args.price:g} {word} each")
    if r["ratio"] is None:
        print(f"  {r['note']}")
        return
    print(f"  {line(r['ratio'])}")
    if r["max_lots"] is not None:
        print(f"  With the limits you gave, that ratio fits {r['max_lots']} times.")
    for c in r["alternatives"]:
        print(f"  {'rounder' if c['kind'] == 'rounder' else 'closer'}: {line(c)}")


def _screen(args: argparse.Namespace) -> None:
    # Only the companion's own settings file; never the archive or the ledger, which may live in
    # another operating system's filesystem when this runs under Windows next to a desk in WSL.
    from vaalstreet import screen

    try:
        screen.run(data_dir(), args.desk, args.hotkey, args.league, args.calibrate, args.region, args.stacks or args.stash,
                   args.stash, args.why, record_hotkey=args.record_hotkey)
    except (RuntimeError, ValueError) as err:
        sys.exit(str(err))


def _stash_import(args: argparse.Namespace, desk, ledger) -> None:
    """Read a list of holdings from a file, show what it was read as, and write it only with --yes."""
    from pathlib import Path

    from vaalstreet import check

    if not args.item:
        sys.exit("Usage: stash import <file> [--replace] [--yes]   (one item per line: 45 Divine Orb)")
    try:
        text = Path(args.item).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as err:
        sys.exit(f"Cannot read {args.item}: {err}")
    holdings, unread = check.read_holdings(desk, text)
    held = ledger.stash(desk.league)
    for item, entry in holdings.items():
        was = held.get(item)
        note = "new" if was is None else "unchanged" if was == entry["units"] else f"was {was:g}"
        print(f"  {entry['units']:>12g}  {desk._name(item)}  ({note})")
    for row in unread:
        print(f"  line {row['line']} not read: {row['text']}  ({row['why']})")
    gone = [item for item in held if item not in holdings] if args.replace else []
    for item in gone:
        print(f"  removed: {held[item]:g} {desk._name(item)}  (not in the file)")
    if not holdings:
        sys.exit("No line of that file could be read.")
    if args.replace and unread:
        sys.exit("Some lines could not be read. Fix or delete them before using --replace, "
                 "or the items on those lines would be removed from your stash.")
    if not args.yes:
        sys.exit(f"Nothing written. Run it again with --yes to write these {len(holdings)} lines"
                 f"{f' and remove {len(gone)}' if gone else ''}.")
    ledger.set_stash_many(desk.league, {item: entry["units"] for item, entry in holdings.items()}, replace=args.replace)
    print(f"Wrote {len(holdings)} lines to your stash in {desk.league}.\n")


def _stash_plan(args: argparse.Namespace, desk, ledger, holdings: dict[str, float]) -> None:
    """The sell order to post for each line, as the Stash panel's sell plan shows it."""
    from vaalstreet import calibrate, check
    from vaalstreet.panel import HOUR

    measured = calibrate.from_ledger(ledger, desk.league, desk.panel, desk.as_of + HOUR)
    try:
        out = check.stash_plan(desk, holdings, reach=args.reach, min_value=args.min_value,
                               share_by_item=calibrate.shares(measured))
    except ValueError as err:
        sys.exit(str(err))
    if not holdings:
        print(f"Nothing held in {desk.league}. Add a line with: stash set \"<item>\" <units>")
        return
    rows = []
    for row in out["rows"]:
        best = row.get(row.get("best")) or {}
        o = best.get("order")
        rows.append({"name": row["name"], "units": row["units"],
                     "market": row["best"].title() if o else "",
                     "order": f"{o['item_units']} for {o['currency_units']} {o['currency']}" if o else "",
                     "post": best.get("units"), "left": best.get("left") or None, "value": best.get("value"),
                     "hours": best.get("hours"),
                     # A line with no order says why in the last column, where a long sentence does not push the table.
                     "note": ("thin market" if best.get("thin") else "") if o else row.get("why", "")})
    print(f"{desk.league} - selling {'your ledger positions' if args.ledger else 'your stash'}: orders that "
          f"{args.reach:.0%} of the last two days' volume traded at or above (Divine {desk.bridge:.1f}c)")
    _table(rows, [("name", "Item", ""), ("units", "Units", ",.6g"), ("market", "Sell in", ""), ("order", "Order", ""),
                  ("post", "Post", ",.6g"), ("left", "Left over", ",.6g"), ("value", "Brings in", ",.0f"),
                  ("hours", "Hours", ",.1f"), ("note", "", "")], 200)
    t, skipped = out["total"], out["skipped"]
    print(f"\nThe plan brings in {t['plan']:,.0f} Chaos for lines worth {t['planned_worth']:,.0f} at fair value "
          f"(the whole list is worth {t['worth']:,.0f}).")
    if skipped["lines"]:
        print(f"{skipped['lines']} line(s) worth {skipped['value']:,.0f} Chaos in all are under your minimum.")


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
    elif args.action == "import":
        _stash_import(args, desk, ledger)
    if args.ledger:
        holdings = {k: p["units"] for k, p in ledger.positions(desk.league).items() if p["units"] > 1e-9}
    else:
        holdings = ledger.stash(desk.league)
    if args.plan:
        _stash_plan(args, desk, ledger, holdings)
        return
    out = check.stash_value(desk, holdings)
    counted, now = ledger.stash_counted(desk.league), ledger.clock()  # when each line was last set
    if not out["rows"]:
        print(f"Nothing held in {desk.league}. Add a line with: stash set \"<item>\" <units>")
        return
    for row in out["rows"]:
        for name in ("chaos", "divine"):
            row[f"{name}_price"] = (row.get(name) or {}).get("price")
        row["best_text"] = (f"{row['best'].title()} +{row['best_by']:.1%}" if row.get("best_by") is not None
                            else (row.get("best") or "-").title())
        row["thin_text"] = "hard to sell" if row.get("thin") else ""
        age = None if args.ledger or row["id"] not in counted else max(0, now - counted[row["id"]])
        row["counted_text"] = ("" if age is None else f"{max(1, round(age / 60))} min ago" if age < 5400
                               else f"{round(age / 3600)} h ago" if age < 172800 else f"{round(age / 86400)} d ago")
    print(f"{desk.league} - {'ledger positions' if args.ledger else 'your stash'} at fair value (Divine {desk.bridge:.1f}c)")
    _table(out["rows"], [("name", "Item", ""), ("units", "Units", ",.6g"), ("counted_text", "Counted", ""),
                         ("value", "Value", ",.0f"),
                         ("low", "Low", ",.0f"), ("high", "High", ",.0f"), ("chaos_price", "Chaos mkt", ",.4g"),
                         ("divine_price", "Divine mkt", ",.4g"), ("best_text", "Pays more", ""),
                         ("hours", "Hours of volume", ",.1f"), ("thin_text", "", "")], 200)
    t = out["total"]
    print(f"\nTotal {t['value']:,.0f} Chaos (between {t['low']:,.0f} and {t['high']:,.0f}); "
          f"{t['best']:,.0f} if each line sells in the market that pays more.")
    if out["unpriced"]:
        print(f"{out['unpriced']} line(s) have no price in this league and are not counted.")


def _export(args: argparse.Namespace) -> None:
    """One ledger table as CSV: to a file with -o (with the byte order mark Excel needs), else to the screen."""
    from vaalstreet import export
    from vaalstreet.server import ApiError, App

    try:
        name, text = App(data_dir(), sync=False).export(args.what, args.league)
    except (ApiError, LookupError) as err:
        sys.exit(str(err))
    if not args.output:
        sys.stdout.write(text)
        return
    # newline="" keeps the csv module's own line ends, which spreadsheets expect.
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(export.BOM + text)
    print(f"Wrote {args.output} (the desk would name it {name}).", file=sys.stderr)


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
    outside = _outside_rows(desk, ledger, args.share)
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
    if outside:
        print(f"\nCard sets whose reward does not trade on the exchange: "
              f"reward priced by poe.ninja (stash listings, not the exchange) - prices from poe.ninja")
        now = time.time()
        for r in outside:
            r["recipe"] = f"{r['need']} -> {r['gives']} {r['reward']}"
            r["checked"] = check(r)
            age = (now - r["reward_fetched"]) / HOUR if r.get("reward_fetched") else None
            r["evidence"] = (f"{r['reward_listings'] or 0} listed, {age:.0f}h old" if age is not None else "?")
            r["evidence"] += " (stale)" if r["reward_stale"] else ""
        _table(outside, [("name", "Card", ""), ("recipe", "Set", ""), ("cost", "Cost", ",.4g"),
                         ("proceeds", "Worth", ",.4g"), ("margin", "Margin", ".1%"),
                         ("profit_per_hour", "c/hour", ",.0f"), ("evidence", "poe.ninja", ""),
                         ("checked", "In game", "")], args.rows)
        notes = [f"{r['reward']}: {r['reward_variant']}" for r in outside[: args.rows] if r.get("reward_variant")]
        for note in notes:
            print(f"  {note}")

    loops = desk.loops(share=args.share, swaps=args.swaps, fixed=fixed)
    for r in loops:
        r["route"] = " -> ".join(step["name"] for step in r["path"])
        r["checked"] = check(r)
    print(f"\nRound trips of up to {args.swaps} swaps")
    _table(loops, [("route", "Route", ""), ("edge", "Edge", ".1%"), ("value_24h", "Thinnest swap/day", ",.0f"),
                   ("resting_value", "Resting now", ",.0f"), ("profit_per_hour", "c/hour", ",.1f"),
                   ("checked", "In game", "")], args.rows)


def _outside_rows(desk, ledger, share: float) -> list[dict]:
    """Card sets priced from the poe.ninja cache (no network), unless you turned that off."""
    from vaalstreet import ninja

    if not ledger.settings().get("ninja_prices"):
        return []
    client = ninja.Ninja(data_dir(), os.environ.get("NINJA_BASE_URL", ninja.DEFAULT_BASE_URL))
    return desk.outside_conversions(ninja.reward_prices(client, desk.league, desk.meta), share=share)


def _gold_option(args: argparse.Namespace):
    """What a backtest charges in Gold: the flat `--gold-per-unit`, or with `--fee-table` the fee table."""
    if not getattr(args, "fee_table", False):
        return args.gold_per_unit
    from vaalstreet import fees

    if args.gold_per_unit:
        sys.exit("Give --fee-table or --gold-per-unit, not both.")
    table = fees.by_item(data_dir())
    if not table:
        sys.exit("No Gold fee table yet. Run `python -m vaalstreet fees refresh`.")
    return backtest.FeeTable(table)


def _fees(args: argparse.Namespace) -> None:
    """The Gold fee table: what is cached, a fresh fetch, or the fee of the items whose name matches."""
    from vaalstreet import fees

    root = data_dir()
    league = getattr(args, "league", None)
    if league is None and has_data(root):
        try:
            league = _league(connect(), args)
        except SystemExit:
            league = None
    if args.action == "refresh":
        table = fees.refresh(root, "poe1", league, url=os.environ.get("GOLD_FEES_URL", fees.DEFAULT_URL), force=True)
        if table.error:
            sys.exit(f"Could not fetch the Gold fees: {table.error}"
                     + (f" The table from {_when(table.fetched)} is kept." if len(table) else ""))
    else:
        table = fees.load(root, "poe1", league)
    if not len(table):
        sys.exit("No Gold fee table yet. Run `python -m vaalstreet fees refresh`.")
    print(f"{len(table):,} items - fetched {_when(table.fetched)}" + (f" during {table.league}" if table.league else "")
          + (" - from an earlier league: run `fees refresh`" if table.stale else "") + f" - {fees.CREDIT}")
    if args.action == "show":
        words = " ".join(args.item).casefold()
        rows = [{"name": name, "gold": gold} for name, gold in sorted(table.fees.items()) if words in name.casefold()]
        if not rows:
            sys.exit(f"No item in the table matches {' '.join(args.item)!r}.")
        _table(rows, [("name", "Item", ""), ("gold", "Gold per item", ",.3f")], len(rows))


def _when(t: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "never"


def _ninja(args: argparse.Namespace) -> None:
    from datetime import datetime, timezone

    from vaalstreet import ninja
    from vaalstreet.ledger import Ledger

    root = data_dir()
    if args.action in ("on", "off"):
        Ledger(root).set("ninja_prices", 1.0 if args.action == "on" else 0.0)
        print("poe.ninja prices are " + ("on: the desk asks for them at most hourly." if args.action == "on"
                                         else "off: the desk sends poe.ninja no requests."))
        return
    client = ninja.Ninja(root, os.environ.get("NINJA_BASE_URL", ninja.DEFAULT_BASE_URL))
    if args.action == "refresh":
        if not Ledger(root).settings().get("ninja_prices"):
            sys.exit("poe.ninja prices are turned off (setting ninja_prices = 0).")
        con = connect()
        league = _league(con, args)
        summary = ninja.refresh(client, league, items.load(root, "poe1"))
        if summary["league_id"] is None:
            sys.exit(summary["errors"][0] if summary["errors"] else "No card reward needs poe.ninja.")
        print(f"{league}: checked {', '.join(summary['types']) or 'nothing (no card rewards need it)'} "
              f"- prices from poe.ninja. Each list is asked for at most once an hour.")
        for error in summary["errors"]:
            print(f"  {error} (the last good answer is kept and marked stale)")
        return
    rows = client.status()
    if not rows:
        print("Nothing from poe.ninja yet. Run `python -m vaalstreet ninja refresh`, or start the desk.")
        return
    when = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "never"
    print("Cached answers from poe.ninja (prices from poe.ninja):")
    for r in rows:
        what = r["params"].get("type") or "leagues"
        league = r["params"].get("league", "")
        state = f"failed: {r['error']}" if r["error"] else "ok"
        print(f"  {what:<16} {league:<12} fetched {when(r['fetched'])}, last asked {when(r['checked'])}, {state}")


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


def _meta(args: argparse.Namespace) -> None:
    from vaalstreet.leagues import LeagueBook

    con = connect()
    out = LeagueBook(con).meta(_league(con, args))
    print(f"{out['league']} day {out['day']}: items whose demand (units traded in the last 24 hours against day 1) "
          f"grew faster than in any past league by the same hour ({', '.join(out['precedents']) or 'none on disk'})")
    if out["note"]:
        print(out["note"])
        return
    if not out["items"]:
        print("Nothing is growing faster than past leagues did.")
        return
    _table(out["items"], [("name", "Item", ""), ("growth", "Growth", ".2f"), ("past_low", "Past low", ".2f"),
                          ("past_median", "Past median", ".2f"), ("past_high", "Past high", ".2f"),
                          ("n", "Leagues", "")], args.rows)


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
    seen = {r["other"] for r in out["items"] + out["divine"]}
    thin = [o for o in out["others"] if o not in seen]
    if thin:
        print(f"\n{', '.join(thin)}: too little trade on the same hours to compare.")
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
                   whole_lots=args.whole_lots, gold_per_unit=_gold_option(args), gold_budget=args.gold_budget,
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
        sharing.export(args.export, spec, {**own, **params}, backtest.describe_options(options), summaries)
        print(f"Wrote {args.export}: the strategy, its settings and {len(summaries)} league result(s). Nothing was uploaded.",
              file=sys.stderr)
    if args.json:
        print(json.dumps(summaries, indent=1))
        return
    _table(summaries, [("league", "League", ""), ("strategy", "Strategy", ""), ("days", "Days", ""),
                       ("vs_divine", "Vs Divines", "+.1%"), ("return", "Return", "+.1%"),
                       ("divine_return", "Hold Divine", "+.1%"), ("per_hour", "c/hour", ",.1f"),
                       ("vs_divine_per_hour", "c/h vs Div", ",.1f"), ("max_drawdown", "Max DD", ".1%"),
                       ("daily_sharpe", "Sharpe/d", ".2f"), ("fills", "Fills", ","), ("turnover", "Turnover", ".1f"),
                       ("gold_spent", "Gold", ","), ("chaos_per_1000_gold", "c/1k Gold", ",.1f"),
                       ("peak_holdings", "Most held", ",.0f"), ("peak_risk", "Worst drop", ",.0f"),
                       ("average_risk", "Avg drop", ",.0f")])
    print("  Vs Divines: the return measured in Divines, so beating simply holding Divines is above zero.")
    print("  Most held: the most Chaos held in items at fair value. Worst / Avg drop: what the holdings would lose"
          f" on a bad day for these items, about 1 day in 20 over {args.risk_hours:g} hours (the peak, and the average over the run).")
    for s in summaries:
        if s.get("gold_unpriced"):
            names = s["gold_unpriced"]
            print(f"  {s['league']}: the fee table does not list {len(names)} item(s) this run bought, so their buy orders"
                  f" cost no Gold here and the Gold figure is too low: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
    for s in summaries:
        tops = ", ".join(f"{t['name']} {t['profit']:+,.0f}" for t in s["top_items"][:5])
        print(f"  {s['league']}: {tops}")
        if s.get("strategies"):
            _table(s["strategies"], [("strategy", "  Strategy", ""), ("profit", "Profit", "+,.0f"),
                                     ("profit_share", "Share", ".0%"), ("gold_spent", "Gold", ","),
                                     ("peak_capital", "Most capital", ",.0f"),
                                     ("average_capital", "Avg capital", ",.0f"),
                                     ("peak_risk", "Worst drop", ",.0f"), ("fills", "Fills", ",")])


def _search(args: argparse.Namespace) -> None:
    from vaalstreet import search

    con = connect()
    leagues = [x.strip() for x in args.leagues.split(",") if x.strip()] if args.leagues else None
    options = dict(capital=args.capital, participation=args.participation, slippage=args.slippage, days=args.days,
                   attention=args.attention, whole_lots=args.whole_lots, gold_per_unit=_gold_option(args),
                   schedule=args.schedule, fill_model=args.fill_model)
    report = search.walk_forward(con, args.strategy, args.grid, leagues=leagues, how=args.score, **options)
    if args.export:
        from vaalstreet import sharing

        params, _ = search._split(report["next"]["setting"], args.strategy)
        sharing.export(args.export, args.strategy, params, backtest.describe_options({**options, **report["next"]["setting"]}),
                       search.export_rows(report), extra={"walk_forward": report})
        print(f"Wrote {args.export}: the setting it would pick next, and the results on leagues it was not tuned on."
              " Nothing was uploaded.", file=sys.stderr)
    print(json.dumps(report, indent=1) if args.json else search.format_report(report))


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


def _research(args: argparse.Namespace) -> None:
    from vaalstreet import research

    if args.name == "all":
        _research_all(research.MEASUREMENTS, args.league or [])
        return
    con = connect()
    print("\n".join(research.run(con, args.name, args.league).lines()))
    print()


def _research_all(names: tuple[str, ...], leagues: list[str]) -> None:
    """Each measurement in its own child process, printed in order.

    On the full archive one measurement alone peaks near 6.4 GB, and memory a process has used is
    not handed back to the system, so running them one after another in one process gets killed.
    A child gives all of its memory back when it exits.
    """
    import subprocess
    from pathlib import Path

    env = dict(os.environ)
    # The child imports this same copy of the package, even when it is not installed.
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
    failed = []
    for name in names:
        command = [sys.executable, "-m", "vaalstreet", "research", name]
        for league in leagues:
            command += ["--league", league]
        child = subprocess.run(command, capture_output=True, text=True, env=env)
        sys.stdout.write(child.stdout)
        sys.stdout.flush()
        if child.stderr:
            sys.stderr.write(child.stderr)
        if child.returncode != 0:
            failed.append(name)
            print(f"{name}: did not finish (exit code {child.returncode})\n")
    if failed:
        sys.exit(f"Measurements that did not finish: {', '.join(failed)}")


def _report(args: argparse.Namespace) -> None:
    from pathlib import Path

    from vaalstreet import report

    con = connect()
    league = _league(con, args)
    con.close()
    try:
        report.write(None, Path(args.out), league, args.day, all_days=args.all_days)
    except (LookupError, ValueError) as e:
        sys.exit(str(e))
    print("Nothing was uploaded. The folder is yours to publish wherever you like, or to keep.", file=sys.stderr)


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

    p = commands.add_parser(
        "app", help="run the desk and the screen check together, as the Windows app does",
        description="One process that runs the desk and, when its optional libraries are installed, the screen "
                    "check next to it. The Windows app starts this for you; run it by hand under Windows Python "
                    "to get the same without the app window. The stack check stays off unless you ask for it.",
        epilog="The first line printed is VAAL-DESK-READY and the desk's address. The process stops when its "
               "standard input closes, or on Ctrl+C.")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3000)),
                   help="the desk's port (default %(default)s); 0 takes any free port")
    p.add_argument("--no-sync", action="store_true", help="do not download new hours in the background")
    p.add_argument("--no-screen", action="store_true", help="run the desk alone, without the screen check")
    p.add_argument("--hotkey", help="the screen check's key, in pynput's form (default <ctrl>+<alt>+<f9>)")
    p.add_argument("--record-hotkey", help="the key that also records the order shown (default <ctrl>+<alt>+<f10>)")
    p.add_argument("--stacks", action="store_true",
                   help="also value the stack under the cursor when you press Ctrl+C in game (see `screen -h`)")
    p.add_argument("--stash", action="store_true",
                   help="like --stacks, and also write each copied stack to your stash list")
    p.set_defaults(run=_app)

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

    p = commands.add_parser("ninja", help="prices from poe.ninja for card rewards the exchange does not trade")
    p.add_argument("action", choices=("refresh", "status", "on", "off"),
                   help="refresh: ask poe.ninja for what the card rewards need (at most hourly); "
                        "status: what is cached and how old it is; on/off: allow or stop poe.ninja requests")
    p.add_argument("--league")
    p.set_defaults(run=_ninja)

    p = commands.add_parser("fees", help="the Gold each item costs on the exchange, from poedb.tw")
    p.add_argument("action", choices=("status", "refresh", "show"), nargs="?", default="status",
                   help="status: what is cached and how old it is; refresh: fetch the table again now "
                        "(the desk does that itself once per league); show: the fee of the items you name")
    p.add_argument("item", nargs="*", help='with show: part of an item name, e.g. "stacked deck"')
    p.add_argument("--league")
    p.set_defaults(run=_fees)

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

    p = commands.add_parser("rehearse", help="practise on a past league: step through it hour by hour, "
                                             "seeing only what the desk knew at each hour")
    p.add_argument("league")
    p.add_argument("--start-day", type=float, default=0, help="league day to start on (default: its first hour)")
    p.add_argument("--min-margin", type=float, default=0.02)
    p.add_argument("--rows", type=int, default=5)
    p.set_defaults(run=_rehearse)

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
        "ratio", help="the whole-number ratio to type for a price per item, e.g. 9.5 -> 19 Chaos for 2",
        description="The exchange takes whole numbers only. Give the price you want per item and see the "
                    "smallest ratio within 2%% of it (the one flip plans post), how far it is off, and a "
                    "rounder and a closer choice.")
    p.add_argument("price", type=float, help="the price per item, in the currency below")
    p.add_argument("--currency", choices=["chaos", "divine"], default="chaos", help="the price is in Chaos (default) or Divines")
    p.add_argument("--max-items", type=float, help="the items you hold: no ratio may ask for more")
    p.add_argument("--max-currency", type=float, help="what you can pay: no ratio may ask for more")
    p.set_defaults(run=_ratio)

    p = commands.add_parser(
        "screen", help="press a key to read an offer off the exchange window and check it (optional extra)",
        description="A companion to run next to the game, under Windows Python. Each press of the hotkey takes "
                    "one screenshot (your whole screen, or the area you set with --calibrate), reads the offer's "
                    "items, ratio, amounts and Gold, and shows the desk's verdict here and in the desk's hideout window. It sends "
                    "nothing to the game and keeps no screenshot. With --stacks it also says what a stack is "
                    "worth when you press the game's own copy key (Ctrl+C) over it.",
        epilog='Needs the optional libraries: pip install "vaalstreet[screen]". The desk itself '
               "(vaalstreet serve) can run in WSL; this companion reaches it at http://localhost:<port>.")
    p.add_argument("--desk", default="http://localhost:3000", help="the desk's address (default %(default)s)")
    p.add_argument("--hotkey", help="the key to press, in pynput's form (default <ctrl>+<alt>+<f9>)")
    p.add_argument("--record-hotkey", help="the key that also records the order shown, with your amounts and its Gold, "
                                           "in your Ledger (default <ctrl>+<alt>+<f10>)")
    p.add_argument("--calibrate", action="store_true", help="capture once, then save the area around the exchange window")
    p.add_argument("--region", help='save this area and use it: "left top width height" in pixels')
    p.add_argument("--stacks", action="store_true",
                   help="also value the stack under the cursor when you press Ctrl+C in game: reads the clipboard "
                        "once per press, only while the game is the window in front (Windows only)")
    p.add_argument("--stash", action="store_true",
                   help="like --stacks, and also write each copied stack to your stash list on the desk: a copy "
                        "sets the item's line; the same item copied again right after with a different count adds")
    p.add_argument("--why", action="store_true",
                   help="with --stacks or --stash, for checking your setup: when a Ctrl+C is ignored because another "
                        "window is in front, say which kind of window it was. The clipboard is still not read")
    p.add_argument("--league")
    p.set_defaults(run=_screen)

    p = commands.add_parser(
        "stash", help="value what you hold, and say which market pays more for each item",
        description="Keep a list of what you hold and value it at fair value, with a low and a high. "
                    "The list is saved in your ledger file.")
    p.add_argument("action", nargs="?", choices=["list", "set", "clear", "import"], default="list",
                   help='list (default); set "<item>" <units> to add or change a line (0 removes it); clear to empty it; '
                        'import <file> to read a list with one item per line ("45 Divine Orb", or rows of a CSV file)')
    p.add_argument("item", nargs="?", help="the item for set, or the file for import")
    p.add_argument("units", nargs="?", type=float)
    p.add_argument("--yes", action="store_true", help="import: write the lines; without it they are only shown")
    p.add_argument("--replace", action="store_true",
                   help="import: also remove the lines the file does not name, so the stash becomes exactly the file")
    p.add_argument("--ledger", action="store_true", help="value the positions your recorded trades add up to instead")
    p.add_argument("--plan", action="store_true",
                   help="say what sell order to post for each line, in which market, and about how long it takes")
    p.add_argument("--reach", type=float, default=0.35, choices=[0.15, 0.25, 0.35, 0.5],
                   help="--plan: how patient the orders are, as the share of the last two days' volume that traded "
                        "at or above the price: 0.5 sells fastest, 0.15 holds out for most (default %(default)s)")
    p.add_argument("--min-value", type=float, default=0.0,
                   help="--plan: leave out lines worth less than this many Chaos (they are counted, not hidden)")
    p.add_argument("--league")
    p.set_defaults(run=_stash)

    p = commands.add_parser(
        "export", help="save your trades, positions, orders, items you made or stash as a CSV file for a spreadsheet",
        description="Writes one of the Ledger page's tables as CSV, the same rows the page shows (all of them, not "
                    "only the latest 200). It is a file on this machine; nothing is uploaded.")
    p.add_argument("what", choices=["trades", "positions", "orders", "made", "stash"])
    p.add_argument("-o", "--output", metavar="FILE", help="write to this file; without it the CSV is printed")
    p.add_argument("--league")
    p.set_defaults(run=_export)

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

    p = commands.add_parser("meta", help="items whose demand is growing faster than in any past league: meta shifts")
    p.add_argument("--league")
    p.add_argument("--rows", type=int, default=30)
    p.set_defaults(run=_meta)

    p = commands.add_parser("cross-league", help="the same items in this league's Hardcore, Ruthless and Standard: "
                                                 "which price gaps are real and which close")
    p.add_argument("--league")
    p.add_argument("--days", type=int, default=7, help="how many recent days to compare (default 7)")
    p.add_argument("--all", action="store_true", help="show the noisy gaps too, not only the structural ones")
    p.add_argument("--rows", type=int, default=30)
    p.set_defaults(run=_cross_league)

    p = commands.add_parser("report", help="write a 'state of the economy' web page for a league day to a local folder")
    p.add_argument("--league")
    p.add_argument("--day", type=int, help="league day (default: today, or the last day of an ended league)")
    p.add_argument("--all-days", action="store_true", help="write a page for every day of the league")
    p.add_argument("--out", default="reports", help="folder to write to (default: ./reports)")
    p.set_defaults(run=_report)

    p = commands.add_parser("backtest", help="replay a strategy over one or more leagues")
    p.add_argument("strategy", nargs="+",
                   help=f"{', '.join(backtest.STRATEGIES)} or path/to/file.py:ClassName; add settings as "
                   "'reversion:{\"entry_z\": -2.5}'. Give several with --portfolio")
    p.add_argument("--portfolio", action="store_true",
                   help="run the strategies together on one bankroll; the first one listed fills first")
    p.add_argument("--fill-model", choices=backtest.FILL_MODELS, default="queue",
                   help="queue: an order first waits until the stock already on offer at your side has "
                   "traded, then takes at most --participation of the hour's volume (default). "
                   "share: it takes that share from the start, as if no one were ahead of you")
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
                   help="one flat Gold fee per unit each time you post an order, on buys and sells alike")
    p.add_argument("--fee-table", action="store_true",
                   help="charge Gold as the game does, from the fee table (`fees`): each order pays the fee of "
                        "what it asks for. These are today's fees, also for a past league")
    p.add_argument("--gold-budget", type=float, help="stop posting new orders once this much Gold is spent")
    p.add_argument("--schedule", help='when you play, in UTC, e.g. "mon-fri 19-22; sat,sun 10-14" '
                   "(end hour not included); orders stand while you are away")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_backtest)

    p = commands.add_parser("search", help="try a grid of settings, judged only on leagues each choice was not tuned on")
    p.add_argument("strategy", choices=list(backtest.STRATEGIES))
    p.add_argument("--grid", required=True,
                   help="settings to try, e.g. 'reach=0.2,0.3;top=20,40;attention=1,4', or a JSON object such as "
                   "'{\"schedule\": [\"sat,sun 10-14\"]}' (needed when a value holds a comma). Besides the strategy's "
                   "own settings: attention, schedule, whole_lots, fill_model, participation, slippage, gold_per_unit")
    p.add_argument("--leagues", help="comma-separated leagues (default: every challenge league); they are put "
                   "in order of their start, and each is tuned only on leagues that ended before it began")
    p.add_argument("--score", choices=["divine", "return", "drawdown"], default="divine",
                   help="what tuning maximises: divine = return over holding Divines (default), return = Chaos "
                   "return, drawdown = return over the worst drop")
    p.add_argument("--export", metavar="FILE.json",
                   help="save the setting it would pick next, with the walk-forward table, to share")
    p.add_argument("--capital", type=float, default=10000)
    p.add_argument("--participation", type=float, default=0.1)
    p.add_argument("--slippage", type=float, default=0.03)
    p.add_argument("--attention", type=int, default=1)
    p.add_argument("--days", type=float, help="only the first days of each league, for a quicker search")
    p.add_argument("--whole-lots", action="store_true")
    p.add_argument("--gold-per-unit", type=float, default=0.0)
    p.add_argument("--fee-table", action="store_true", help="charge Gold from the fee table, as the game does")
    p.add_argument("--schedule")
    p.add_argument("--fill-model", choices=backtest.FILL_MODELS, default="queue")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=_search)

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

    p = commands.add_parser(
        "research", help="experiments: test a claim about the market against pass rules fixed in advance",
        description="Each measurement prints pass, fail or not enough data, its numbers, and how many settings "
                    "were tried. The rules are in docs/measurements.md.")
    from vaalstreet.research import MEASUREMENTS
    p.add_argument("name", choices=MEASUREMENTS + ("all",))
    p.add_argument("--league", action="append",
                   help="leagues to use (repeat); default: every challenge league, or the live one where only one is used")
    p.set_defaults(run=_research)

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
