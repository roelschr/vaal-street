"""Strategy search, judged only on leagues the search did not see (walk-forward evaluation).

A parameter sweep always finds a setting that looks great on the leagues it was fitted to;
that number says little about the next league. So the search is run the way you would have
used it at the time. Leagues are put in order of their first hour. For each test league k,
the tuning leagues are those whose last hour came *before* k's first hour: a league still
running when k started (a tail overlap, or Standard) is never used, because its later hours
are k's own market. Every setting in the grid is backtested on the tuning leagues, the best
mean tuning score is chosen (ties go to the first setting in grid order), and only then is
the chosen setting run on league k. The default setting (the strategy's own defaults) and
holding Divines run on league k too, so the search can be judged against doing nothing clever.

The report leads with those out-of-sample results, the number of settings tried (the more you
try, the luckier the best one looks) and how often the choice changed between folds. The
tuning score is shown only as "fit on earlier leagues".

A backtest of one setting on one league does not depend on the fold, so each (setting,
league) pair runs once and is reused; each league's prices are loaded once.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import math
import time
from typing import Any

import duckdb
import numpy as np

from vaalstreet import backtest
from vaalstreet.panel import HOUR

# Run options a grid may vary, besides the strategy's own parameters, and how to read them.
OPTIONS: dict[str, Any] = {
    "attention": int, "schedule": str, "whole_lots": bool, "fill_model": str,
    "participation": float, "slippage": float, "gold_per_unit": float, "compound": bool,
}
SCORES = {
    "divine": "return over holding Divines: (1 + return) / (1 + Divine's move) - 1",
    "return": "return in Chaos",
    "drawdown": "return in Chaos over the worst drop from a peak (at least 1%)",
}
MAX_SETTINGS = 200
MAX_RUNS = 2000  # settings x leagues


def score(summary: dict, how: str) -> float:
    ret = summary.get("return")
    if ret is None or not math.isfinite(ret):
        return -math.inf
    if how == "return":
        return float(ret)
    if how == "drawdown":
        return float(ret / max(summary.get("max_drawdown") or 0.0, 0.01))
    if how == "divine":
        div = summary.get("divine_return")
        return float((1 + ret) / (1 + div) - 1) if div is not None else float(ret)
    raise ValueError(f"Unknown score {how!r}; use {', '.join(SCORES)}.")


# ------------------------------------------------------------------ grid
def _value(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse_grid(spec: str | dict | None, strategy: str) -> dict[str, list]:
    """'reach=0.2,0.3;top=20,40' or a JSON object {"reach": [0.2, 0.3]}: name -> values to try.

    Values with commas or semicolons, like a schedule "sat,sun 10-14", need the JSON form."""
    if not spec:
        grid: dict = {}
    elif isinstance(spec, dict):
        grid = dict(spec)
    elif spec.strip().startswith("{"):
        try:
            grid = json.loads(spec)
        except json.JSONDecodeError as err:
            raise ValueError(f"Cannot read the grid: {err.msg}") from None
        if not isinstance(grid, dict):
            raise ValueError("The grid must be a JSON object of name: [values].")
    else:
        grid = {}
        for part in filter(None, (p.strip() for p in spec.split(";"))):
            name, sep, values = part.partition("=")
            if not sep or not name.strip():
                raise ValueError(f"Cannot read {part!r} in the grid; write name=value1,value2.")
            grid[name.strip()] = [_value(v) for v in values.split(",") if v.strip()]
    fields = {f.name for f in dataclasses.fields(backtest.STRATEGIES[strategy])}
    out = {}
    for name, values in grid.items():
        values = values if isinstance(values, list) else [values]
        if name not in fields and name not in OPTIONS:
            raise ValueError(f"{strategy} has no setting {name!r}. Settings: {', '.join(sorted(fields))}; "
                             f"run options: {', '.join(OPTIONS)}.")
        if not values:
            raise ValueError(f"No values to try for {name!r}.")
        out[name] = values
    return out


def settings(grid: dict[str, list], cap: int = MAX_SETTINGS) -> list[dict]:
    size = math.prod(len(v) for v in grid.values())
    if size > cap:
        raise ValueError(f"The grid has {size} settings; the limit is {cap}. Try fewer values per name.")
    names = list(grid)
    return [dict(zip(names, combo)) for combo in itertools.product(*grid.values())]


def _split(setting: dict, strategy: str) -> tuple[dict, dict]:
    fields = {f.name for f in dataclasses.fields(backtest.STRATEGIES[strategy])}
    params = {k: v for k, v in setting.items() if k in fields}
    options = {k: OPTIONS[k](v) if OPTIONS[k] is not bool else _bool(v) for k, v in setting.items() if k not in fields}
    return params, options


def _bool(v: Any) -> bool:
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ folds
def league_rows(con: duckdb.DuckDBPyConnection, leagues: list[str] | None = None, family: str = "challenge") -> list[dict]:
    """(league, first_hour, last_hour, family), ordered by first hour. Without `leagues`, every league of
    `family` (challenge softcore by default; events, variants and Standard are left out)."""
    columns = {c[0] for c in con.execute("DESCRIBE leagues").fetchall()}
    group = "family" if "family" in columns else "kind"  # an older leagues table has no family
    rows = [dict(zip(("league", "first_hour", "last_hour", "family"), r)) for r in con.execute(
        f"SELECT league, epoch(first_hour)::BIGINT, epoch(last_hour)::BIGINT, {group} FROM leagues"
    ).fetchall()]
    if leagues:
        known = {r["league"]: r for r in rows}
        missing = [name for name in leagues if name not in known]
        if missing:
            raise LookupError(f"No prices for league(s): {', '.join(missing)}")
        rows = [known[name] for name in dict.fromkeys(leagues)]
    else:
        rows = [r for r in rows if r["family"] == family]
    return sorted(rows, key=lambda r: (r["first_hour"], r["league"]))


def ended_before(rows: list[dict], hour: int) -> list[dict]:
    """The leagues whose last hour came before `hour`: the only ones a league starting then may be tuned on."""
    return [r for r in rows if r["last_hour"] < hour]


def folds(rows: list[dict]) -> list[tuple[dict, list[dict]]]:
    """(test league, tuning leagues) for every league that has at least one league ending before it starts."""
    out = []
    for test in rows:
        train = ended_before(rows, test["first_hour"])
        if train:
            out.append((test, train))
    return out


# ------------------------------------------------------------------ runs
class Runner:
    """Backtests a (setting, league) pair once; loads each league once."""

    def __init__(self, con, strategy: str, base: dict) -> None:
        self.con, self.strategy, self.base = con, strategy, dict(base)
        self.panels: dict[str, tuple] = {}
        self.results: dict[tuple[str, str], dict] = {}
        self.seconds: list[float] = []

    def __call__(self, league: str, setting: dict) -> dict:
        key = (league, json.dumps(setting, sort_keys=True))
        if key not in self.results:
            started = time.time()
            opts = dict(self.base)
            load = {k: opts.pop(k) for k in ("start_day", "days", "universe") if k in opts}
            gold = opts.pop("gold_per_unit", 0.0)
            if league not in self.panels:
                self.panels[league] = backtest._load(self.con, league, load.get("start_day", 0.0),
                                                     load.get("days"), load.get("universe", 150))
            panel, offset = self.panels[league]
            params, extra = _split(setting, self.strategy)
            gold = extra.pop("gold_per_unit", gold)
            opts.update(extra)
            instance, name = backtest.load_strategy(self.strategy, params)
            sim = backtest.Simulation(panel, offset=offset, **backtest._options(panel, gold, **opts))
            summary = sim.run(instance, name, params).summary()
            summary.pop("top_items", None)
            self.results[key] = summary
            self.seconds.append(time.time() - started)
        return self.results[key]


def _pick(runner: Runner, grid: list[dict], train: list[dict], how: str) -> tuple[int, float]:
    best, best_score = 0, -math.inf
    for i, setting in enumerate(grid):
        s = float(np.mean([score(runner(r["league"], setting), how) for r in train]))
        if s > best_score:  # strict: ties keep the earlier setting
            best, best_score = i, s
    return best, best_score


def choose(
    con: duckdb.DuckDBPyConnection, strategy: str, grid: dict[str, list] | str | None, before_hour: int,
    *, leagues: list[str] | None = None, how: str = "divine", **options,
) -> dict:
    """The setting the search picks for a league starting at `before_hour` (epoch seconds), tuned only
    on leagues that ended before then."""
    tried = settings(parse_grid(grid, strategy))
    train = ended_before(league_rows(con, leagues), before_hour)
    if not train:
        raise ValueError("No league ended before that hour, so there is nothing to tune on.")
    i, fit = _pick(Runner(con, strategy, options), tried, train, how)
    return {"setting": tried[i], "fit_on_earlier": fit, "tuned_on": [r["league"] for r in train]}


def walk_forward(
    con: duckdb.DuckDBPyConnection,
    strategy: str = "swing",
    grid: dict[str, list] | str | None = None,
    *,
    leagues: list[str] | None = None,
    how: str = "divine",
    cap: int = MAX_SETTINGS,
    max_runs: int = MAX_RUNS,
    **options,
) -> dict:
    """Walk-forward strategy search. `options` are `backtest.run`'s, the same for every setting
    unless the grid varies them. Returns the report (see `format_report`)."""
    if strategy not in backtest.STRATEGIES:
        raise ValueError(f"Search runs the built-in strategies: {', '.join(backtest.STRATEGIES)}.")
    if how not in SCORES:
        raise ValueError(f"Unknown score {how!r}; use {', '.join(SCORES)}.")
    grid = parse_grid(grid, strategy)
    tried = settings(grid, cap)
    rows = league_rows(con, leagues)
    if len(rows) * len(tried) > max_runs:
        raise ValueError(f"{len(tried)} settings x {len(rows)} leagues is {len(rows) * len(tried)} backtests; "
                         f"the limit is {max_runs}. Try fewer values or fewer leagues.")
    plan = folds(rows)
    if not plan:
        raise ValueError("Walk-forward needs at least two leagues, one ending before the next starts.")
    started = time.time()
    runner = Runner(con, strategy, options)
    default = {}
    out_folds = []
    for test, train in plan:
        i, fit = _pick(runner, tried, train, how)
        chosen = runner(test["league"], tried[i])
        base = runner(test["league"], default)
        out_folds.append({
            "league": test["league"],
            "tuned_on": [r["league"] for r in train],
            "setting": tried[i],
            "fit_on_earlier": round(fit, 4) if math.isfinite(fit) else None,
            "score": round(score(chosen, how), 4),
            "default_score": round(score(base, how), 4),
            "return": chosen["return"], "default_return": base["return"],
            "divine_return": chosen["divine_return"],
            "max_drawdown": chosen["max_drawdown"], "days": chosen["days"], "fills": chosen["fills"],
        })
    # What the search would pick now for the next league, tuned on every league that has ended.
    i, fit = _pick(runner, tried, rows, how)
    changes = sum(a["setting"] != b["setting"] for a, b in zip(out_folds, out_folds[1:]))
    n = len(out_folds)
    seconds = runner.seconds
    return {
        "strategy": strategy,
        "score": how,
        "score_means": SCORES[how],
        "grid": grid,
        "settings_tried": len(tried),
        "options": backtest.describe_options(options),
        "folds": out_folds,
        "pooled": {
            "folds": n,
            "score": round(float(np.mean([f["score"] for f in out_folds])), 4),
            "default_score": round(float(np.mean([f["default_score"] for f in out_folds])), 4),
            "return": round(float(np.mean([f["return"] for f in out_folds])), 4),
            "default_return": round(float(np.mean([f["default_return"] for f in out_folds])), 4),
            "divine_return": _mean([f["divine_return"] for f in out_folds]),
            "beat_default": sum(f["score"] > f["default_score"] + 0.001 for f in out_folds),  # by over 0.1 point
        },
        "stability": {"changes": changes, "of": max(n - 1, 0)},
        "next": {"setting": tried[i], "fit_on_earlier": round(fit, 4) if math.isfinite(fit) else None,
                 "tuned_on": [r["league"] for r in rows]},
        "backtests": len(seconds),
        "seconds": round(time.time() - started, 1),
        "seconds_per_backtest": round(float(np.mean(seconds)), 2) if seconds else None,
    }


def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return round(float(np.mean(values)), 4) if values else None


# ------------------------------------------------------------------ report
def _setting(s: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in s.items()) or "defaults"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1%}"


def format_report(rep: dict) -> str:
    p = rep["pooled"]
    lines = [
        f"Strategy search for {rep['strategy']}: {rep['settings_tried']} settings tried, "
        f"each judged on leagues it was not tuned on.",
        f"Score: {rep['score_means']}.",
        "",
        f"{'Test league':<16}{'Score':>8}{'Default':>9}{'Return':>9}{'Hold Div':>10}  Chosen setting"
        f"  (fit on earlier leagues)",
    ]
    for f in rep["folds"]:
        lines.append(f"{f['league'][:15]:<16}{_pct(f['score']):>8}{_pct(f['default_score']):>9}{_pct(f['return']):>9}"
                     f"{_pct(f['divine_return']):>10}  {_setting(f['setting'])}"
                     f"  ({_pct(f['fit_on_earlier'])} on {', '.join(f['tuned_on'])})")
    st = rep["stability"]
    lines += [
        "",
        f"Out of sample, {p['folds']} league(s): the search scored {_pct(p['score'])} on average, the default "
        f"setting {_pct(p['default_score'])}; the search beat the default by more than 0.1 point in {p['beat_default']} of {p['folds']}. "
        f"Return {_pct(p['return'])} against {_pct(p['default_return'])} for the default and "
        f"{_pct(p['divine_return'])} for holding Divines.",
        f"The chosen setting changed {st['changes']} time(s) in {st['of']} step(s) between leagues"
        + (" (a choice that keeps changing is mostly luck)." if st["changes"] else "."),
        f"For the next league it would pick {_setting(rep['next']['setting'])} "
        f"(fit on earlier leagues {_pct(rep['next']['fit_on_earlier'])}; not yet tested on any league it did not see).",
        "The fit on earlier leagues is the best of many tries, so it flatters; judge the search by the Score column.",
        f"{rep['backtests']} backtests in {rep['seconds']:g} s, about {rep['seconds_per_backtest']} s each.",
    ]
    return "\n".join(lines)


def export_rows(rep: dict) -> list[dict]:
    """The out-of-sample results as strategy-file results (league, return, days, fills per row)."""
    return [{"league": f["league"], "return": f["return"], "days": f["days"], "fills": f["fills"],
             "params": f["setting"], "out_of_sample": True} for f in rep["folds"]]
