"""Command line: python -m vaalstreet <command>."""

from __future__ import annotations

import argparse
import os
import sys

from vaalstreet import archive, items
from vaalstreet.db import connect
from vaalstreet.ingest import ingest
from vaalstreet.paths import REALMS, data_dir


def _archive(args: argparse.Namespace) -> None:
    base_url = os.environ.get("ARCHIVE_BASE_URL", archive.DEFAULT_BASE_URL)
    for realm in args.realms or REALMS:
        archive.archive_realm(data_dir(), realm, base_url=base_url, delay=args.delay)


def _items(args: argparse.Namespace) -> None:
    for realm in REALMS:
        print(f"[{realm}] {items.download(data_dir(), realm):,} item names saved")


def _ingest(args: argparse.Namespace) -> None:
    rebuilt = ingest(data_dir())
    if not rebuilt:
        print("No raw data found. Run `python -m vaalstreet archive` first.")
    elif not any(rebuilt.values()):
        print("Parquet is already up to date.")


def _status(args: argparse.Namespace) -> None:
    con = connect()
    con.sql(
        """
        SELECT realm, count(DISTINCT league) AS leagues, min(first_hour) AS first_hour,
               max(last_hour) AS last_hour, max(hours) AS hours
        FROM leagues GROUP BY realm ORDER BY realm
        """
    ).show()
    con.sql(
        """
        SELECT realm, league, first_hour, last_hour, hours
        FROM leagues WHERE NOT is_private
        ORDER BY realm, first_hour DESC, hours DESC LIMIT 40
        """
    ).show(max_rows=40)


def _price(args: argparse.Namespace) -> None:
    con = connect()
    con.sql(
        """
        SELECT hour, name, round(price, 4) AS price, round(price_lo, 4) AS low,
               round(price_hi, 4) AS high, volume, round(volume_ref) AS volume_ref,
               split_part(via, '/', -1) AS via
        FROM prices
        WHERE realm = $realm AND league = $league
          AND (name ILIKE $item OR item ILIKE '%' || $item)
        ORDER BY hour DESC, volume_ref DESC LIMIT $rows
        """,
        params={"realm": args.realm, "league": args.league, "item": args.item, "rows": args.rows},
    ).show(max_rows=args.rows, max_width=200)


def _sql(args: argparse.Namespace) -> None:
    connect().sql(args.query).show(max_rows=200, max_width=220)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vaalstreet", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("archive", help="download exchange history not yet on disk")
    p.add_argument("realms", nargs="*", choices=REALMS, help="default: both")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between requests (default 2)")
    p.set_defaults(run=_archive)

    commands.add_parser("items", help="download readable item names").set_defaults(run=_items)
    commands.add_parser("ingest", help="convert new raw hours to Parquet").set_defaults(run=_ingest)
    commands.add_parser("status", help="show what the archive covers").set_defaults(run=_status)

    p = commands.add_parser("price", help="latest hourly prices for an item")
    p.add_argument("item", help='item name or ID ending, e.g. "Divine Orb"')
    p.add_argument("--league", required=True)
    p.add_argument("--realm", choices=REALMS, default="poe1")
    p.add_argument("--rows", type=int, default=24)
    p.set_defaults(run=_price)

    p = commands.add_parser("sql", help="run a query against the views")
    p.add_argument("query")
    p.set_defaults(run=_sql)

    args = parser.parse_args(argv)
    try:
        args.run(args)
    except (FileNotFoundError, RuntimeError) as err:
        sys.exit(str(err))
    except KeyboardInterrupt:
        sys.exit("Stopped. Run the same command again to resume.")


if __name__ == "__main__":
    main()
