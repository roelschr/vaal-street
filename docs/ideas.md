# Ideas from issue #2: what happened to each one

This page records what became of every idea in issue #2, "Ideas to explore next". All the work sits in a stack of pull requests, #4 to #29, plus #16 on `main`. The verdicts are:

- **Built (#N)**: it is on the desk, in the pull request(s) named.
- **Measured: fail, nothing shipped**, or **not enough data**: it was a claim about the market. It was tested on the full history under the rules at the end of this page and did not pass, so the desk does not use it. The numbers are under "Results on the full history".
- **Measured: candidates** or **report only**: a list to check in game, or a description of the data. The desk never trades on it.
- **Not building (owner's decision)**: it would break a promise the README makes. The owner decided: collecting public data under each source's rules is fine; uploading or publishing is not; the server stays local; PoE 2 waits.
- **Not built**: no data source exists for it, and guessing would put made-up game facts into the desk.

What each outside data source allows is written up in `data-sources.md`.

## Ground truth: closing the loop with the game

| Idea | Verdict | Why |
| --- | --- | --- |
| Gold in the ledger | Built (#5) | Each trade takes an optional Gold amount. The desk learns each item's Gold fee from those. Until you record a trade, the fee shows as unknown; there is no guessed fee schedule. |
| Profit per Gold | Built (#9) | Flips, conversions and round trips are ranked by Chaos per 1,000 Gold, from your recorded fees or a fee you enter. A session plan fits under a Gold budget. |
| Typed trade check | Built (#8, page in #15) | Type the ratio and stock from the order window and get go, caution or no-go against fair value and its range. |
| Screenshot trade check | Built (#22), as an optional extra | `vaalstreet screen` (`pip install "vaalstreet[screen]"`) runs under Windows Python next to the game. It reads the offer on a key press, sends nothing to the game and keeps screenshots in memory only. The window layout it expects has not been tested against the live game. |
| Fill calibration | Built (#12, page in #18) | Compares the fills of your tracked orders with the plan's estimate and learns your share of the flow per market once enough fills exist. |
| Recipe verification log | Built (#5, #10) | You mark whether a recipe or round trip worked in game. The Arbitrage page flags recipes nobody has confirmed. |

## Small bankrolls

| Idea | Verdict | Why |
| --- | --- | --- |
| Whole-lot backtests | Built (#7) | The backtest fills only whole items, at whole-number ratios a small purse can post. |
| Bankroll ladder | Built (#11, page in #18) | Flips and backtests at a ladder of purse sizes. On the full history, last week's replay delivered only 3 to 26% of the "promised" rate, so the desk shows the replay first. Trust the replay. |
| Compounding plan | Built (#11) | Shows the purse sizes at which the session plan changes, and what to switch to. |
| Vendor loops | Built (#10), with prices you enter | No sanctioned source carries NPC vendor prices. The desk runs vendor loops on a price table you fill in, each entry unconfirmed until you check it in game. |

## Arbitrage

| Idea | Verdict | Why |
| --- | --- | --- |
| Longer loops | Built (#10) | Round trips of three or more swaps, and loops through Divine, with the same depth check. |
| More recipes | Built (#10), as recipes you enter | A local recipe list you extend from the desk. Every new recipe starts unconfirmed. |
| Recipe discovery from prices | Measured: candidates (#21) | One candidate on the full history: History against The Slumbering Beast at 2:1. Check it in game; the desk does not trade on it. |
| Live resting-order watch | Built (#13) | An alert fires when a round trip has orders waiting to be taken and its edge clears your threshold. |
| Cross-league arbitrage of attention | Built (#19) | Compares the same market across the challenge league, Hardcore, Standard and Ruthless. The gaps found so far are lasting ones, such as the Divine in Standard. |

## Pricing models

| Idea | Verdict | Why |
| --- | --- | --- |
| Two-market model | Built as an experiment (#6); measured: fail | It predicts worse than pricing each market on its own, in every league. It stays a scorecard experiment (`scorecard --two-market`) and is not used anywhere on the desk. |
| Premium forecasting | Measured: fail, nothing shipped (#21) | The desk keeps the trailing premium. |
| Order-book inference | Measured: fail, nothing shipped (#21) | The stock on offer adds nothing beyond fair value. |
| Spread estimation | Measured: report only (#21) | Spreads are wide and steady from week to week. Check them in game with the trade check. |
| Regime detection | Measured: not enough data, not shipped as an alert (#21) | The only known crash is in Covens, too young to count, and the detector raises too many false alarms. |
| Item relationships | Measured: candidates (#21) | 140 pairs, all expected ones. Nothing to trade on. |
| Uncertainty everywhere | Built (#6, #8, #15) | Fair value carries a range, shown on Markets, the item chart, the trade check and stash value. The range says how sure the model is now; it is not a forecast. |

## League start

| Idea | Verdict | Why |
| --- | --- | --- |
| Launch-day playbook | Built (#19, page in #27) | Hour by hour for the first days of each past challenge league. |
| Patch-note impact | Not built | No machine-readable patch-note source is open to the desk, and no tag list you enter was built. |
| Next-league rehearsal | Built (#25, page in #27) | The desk is rebuilt "as of" a past hour and played forward, with its own practice ledger. Start it from League start or with `rehearse`. |
| Divine timing | Built (#19, page in #27) | From the Divine-by-league-day curves. In 5 of 5 past leagues the Divine was cheapest on day 0. |
| Meta shifts | Built (#19, page in #27) | Each item's demand growth against every past league at the same day. Many rows rest on a single past league. |
| Event leagues | Built (#19) | Event leagues are their own family and never count as precedents for a challenge league. |

## Backtesting and strategy

| Idea | Verdict | Why |
| --- | --- | --- |
| Queue model | Built (#14); the default since #29 | A resting order waits in line behind the stock already on offer, and takes at most 10% of an hour. It has not been checked against real fills; your tracked orders (`fills`) are how to check it. |
| Gold-aware backtests | Built (#7) | Each order pays Gold, and the run stops posting when the budget runs out. The fee is a setting, because the schedule is not published. |
| Strategy search | Built (#20, page in #27) | Sweeps settings and reports results on leagues the search did not see. |
| Walk-forward evaluation | Built (#20) | Tunes on earlier leagues and tests on the next one. |
| Portfolio view | Built (#14) | Several strategies share one bankroll. |
| Inventory risk | Built (#14) | What a plan stands to lose on what it holds, from the fair value range. |
| Session simulator | Built (#7) | Backtests follow a play schedule, such as "3 hours on weekday evenings". Orders stand while you are away. |

## The hideout warrior's desk

| Idea | Verdict | Why |
| --- | --- | --- |
| Order tracker | Built (#5) | You list the orders you post, and the desk says which to keep, cancel, re-price or collect. |
| Session journal | Built (#5) | Start and stop a session to see profit, Gold used and Chaos per hour against the plan. |
| Smarter alerts | Built (#13, page in #18) | Alerts on flip plans, round trips, your orders and league precedents, not only on prices. |
| Phone view | Not building (owner's decision) | The server answers only this computer, as the README promises. Alerts reach you through browser notifications. |
| Voice or toast overlay | Built (#18) | Browser notifications and spoken prompts from the desk's window. No new dependency, and nothing touches the game. |
| Stash valuation | Built (#8, page in #15) | Enter what you hold. Each line gets a value with a range, and which market pays more for it. |
| Tax of time | Built (#9) | Each plan shows its count of orders and a minutes estimate next to its Chaos. |

## Data

| Idea | Verdict | Why |
| --- | --- | --- |
| Shared snapshots | Not building (owner's decision) | Local `snapshot` and `restore` exist. The desk does not upload or publish anything. |
| Finer data | Not possible | GGG's hourly digest is the only source, with no data from the current hour (see `data-sources.md`). |
| Trade-site prices | Not through the trade site; built through poe.ninja for card rewards (#24, table in #27) | GGG's terms forbid the trade site's undocumented API. poe.ninja's terms allow its economy endpoints. The desk prices card sets whose reward is a unique, gem or base type from poe.ninja's listings, fetched at most hourly and cached locally. They are listing prices, credited to poe.ninja, and never enter fair value. |
| Kingsmarch and Gold income | Not built as a model; Gold earned is in the session journal (#5) | No public data source exists. Your own sessions record the Gold you earned. |
| PoE 2 | Not now (owner's decision) | The desk and models are built and tested for PoE 1 only. |

## Community and sharing

| Idea | Verdict | Why |
| --- | --- | --- |
| Strategy sharing | Built (#14) | Export a strategy with its cross-league results to a local file, and import one. Nothing is uploaded. |
| Public league report | Built (#23) | `vaalstreet report` writes a "state of the economy" page per league day to a local folder, from data up to the end of that day. It has no scripts and nothing from your ledger. Where to publish it is up to you. |
| Anonymous fill data | Not building (owner's decision) | Pooling needs a server and uploads. |

## Fixes along the way

- **Ingest memory** (#16, based on `main`): ingest fits in memory on the full archive.
- **Real-data fixes** (#17, #26, #28): joke-hour prices, garbage swaps, plans judged against holding Divines, each market's own flip levels, and crashes and memory limits found in the full-history run.

## Results on the full history

The archive runs from July 2024 to 22 November 2026. The challenge leagues in it are Settlers, Mercenaries, Keepers, Mirage, Allflame and Covens. Settlers only trains. Mercenaries, Keepers, Mirage and Allflame are the test leagues. Covens was live at day 8 and never counts, because it has under 14 days. The pass rules below were fixed before the run, and "fail" is a valid result.

| Measurement | Command | Verdict | Key numbers | What it means for a player |
| --- | --- | --- | --- | --- |
| Fair value | `scorecard --league <L>` | Beats the last print | Cuts the error by 9 to 34% in every finished league. | Fair value is a better guide than the last trade. |
| Two-market model | `research two-market`, `scorecard --two-market` | Fail | Loses to pricing each market on its own in every league: Chaos market −26% to −50%, Divine market −65% to −381%, on items that trade almost every hour. | Not used on the desk; it stays a scorecard experiment. |
| Premium forecasting | `research premium` | Fail | −79% against the trailing premium. | The desk keeps the trailing premium. |
| Order-book inference | `research orderbook` | Fail | −0.03% against fair value, better in 1 of 4 leagues. | The stock on offer does not predict the price. Nothing shipped. |
| Spread estimation | `research spread` | Report only | Median spread 0.37 to 0.50 in the Chaos market, 0.16 to 0.21 in the Divine market. Steady from week to week (rank correlation 0.71 to 0.84). | Spreads are wide. Check them in game with the trade check. |
| Regime detection | `research regime` | Not enough data | The Covens crash was flagged 1 hour after it happened, but Covens does not count. 0.215 false alarms per quiet item-week, against a ceiling of 0.1. | Not shipped as an alert. |
| Item relationships | `research relationships` | Candidates | 140 pairs, all expected ones, such as Mirror of Kalandra with Mirror Shard. | Nothing to trade on. |
| Recipe discovery | `research recipes` | Candidates | 1: History against The Slumbering Beast at 2:1, in 2 of 2 leagues. | Check it in game. The desk does not trade on it. |
| Strategy search with walk-forward | `search <strategy> --fill-model queue` | Swing: a lead. Reversion and momentum: fail | With the wait-in-line fill model, the chosen swing settings beat holding Divines on Mercenaries and Keepers, and are about level on Mirage and Allflame. Reversion −79% and momentum −82% against holding Divines. Under the old share fill model, swing showed +600 to +2,500%, which came from the fill model. | The wait-in-line model was chosen after the share result was seen, so treat swing as a lead, not proof. |
| Waiting in line against a share of volume | `research queue-vs-share`, `backtest swing --fill-model share\|queue` | Report only | Swing on Mirage against holding Divines: +960% with a share of volume, −5.4% waiting in line (with the 10% cap). | Waiting in line is now the default (#29). Neither model has been checked against real fills; `fills` is how to check it. |
| Bankroll ladder | `research ladder-rates` | Report only | Last week's replay delivers only 3 to 26% of the "promised" rate. | The desk shows the replay first. Trust it. |
| League start | `playbook`, `divine-timing`, `meta`, `cross-league` | Checked | Every analysis uses the 5 earlier challenge leagues as precedents, and no event league. In 5 of 5 leagues the Divine was cheapest on day 0. | The Divine has been cheapest at launch every time so far. |

## How a measurement passes

The "Measure first" ideas are claims about the market. We test seven or more of them on a handful of leagues, so one of them is likely to look good by chance alone. To guard against that, the rules below were written down before any real run. Their numbers are fixed. If we change a rule after seeing a result, that change counts as one more setting tried, and we record it here. `vaalstreet research` applies these rules. Each result prints its verdict, its numbers and the number of settings tried. The verdict is "pass", "fail" or "not enough data" for a claim, "candidates" for a discovery list, and "report only" for a description.

**Leagues.** Only leagues of the "challenge" family count (the `family` column of the `leagues` view, so events, Hardcore and other variants, Standard and private leagues do not), and only those whose first hour is in the archive. A league that was already running when the archive starts has no known league day. Each league needs at least 14 days of history. Items that traded less than 10,000 Chaos over the whole league are left out, as `scorecard` leaves out thin items. The whole league's total decides it, so an item is in or out for the full league. A measurement fits or tunes only on *training leagues*, which are leagues that ended before the *test league* began, by the same rule strategy search uses. It is never evaluated on a league that overlaps a training league in time. Training leagues must count too: a league with an unknown start never trains anything. A forecasting measurement needs at least 3 test leagues, each with at least one training league before it, so that "a majority" means something. With fewer leagues, the verdict is "not enough data". Every result is reported for each test league and pooled across them.

**Forecasting signals** (`two-market`, `premium`, `orderbook`):

- *Target and metric.* The target is the scorecard's: the value-weighted average price over the next 6 hours. The metric is the mean absolute log error (0.05 is about 5%), over the cells (item and hour) where the signal, both baselines and the target all exist. The first 48 hours of each league are skipped as warm-up. This is the one figure that decides the verdict. Breakdowns by market or by how often an item trades are shown for information only.
- *Baselines.* There are two: the desk's fair value and the last print. For `premium`, which forecasts the Divine market's premium rather than a price, the baselines are the trailing 24-hour premium (the desk's estimate) and the last observed premium.
- *Pass.* The signal must cut the error against **both** baselines, in a majority (more than half) of test leagues **and** pooled. For the pooled figure, the interval for the error cut must exclude zero. The interval comes from a block bootstrap: whole days are resampled with all items together, within each league. It uses 10,000 resamples and the fixed seed 20261121.
- *Correction for many tries.* The intervals use a Bonferroni correction over the whole family of forecasting hypotheses and settings, fixed at **7**: `two-market` tries 3 premium drift settings (0.05, 0.1, 0.2), `premium` tries 1, and `orderbook` tries 3 lookbacks (3, 6, 24 hours). At an overall 5% error rate, each interval is a two-sided 99.29% interval (0.05 / 7 split over two tails). The correction stays at 7 even when one measurement runs alone. Adding a hypothesis or a setting raises the count, and the change is recorded here.
- *Tuning.* Where a measurement has several settings, it picks one on the training leagues of each test league and evaluates only that one.

**Detectors** (`regime`):

- *The known case.* Foulborn Exalted Orb fell from about 820 Chaos to about 32 on day 6 of Covens. The *event hour* is the first hour whose Chaos-market price is below half its trailing 24-hour median. The detector passes on the known case if it raises an alarm on that item between 48 hours before and 6 hours after the event hour. A league without the case gives "not enough data" for this part.
- *False alarms.* A *quiet item-week* is a week in which the item printed in at least half the hours and its fair value moved less than 20% from the start of the week to the end. The detector may raise at most 0.1 alarms per quiet item-week, pooled over the leagues tested.
- *Fixed settings.* There is one setting. Each print's surprise against fair value is capped at 5 standard deviations. A two-sided cumulative sum with an allowance of 1.0 raises an alarm when it reaches 8, provided the number of items traded over the last 6 hours is at least its 7-day median for 6 hours. After an alarm, the sum restarts at zero. These settings were chosen on synthetic data before any real run. A first draft had an allowance of 0.5 and measured activity by traded value. On synthetic noise it raised about 0.3 false alarms per quiet item-week, and the value check blocked a planted crash, because a crash lowers traded value even when far more items change hands. Both were changed before any real run.

**Discovery outputs** (`relationships`, `recipes`) are lists of candidates to check in game. The desk never uses them in a plan. A candidate holds only if it holds in more than half of the leagues where both items trade, with at least 2 such leagues. The candidates are drawn from the 60 most traded items of each league.

- *Recipes.* The daily price ratio of the two items sits within 2% of a simple ratio p:q with p and q at most 10, in the same direction in each league. The ratio must also be steady: the median absolute deviation of its daily log is at most 0.03, over at least 7 days.
- *Lead and lag.* Item A's 6-hour move in fair value is correlated with item B's error against fair value over the next 6 hours. The correlation must be at least 0.1 in size, with the same sign in each league.
- *Baskets.* The daily changes in fair value of two items are correlated at 0.6 or more.

**Report-only measurements** (`spread`, `queue-vs-share`, `ladder-rates`) describe the data and the desk's own models. They do not claim that a signal predicts anything, so they have no pass rule and print "report only". The spread estimate still needs checks in game before it is trusted.

## What the owner still has to do

- **Merge the pull requests.** Start with #16, the ingest memory fix, which is based on `main`. Then merge the stack from #4 to #30 in order.
- **Index the archive.** After merging #16, run `python -m vaalstreet ingest`. The archive is downloaded but not yet indexed in `data/`.
- **Check the screen check in game.** Its window layout has not been tested against the live game. It needs Windows and Python 3.10 to 3.12.
- **poe.ninja dumps (optional).** The leagues before July 2024 could go into `data/ninja/`.
