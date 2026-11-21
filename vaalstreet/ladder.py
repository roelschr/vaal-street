"""The bankroll ladder and the compounding plan: from a few dozen Chaos to a first Divine and beyond.

The desk already knows how to re-cut flips to a purse (`Desk._fit`), which flips to run in one
sitting (`session_plan`), and which conversions and round trips a purse can start. The ladder runs
all of that at a series of purse sizes and reports, per step, what opens up that the step below
could not run. That difference is the useful part: it tells a small trader what the next few
hundred Chaos buy them, not just that bigger purses earn more.

Promised against replayed
-------------------------
A session plan's expected rate is what the current order levels promise. Next to it we put what the
same rule actually made over the last week (`Desk.replay`). Replay returns are measured on a fixed
stake of `REPLAY_STAKE` Chaos per item, so a pick's replayed rate is that weekly return times its
stake (capped at the replay stake, since a bigger stake was never tested) spread over the week.
Scaling a return measured on 1,000 Chaos down to a 25 Chaos stake is an approximation: small lots
round worse. The compounding plan grows the purse at the lower of the two rates for each pick, so it
leans toward what worked rather than what is promised. It is still a projection from recent rates.

Causality: everything here reads the desk as of its latest hour; nothing looks ahead.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from vaalstreet.desk import DAY, REPLAY_DAYS, REPLAY_STAKE, Costs, Desk, session_plan

CHAOS_RUNGS = (25, 50, 100, 200, 400)
DIVINE_RUNGS = (1, 2, 5, 10)
MAX_SEGMENTS = 50

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


def replayed_rate(pick: dict) -> float | None:
    """Chaos an hour this pick's rule made over the last week, at its stake. None if never replayed."""
    rep = pick.get("replay") or {}
    if rep.get("return") is None:
        return None
    return rep["return"] * min(pick["stake"], REPLAY_STAKE) / (REPLAY_DAYS * DAY)


def _flip_option(p: dict) -> dict:
    return {"key": f"flip:{p['kind']}:{p['id']}", "group": GROUP[p["kind"]],
            "label": f"{p['name']} ({FLIP_KIND[p['kind']]})", "lot_cost": p.get("lot_cost")}


def _conversion_option(c: dict) -> dict:
    key = c.get("recipe_key") or f"{c['id']}:{c.get('reward_id')}:{c.get('need')}"
    return {"key": f"conversion:{key}", "group": GROUP["conversion"],
            "label": f"{c.get('need')} {c['name']} into {c.get('gives')} {c.get('reward')}", "lot_cost": c.get("cost")}


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
        picks, promised, replayed, cautious = [], 0.0, 0.0, 0.0
        gold: float | None = 0.0
        for p in session["flips"]:
            rep = replayed_rate(p)
            low = min(p["est"], rep) if rep is not None else 0.0
            promised += p["est"]
            replayed += rep or 0.0
            cautious += low
            gold = None if gold is None or p.get("gold_per_hour") is None else gold + p["gold_per_hour"]
            picks.append({"id": p["id"], "name": p["name"], "kind": p["kind"],
                          "label": f"{p['name']} ({FLIP_KIND[p['kind']]})", "stake": round(p["stake"], 2),
                          "expected_per_hour": round(p["est"], 4),
                          "replayed_per_hour": None if rep is None else round(rep, 4),
                          "replay": p.get("replay")})
        options = ([_flip_option(p) for p in flips] + [_conversion_option(c) for c in conversions]
                   + [_loop_option(row) for row in loops])
        out = {
            "purse": purse,
            "can_run": len(options),
            "counts": {"flips": len(flips), "conversions": len(conversions), "round_trips": len(loops)},
            "options": options,
            "session": {"picks": picks, "expected_per_hour": round(promised, 4),
                        "replayed_per_hour": round(replayed, 4),
                        # What the compounding plan grows the purse at: per pick, the lower of the two.
                        "rate": round(max(cautious, 0.0), 4),
                        "gold_per_hour": None if gold is None or not picks else round(gold, 2),
                        "minutes": session["minutes"]},
            "conversion_per_hour": round(sum(c["profit_per_hour"] for c in conversions), 4),
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
            **step, "can_run": info["can_run"], "counts": info["counts"],
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
    return {"bridge": desk.bridge, "start": start, "hours": hours, "target": target,
            "ladder": ladder(desk, costs, share, min_margin, steps, runner),
            "schedule": schedule, "schedule_text": describe(schedule),
            "note": ("A projection, not a promise. The purse grows at the lower of what each plan promises now "
                     "and what the same plan made over the last week, replayed on recent prices.")}


def _hours(h: float) -> str:
    return f"{h:.1f}".rstrip("0").rstrip(".")


def describe(schedule: list[dict]) -> list[str]:
    """The schedule in plain words, one line per segment."""
    lines = []
    for k, seg in enumerate(schedule):
        names = ", ".join(p["label"] for p in seg["plans"]) or "nothing"
        start = f"{_hours(seg['from_hour'])}"
        if seg["rate"] <= 0:
            lines.append(f"from {start} h: at {seg['purse']:,.0f} Chaos no flip has both fitted and made money "
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
