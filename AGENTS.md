# AGENTS.md

Instructions for coding agents working in this repository.

The README describes what the desk does and how its numbers are made. Read its "How the numbers are made" section before you change pricing, flips, arbitrage, league analytics or backtests, and its "Files" section for the module map. This file covers how to work here. It does not repeat the README, so that the two cannot drift apart.

If you ever change anything relevant, make sure to keep this file up to date.

## The project

Vaal Street is a **hobbyist project** for a local trading desk for the Path of Exile Currency Exchange. It is one Python package, `vaalstreet/`. The package mirrors GGG's public hourly exchange digests into a Parquet archive, queries the archive with DuckDB, and serves a browser desk on localhost. The runtime dependencies are `duckdb`, `pyarrow` and `numpy`.

The web UI in `vaalstreet/web/` is plain ES modules and CSS, served as static files by `vaalstreet/server.py`. There is no JavaScript build step and no `package.json`.

PoE 1 is the target. The data layer also mirrors PoE 2, but the desk and the models are built and tested against PoE 1 only.

## Commands

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'     # keep the quotes: zsh treats an unquoted .[dev] as a glob
pytest -q                   # runs in a few seconds and uses no network
python -m vaalstreet -h     # the CLI; every subcommand also takes -h
```

pytest comes with the `dev` extras. There is no linter or formatter configured.

To try the CLI or the server without touching the user's archive, set `VAAL_DATA_DIR` to a temporary directory. Note that `serve` and `sync` download any missing hours from the live exchange. With an empty data directory, `serve` starts by downloading the last two weeks, which takes a couple of minutes. For most checks, the pytest suite is enough.

## Steps you can take without asking

- Read any file in the repository.
- Edit code, tests, docs and examples.
- Create a virtualenv and install the project and its dev extras into it.
- Run pytest, and run the CLI or the server against a temporary `VAAL_DATA_DIR`. This includes the two-week download that `serve` makes into an empty directory, when you need real data to check a change. A temporary directory you created is yours to delete.
- Run read-only git commands, and create a branch for your work.
- Commit, push or open a pull request. Do your work on a branch, not on `main`. Use contextual commits and small stacked PRs. Request the user to send approval message or to merge them into `main`.

## Ask before you do these

- **Change the user's data directory.** This is `data/` in the checkout, or `~/.vaalstreet` for an installed package, or the directory the user's own `VAAL_DATA_DIR` names. Ask before you delete, overwrite or migrate anything in it. `desk.sqlite` holds the user's ledger, watchlist and alerts, and `strategies/` holds strategy code they imported; nothing can rebuild either. The raw archive is about 1.5 GB and takes hours to download again.
- **Start a large download.** Examples are `sync --all` or a backfill into the real data directory. These put load on GGG's servers and on the user's connection.
- **Add a runtime dependency.** The package has only three today.

## Invariants

These rules hold across the code base. If a change you want to make would break one, explain the conflict to the user instead of working around it.

- **Causality.** Fair value, typical ranges, the leg premium and every backtest input at hour *t* use only data published up to hour *t*. Model parameters are re-fitted on trailing data. A strategy's `ctx` sees only the past. Look-ahead makes backtests and the scorecard look better than the truth, and nothing else will catch it, so add or extend a test whenever you touch these paths.
- **Two markets per item.** An item's Chaos market and its Divine market are different prices; the Divine market is usually several percent dearer. Keep them apart in derived data. Convert Divine-market prices at the same hour's Divine price.
- **Published lowest and highest ratios do not set order levels.** Joke trades and single-unit rounding pollute them in most hours. Flip levels come from volume-weighted hourly averages, as the README describes. Where the ratios are used, as in `pricing.typical_range`, they go through a robust trailing median that tolerates a large share of garbage.
- **Raw data is the source of truth.** `data/raw/` keeps the digests byte for byte as the exchange served them. Everything under `data/parquet/` must stay rebuildable from raw.
- **Derived data carries a version.** Existing installs rebuild derived files only when the version changes. If you change how hourly prices are derived in `ingest.py`, bump `PRICES_VERSION`. If `items.build` learns something new, bump `VERSION` in `items.py`. Without the bump, users keep their old files and your change does nothing for them.
- **Tests stay offline.** `tests/conftest.py` builds a small synthetic exchange archive with known prices, premiums and recipes. `tests/test_data.py` serves digests from a local HTTP server and passes its address as `base_url`. New tests should use the same approach, so that the suite stays fast and its answers stay known. If a model change needs a property the synthetic archive lacks, extend the archive and state the new known answer in a comment.
- **Local and read-only.** The server binds to `127.0.0.1` and rejects requests whose Host or Origin is not local; keep those checks. The only outbound traffic is read-only requests for exchange digests, the item export and item icons, sent with the `POE_USER_AGENT` identification string. There are no accounts, no tokens and no in-game automation. The README promises this to users.
- **Shared strategies are code.** `strategy import` parses the file without running or importing it, shows the source under a warning, saves it only with `--yes`, and never overwrites a file. Export writes a local file only; nothing is uploaded.
- **GGG's assets are not committed.** Item icons are fetched at run time and cached in the data directory. The README's license section depends on this.

## Code and writing style

Match the code around you. Modules open with a docstring that explains the model or the job of the module and why it works that way. Comments explain reasons, not mechanics. Python modules use `from __future__ import annotations` and type hints. Model code works on dense numpy arrays shaped hour × item, loaded through `Panel`.

User-facing text is written for a player and junior trader, in plain words. This covers UI labels, CLI help and the README. For example, write "the Divine market pays 8% more", not a term from the code such as "the b-leg premium".

When a change affects what the user sees or how a number is computed, update the README in the same change. The README is the one place that describes the models.

## Finishing work

Carry on until the task the user asked for is done. Status notes are welcome, and so are questions about open decisions, but keep working on the parts that do not depend on the answer.

If you notice yourself offering to wait or inviting the user to redirect you, go on to the next part of the task instead. Ordinary obstacles, such as a failing test, a timeout, an empty result or a tool error, are things to diagnose and work through, not reasons to stop. Stop only when the task cannot move forward without the user's input, or when the blocker was put there on purpose. If something has clearly been placed out of bounds, such as a file marked not to be touched or access that was withheld, leave it alone, say what you found, and look for another safe way to finish. None of this replaces the "Ask before you do these" list above.

Run the tests before you report a change as done. If they fail, say so and show the failure.

## Communicating

Before you start a task, say in one line what you are about to do. Give brief updates while you work, so the user can follow along.

Close with a short recap that stands on its own: what you found, what you changed, and what is next. The reader has not seen your tool calls, your reasoning or the files you opened, and may be switching in from other work. Put the key points first. List every file you changed, including small edits, because the user reviews the diff against your summary.

Use plain language and full sentences. Do not use a term or an abbreviation that you made up during the work; say what the thing is. Prefer short sentences in the active voice, and use the same word for the same thing throughout. Use a paragraph of prose where ideas connect, and keep bullets for lists of separate items.
