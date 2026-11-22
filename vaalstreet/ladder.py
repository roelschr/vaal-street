"""The bankroll ladder and the compounding plan: from a few dozen Chaos to a first Divine and beyond.

The desk already knows how to re-cut flips to a purse (`Desk._fit`), which flips to run in one
sitting (`session_plan`), and which conversions and round trips a purse can start. The ladder runs
all of that at a series of purse sizes and reports, per step, what opens up that the step below
could not run. That difference is the useful part: it tells a small trader what the next few
hundred Chaos buy them, not just that bigger purses earn more.

Promised against replayed
-------------------------
A session plan's promised rate is what the current order levels would earn if the last two days
repeat: each pick's margin on the flow it can take, or on its stake turned over once per buy and
sell, whichever is smaller. Next to it we put what the same rule actually made over the last week
at the pick's own stake (`Desk.replay_at`: the backtest simulator, whole lots, a visit every four
hours), and what that is worth against simply holding Divines over the same hours. On a live
league the promise runs far ahead of the replay: levels drawn from two volatile days are wider than
the market keeps offering, and at a league start the Divine rises fast, so Chaos-denominated gains
shrink when measured in Divines. The replayed figure against Divines is the one plans are judged
by: the compounding plan grows the purse at the lower of the promise and that figure for each pick.
It is still a projection from recent rates.

Causality: everything here reads the desk as of its latest hour; nothing looks ahead.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from vaalstreet.desk import Costs, Desk, _num, session_plan
from vaalstreet.desk import replayed_rate as desk_replayed_rate

CHAOS_RUNGS = (25, 50, 100, 200, 400)
DIVINE_RUNGS = (1, 2, 5, 10)
MAX_SEGMENTS = 50
CONVERSION_CYCLE = 2.0  # hours to buy a conversion's inputs and sell what they make, at the least

FLIP_KIND = {
    "chaos": "flip in the Chaos market",
    "divine": "flip in the Divine market",
    "cross": "buy for Chaos, sell for Divines",
}
GROUP = {
    "chaos": "Chaos-market flips", "divine": "Divine-market flips", "cross": "Chaos-to-Divine flips",
    "conversion": "conversions", "loop": "round trips",
}


def rungs(bridge: float, chaos: tuple = CHAOS_RUNGS, divines: tuple = DIVINE_RUNGS) -> list[dict]:
    """Purse sizes in Chaos, smallest first. Divine rungs are converted at the current Divine price,
    so they can fall between the Chaos rungs; rungs closer than 1 Chaos are merged."""
    out = [{"label": f"{c:g} Chaos", "chaos": float(c)} for c in chaos]
    if bridge and math.isfinite(bridge):
        out += [{"label": f"{d:g} Divine{'s' if d != 1 else ''}", "chaos": round(d * bridge, 2)} for d in divines]
    out.sort(key=lambda r: r["chaos"])
    merged: list[dict] = []
    for r in out:
        if merged and r["chaos"] - merged[-1]["chaos"] < 1:
            continue
        merged.append(r)
    return merged


def replayed_rate(pick: dict, against_divine: bool = True) -> float | None:
    """Chaos an hour this pick's rule made over the last week, at its stake. None if never replayed.

    Uses the replay at the pick's own stake when there is one (`replay_at_stake`), else the fixed
    1,000 Chaos replay scaled to the stake."""
    return desk_replayed_rate(pick.get("replay_at_stake") or pick.get("replay"), pick["stake"], against_divine)


def _flip_option(p: dict) -> dict:
    return {"key": f"flip:{p['kind']}:{p['id']}", "group": GROUP[p["kind"]],
            "label": f"{p['name']} ({FLIP_KIND[p['kind']]})", "lot_cost": p.get("lot_cost")}


def _count(x: Any) -> str:
    """A count of items as players write it: 2, not 2.0; 2.5 stays 2.5."""
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def _conversion_option(c: dict) -> dict:
    key = c.get("recipe_key") or f"{c['id']}:{c.get('reward_id')}:{c.get('need')}"
    return {"key": f"conversion:{key}", "group": GROUP["conversion"],
            "label": f"{_count(c.get('need'))} {c['name']} into {_count(c.get('gives'))} {c.get('reward')}",
            "lot_cost": c.get("cost")}


def counts_text(counts: dict) -> str:
    """"1 flip, 3 conversions and 0 round trips", with each noun matching its count."""
    words = [("flips", "flip", "flips"), ("conversions", "conversion", "conversions"),
             ("round_trips", "round trip", "round trips")]
    parts = [f"{counts.get(k, 0)} {one if counts.get(k, 0) == 1 else many}" for k, one, many in words]
    return f"{parts[0]}, {parts[1]} and {parts[2]}"


def conversion_rate(conversions: list[dict], purse: float, cycle: float = CONVERSION_CYCLE) -> float:
    """Chaos an hour the conversions alone could make with `purse`.

    Each conversion's own profit per hour assumes its full share of the market's flow, whatever it
    costs to buy. A purse buys inputs worth at most itself once per `cycle` hours (buy the inputs,
    then sell what they make), so the purse is spent on the best margins first and the rest get
    what is left.
    """
    budget = purse / cycle
    total = 0.0
    ranked = sorted((c for c in conversions if (c.get("cost") or 0) > 0 and (c.get("sets_per_hour") or 0) > 0),
                    key=lambda c: -(c["profit_per_hour"] / c["sets_per_hour"]) / c["cost"])
    for c in ranked:
        if budget <= 0:
            break
        sets = min(c["sets_per_hour"], budget / c["cost"])
        total += sets * c["profit_per_hour"] / c["sets_per_hour"]
        budget -= sets * c["cost"]
    return total


def _loop_option(row: dict) -> dict:
    path = row.get("path") or []
    return {"key": "loop:" + ">".join(step["id"] for step in path), "group": GROUP["loop"],
            "label": "round trip " + " > ".join(step["name"] for step in path), "lot_cost": row.get("lot_cost")}


class Ladder:
    """Everything the desk can run at a given purse, computed lazily and kept per purse.

    Conversions and round trips do not depend on the purse beyond whether one lot fits (and, for
    round trips, whether the purse can hold a Divine at all), so they are computed once.
    """

    def __init__(self, desk: Desk, costs: Costs | None = None, share: float = 0.1, min_margin: float = 0.02,
                 fixed: list[dict] | None = None):
        self.desk, self.costs, self.share, self.min_margin = desk, costs or Costs(), share, min_margin
        self.fixed = fixed  # your own recipes and vendor prices (`Ledger.fixed_conversions`), as on Arbitrage
        self._at: dict[float, dict] = {}
        self._conversions: list[dict] | None = None
        self._loops: dict[bool, list[dict]] = {}

    def conversions(self) -> list[dict]:
        if self._conversions is None:
            self._conversions = [c for c in self.desk.conversions(share=self.share, fixed=self.fixed, costs=self.costs)
                                 if (c.get("profit_per_hour") or 0) > 0]
        return self._conversions

    def loops(self, chaos_only: bool) -> list[dict]:
        if chaos_only not in self._loops:
            self._loops[chaos_only] = self.desk.loops(chaos_only=chaos_only, share=self.share, fixed=self.fixed,
                                                     costs=self.costs)
        return self._loops[chaos_only]

    def at(self, purse: float) -> dict:
        """What a purse of `purse` Chaos can run, and what its session plan earns."""
        purse = round(float(purse), 2)
        if purse in self._at:
            return self._at[purse]
        flips = self.desk.flips(share=self.share, min_margin=self.min_margin, bankroll=purse, costs=self.costs)
        session = session_plan(flips, purse)
        fits = lambda cost: cost is not None and 0 < cost <= purse  # noqa: E731
        conversions = [c for c in self.conversions() if fits(c.get("cost"))]
        # Like the Arbitrage page: a purse that cannot hold one Divine stays in the Chaos markets.
        loops = [row for row in self.loops(purse < self.desk.bridge) if fits(row.get("lot_cost"))]
        picks, promised, replayed, against, cautious = [], 0.0, 0.0, 0.0, 0.0
        divines = None
        gold: float | None = 0.0
        for p, at_stake in zip(session["flips"], self.desk.replay_at(session["flips"], self.share)):
            p = p | {"replay_at_stake": at_stake}
            rep, vs = replayed_rate(p, against_divine=False), replayed_rate(p)
            promised += p["est"]
            replayed += rep or 0.0
            against += vs or 0.0
            # What the compounding plan grows the purse at: the promise, unless last week did worse
            # than holding Divines would have.
            cautious += min(p["est"], vs) if vs is not None else 0.0
            if at_stake and at_stake.get("divines") is not None:
                divines = at_stake["divines"]
            gold = None if gold is None or p.get("gold_per_hour") is None else gold + p["gold_per_hour"]
            picks.append({"id": p["id"], "name": p["name"], "kind": p["kind"],
                          "label": f"{p['name']} ({FLIP_KIND[p['kind']]})", "stake": round(p["stake"], 2),
                          "expected_per_hour": round(p["est"], 4),
                          "replayed_per_hour": None if rep is None else round(rep, 4),
                          "replayed_vs_divine_per_hour": None if vs is None else round(vs, 4),
                          "replay": p.get("replay"), "replay_at_stake": at_stake})
        options = ([_flip_option(p) for p in flips] + [_conversion_option(c) for c in conversions]
                   + [_loop_option(row) for row in loops])
        out = {
            "purse": purse,
            "can_run": len(options),
            "counts": {"flips": len(flips), "conversions": len(conversions), "round_trips": len(loops)},
            "counts_text": counts_text({"flips": len(flips), "conversions": len(conversions), "round_trips": len(loops)}),
            "options": options,
            "session": {"picks": picks, "expected_per_hour": round(promised, 4),
                        "replayed_per_hour": round(replayed, 4),
                        "replayed_vs_divine_per_hour": round(against, 4),
                        "divines": divines,  # what holding Divines made over the replayed hours
                        "rate": round(max(cautious, 0.0), 4),
                        "gold_per_hour": None if gold is None or not picks else round(gold, 2),
                        "minutes": session["minutes"]},
            "conversion_per_hour": round(conversion_rate(conversions, purse), 4),
        }
        self._at[purse] = out
        return out


def ladder(desk: Desk, costs: Costs | None = None, share: float = 0.1, min_margin: float = 0.02,
           steps: list[dict] | None = None, runner: Ladder | None = None) -> list[dict]:
    """One row per rung: what can run, what opens up compared with the rung below, the session plan's
    promised and replayed Chaos per hour, and hours to the next rung at the cautious rate."""
    runner = runner or Ladder(desk, costs, share, min_margin)
    steps = steps or rungs(desk.bridge)
    rows, seen, groups = [], set(), set()
    for k, step in enumerate(steps):
        info = runner.at(step["chaos"])
        opens = [o for o in info["options"] if o["key"] not in seen]
        new_groups = sorted({o["group"] for o in info["options"]} - groups)
        seen |= {o["key"] for o in info["options"]}
        groups |= {o["group"] for o in info["options"]}
        rate = info["session"]["rate"]
        nxt = steps[k + 1]["chaos"] if k + 1 < len(steps) else None
        rows.append({
            **step, "can_run": info["can_run"], "counts": info["counts"], "counts_text": info["counts_text"],
            "opens": opens, "opens_groups": new_groups,
            "session": info["session"], "conversion_per_hour": info["conversion_per_hour"],
            "next": nxt,
            "hours_to_next": round((nxt - step["chaos"]) / rate, 2) if nxt is not None and rate > 0 else None,
        })
    return rows


def compound(start: float, steps: list[float], rate_at: Callable[[float], tuple[float, Any]],
             hours: float | None = None, target: float | None = None) -> list[dict]:
    """Step a purse up the ladder at the rate each purse earns, switching plans at every rung.

    `rate_at(purse)` returns (Chaos per hour, plans). The rate at a segment's starting purse is used
    for the whole segment, so the projection is a little pessimistic within a step. Stops at `hours`,
    at `target`, when nothing earns, or after `MAX_SEGMENTS` switches. Hours are None where the purse
    never gets there.
    """
    if hours is None and target is None:
        hours = 6.0
    segments: list[dict] = []
    t, purse = 0.0, float(start)
    while len(segments) < MAX_SEGMENTS:
        rate, plans = rate_at(purse)
        above = [s for s in steps if s > purse + 1e-9]
        goal = min(above) if above else None
        if target is not None and target > purse and (goal is None or target < goal):
            goal = target
        seg = {"from_hour": round(t, 2), "purse": round(purse, 2), "rate": round(rate, 4), "plans": plans}
        if rate <= 0:
            seg.update(to_hour=None, end_purse=round(purse, 2), switch_at=None, reason="nothing earns at this purse")
            segments.append(seg)
            break
        dt = (goal - purse) / rate if goal is not None else math.inf
        if hours is not None and t + dt >= hours:
            seg.update(to_hour=round(hours, 2), end_purse=round(purse + rate * (hours - t), 2), switch_at=None,
                       next_rung=goal, hours_to_next=None if goal is None else round(dt - (hours - t), 2),
                       reason="session ends")
            segments.append(seg)
            break
        if goal is None:  # top of the ladder and no session length: nothing more to plan
            seg.update(to_hour=None, end_purse=None, switch_at=None, reason="top of the ladder")
            segments.append(seg)
            break
        t += dt
        seg.update(to_hour=round(t, 2), end_purse=round(goal, 2), switch_at=round(goal, 2), reason="next rung")
        segments.append(seg)
        purse = goal
        if target is not None and purse >= target - 1e-9:
            segments[-1]["reason"] = "target reached"
            break
    return segments


def plan(desk: Desk, start: float, hours: float | None = None, target: float | None = None,
         costs: Costs | None = None, share: float = 0.1, min_margin: float = 0.02,
         fixed: list[dict] | None = None) -> dict:
    """The ladder plus a compounding schedule from `start` Chaos, as one dict for the API and CLI."""
    runner = Ladder(desk, costs, share, min_margin, fixed)
    steps = rungs(desk.bridge)

    def rate_at(purse: float) -> tuple[float, list[dict]]:
        info = runner.at(purse)["session"]
        return info["rate"], [{"name": p["name"], "kind": p["kind"], "label": p["label"], "stake": p["stake"]}
                              for p in info["picks"]]

    schedule = compound(start, [s["chaos"] for s in steps], rate_at, hours=hours, target=target)
    # No Divine price yet (the first hour of a league, or a league that never trades Divines) is
    # None, not NaN: NaN is not JSON, and the browser would refuse the whole answer.
    return {"bridge": _num(desk.bridge), "start": start, "hours": hours, "target": target,
            "ladder": ladder(desk, costs, share, min_margin, steps, runner),
            "schedule": schedule, "schedule_text": describe(schedule),
            "note": ("A projection, not a promise. The purse grows at the lower of what each plan promises now "
                     "and what the same plan made over the last week at its own stake, measured against "
                     "simply holding Divines over the same hours.")}


def _hours(h: float) -> str:
    return f"{h:.1f}".rstrip("0").rstrip(".")


def describe(schedule: list[dict]) -> list[str]:
    """The schedule in plain words, one line per segment."""
    lines = []
    for k, seg in enumerate(schedule):
        names = ", ".join(p["label"] for p in seg["plans"]) or "nothing"
        start = f"{_hours(seg['from_hour'])}"
        if seg["rate"] <= 0:
            lines.append(f"from {start} h: at {seg['purse']:,.0f} Chaos no flip both fits and beat holding Divines "
                         "last week; nothing to compound.")
            continue
        if seg["to_hour"] is None:
            lines.append(f"from {start} h: run {names} at {seg['purse']:,.0f} Chaos, about {seg['rate']:.2f} c an hour; "
                         "this is the top of the ladder.")
            continue
        span = f"{start}–{_hours(seg['to_hour'])} h"
        line = f"{span}: run {names} at {seg['purse']:,.0f} Chaos, about {seg['rate']:.2f} c an hour"
        if seg["switch_at"] is not None:
            after = schedule[k + 1]["plans"] if k + 1 < len(schedule) else None
            same = after is not None and [p["label"] for p in after] == [p["label"] for p in seg["plans"]]
            line += (f"; at about {seg['switch_at']:,.0f} Chaos, " + ("keep the same plans with bigger stakes."
                     if same else "switch plans."))
        else:
            line += f"; you end near {seg['end_purse']:,.0f} Chaos"
            if seg.get("next_rung") is not None and seg.get("hours_to_next") is not None:
                line += f", about {_hours(seg['hours_to_next'])} more hours from the next step ({seg['next_rung']:,.0f} Chaos)."
            else:
                line += "."
        lines.append(line)
    return lines
