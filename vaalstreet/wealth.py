"""Your worth over time: snapshots of what you hold, each valued at its own hour and never again.

The desk knows two pictures of what you hold, the stash list you entered and the positions your
recorded trades add up to, but neither has a history. A snapshot keeps both for one league at one
data hour: every item's units and its fair value at that hour (None where the desk has none), the
worth of each picture, the profit you had realised so far and that hour's Divine price.

Causality. A snapshot is valued with the desk as it stood at its hour, and what it stored is what
every chart and comparison reads later. Later hours, and later corrections of the model, never
reach back into it; that is what makes a chart of snapshots an honest record of what you saw.

Each snapshot also prices the items you held at the league's previous snapshot, or ever traded,
but no longer hold (`known`). Without that, an item you sold between two snapshots would have no
price at the later one, and the comparison of two snapshots could not say what the change is worth.

The worth of each picture is `check.stash_value`'s total, so it agrees with the Stash panel.
"""

from __future__ import annotations

from vaalstreet import check
from vaalstreet.desk import Desk, _num
from vaalstreet.ledger import Ledger

SOURCES = ("stash", "ledger")
_EPS = 1e-9


def holdings(ledger: Ledger, league: str) -> tuple[dict[str, float], dict[str, float], dict[str, dict]]:
    """(stash lines, ledger positions held, all positions with their profit) for one league."""
    positions = ledger.positions(league)
    held = {item: p["units"] for item, p in positions.items() if p["units"] > _EPS}
    return ledger.stash(league), held, positions


def _price(desk: Desk, item_id: str) -> float | None:
    """The fair value `check.stash_value` would give the item, rounded as it rounds."""
    fr = desk.fair_range(item_id)
    return _num(fr[1]) if fr else None


def take(ledger: Ledger, desk: Desk, kind: str = "auto", note: str = "") -> int | None:
    """Value what the ledger holds in `desk.league` at the desk's hour and store it. Returns the new id.

    None when there is nothing to picture (no stash line and no position held), or when an auto
    snapshot of this hour is already stored.
    """
    league = desk.league
    stash, held, positions = holdings(ledger, league)
    if not stash and not held:
        return None
    pictures = {}
    for key, units in (("stash", stash), ("positions", held)):
        valued = check.stash_value(desk, units)
        fair = {row["id"]: row.get("fair") for row in valued["rows"]}
        pictures[key] = ({item: [float(n), fair.get(item)] for item, n in units.items()},
                         valued["total"]["value"], valued["unpriced"])
    previous = ledger.last_snapshot(league)
    earlier = set(positions)
    if previous:
        earlier |= set(previous["stash"]) | set(previous["positions"]) | set(previous["known"])
    known = {item: _price(desk, item) for item in sorted(earlier - set(stash) - set(held))}
    bridge = _num(desk.bridge)
    return ledger.add_snapshot(
        league, desk.as_of, kind, stash=pictures["stash"][0], positions=pictures["positions"][0], known=known,
        bridge=bridge, stash_value=pictures["stash"][1], positions_value=pictures["positions"][1],
        stash_unpriced=pictures["stash"][2], positions_unpriced=pictures["positions"][2],
        realized=_num(sum(p["realized"] for p in positions.values())),
        loot=_num(sum(p["loot"] for p in positions.values())), note=note)


def chart_rows(ledger: Ledger, league: str, source: str = "stash") -> list[dict]:
    """The league's snapshots for one picture, oldest first: when, the Divine price, the worth, and the profit
    realised so far. Read as stored; nothing is valued again."""
    if source not in SOURCES:
        raise ValueError("source must be stash or ledger.")
    key = "stash" if source == "stash" else "positions"
    return [{"id": r["id"], "t": r["t"], "hour": r["hour"], "kind": r["kind"], "note": r["note"],
             "bridge": r["bridge"], "value": r[f"{key}_value"], "unpriced": r[f"{key}_unpriced"],
             "realized": r["realized"], "loot": r["loot"]} for r in ledger.snapshots(league)]
