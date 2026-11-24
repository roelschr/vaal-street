"""Your records as CSV files, for a spreadsheet.

The ledger's tables (trades, positions, orders, items you made, and the stash) can be saved as
CSV from the Ledger page or with `vaalstreet export`. The rows come from what the page's own API
answers (`App.export`), so a number in the file is the number on the page. This module only
writes them: it is a file on your machine, and nothing is uploaded.

Two details make the files open right in a spreadsheet. They start with a UTF-8 byte order mark,
without which Excel reads item names with accents as gibberish. And a text cell that starts with
`=`, `+`, `-` or `@` gets a leading apostrophe: a note is free text, and a spreadsheet would
otherwise run "=HYPERLINK(...)" typed into a note as a formula.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

KINDS = ("trades", "positions", "orders", "made", "stash")
HEADERS = {
    "trades": ["Time (UTC)", "Unix time", "Item", "Item id", "Side", "Units", "Price each (Chaos)", "Gold", "Note",
               "Order id", "Units with no recorded cost"],
    "positions": ["Item", "Item id", "Units", "Average cost (Chaos)", "Fair value now (Chaos)", "Value (Chaos)",
                  "Unrealised (Chaos)", "Realised (Chaos)", "Of which loot (Chaos)"],
    "orders": ["Created (UTC)", "Item", "Item id", "Side", "Market", "Ratio items", "Ratio currency", "Units", "Filled",
               "Gold", "Status"],
    "made": ["Time (UTC)", "Used", "Got", "Note"],
    "stash": ["Item", "Item id", "Units", "Last counted (UTC)", "Worth (Chaos)", "Low (Chaos)", "High (Chaos)",
              "Chaos-market value (Chaos)", "Divine-market value (Chaos)", "Better market"],
}
# Characters a spreadsheet reads as the start of a formula, and the tab and carriage return that
# some spreadsheets strip before looking.
_FORMULA = ("=", "+", "-", "@", "\t", "\r")
BOM = "﻿"


def iso(t: float | None) -> str:
    """A unix time as ISO 8601 in UTC ("2026-11-24T18:00:00Z"); empty for no time."""
    return "" if t is None else datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(value) -> object:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA):
        return "'" + value
    return value


def text(what: str, rows: list[list]) -> str:
    """The table as CSV text (without the byte order mark), its header first."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADERS[what])
    writer.writerows([_cell(v) for v in row] for row in rows)
    return out.getvalue()


def filename(league: str, what: str, practice: bool = False) -> str:
    """`vaalstreet-<league>-<what>.csv`, with "practice" in it during a rehearsal. A league name can hold
    spaces and brackets ("Dead (PL1)"), which do not belong in a file name or a header."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", league).strip("-") or "league"
    return f"vaalstreet-{safe}{'-practice' if practice else ''}-{what}.csv"
