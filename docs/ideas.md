# Ideas from issue #2: what we build, and in what order

This page gives a verdict on every idea in issue #2, "Ideas to explore next". There are five kinds of verdict:

- **Build**: it fits the desk and the data we have. The wave number says when.
- **Measure first**: it is a claim about the market, such as "resting stock predicts price". We test it on the full history once the sync is done. We ship it only if it holds up. "Tested, does not predict" is a valid result, and we record it here.
- **After the sync**: it needs the full league history on disk, which is still downloading.
- **Owner's decision**: it touched a promise the README makes (local only, read-only traffic, PoE 1). The owner has decided: collecting public data under each source's rules is fine; uploading or publishing is not; the server stays local; PoE 2 waits.
- **Not now**: no data source exists for it, and guessing would put made-up game facts into the desk.

Waves run in order. Within a wave, the tasks touch different files, so they can be built side by side. What each outside data source allows is written up in `data-sources.md`.

- **Wave 1 (foundations):** the ledger learns Gold, orders, sessions and recipe checks. The backtest trades whole lots, pays Gold and keeps a play schedule. Fair value gets a range and a separate Divine-market premium.
- **Wave 2:** the desk uses wave 1, with profit per Gold, trade checks, stash value and longer round trips.
- **Wave 3:** planning tools and alerts.
- **Wave 4 (after the sync):** league-start analytics and the measurements.

## Ground truth: closing the loop with the game

| Idea | Verdict | Why |
| --- | --- | --- |
| Gold in the ledger | Build, wave 1 | Each trade gets an optional Gold amount. From those, the desk learns the Gold fee per unit for each item. Until you have recorded a trade, the fee shows as unknown. We do not guess a fee schedule. |
| Profit per Gold | Build, wave 2 | Flips, conversions and round trips are ranked by Chaos per 1,000 Gold, using the fees from your ledger or a fee you enter. A session plan fits under a Gold budget. |
| Typed trade check | Build, wave 2 | You type the ratio and stock from the order window and get go / no-go against fair value with its range, the flip plan and how much stock already rests at that level. |
| Screenshot trade check | Allowed with conditions; owner's call on dependencies | GGG's developer docs allow reading the screen once per manual key press, if you know what is read and the tool sends nothing to the game (see `data-sources.md`). It would need screen capture and text recognition, which are new dependencies. The typed check comes first. |
| Fill calibration | Build, wave 3 | It needs orders you posted and what filled, which the order tracker records in wave 1. It then compares your fills with the plan's estimate, and tunes "share of the flow" per item once enough fills exist. |
| Recipe verification log | Build, wave 1 and 2 | Wave 1 stores "confirmed in game" per recipe. In wave 2, the Arbitrage page flags recipes nobody has confirmed. |

## Small bankrolls

| Idea | Verdict | Why |
| --- | --- | --- |
| Whole-lot backtests | Build, wave 1 | The simulator fills only whole items, at whole-number ratios a small purse can post. The flip page already does this when you enter a bankroll. |
| Bankroll ladder | Build, wave 3 | The same flips and backtests are run at a ladder of purse sizes, from a few dozen Chaos to a few Divines. The ladder shows what opens up at each step. We check it on full league history after the sync. |
| Compounding plan | Build, wave 3 | It is built with the ladder. It shows the purse sizes at which the session plan changes, and what to switch to at each one. |
| Vendor loops | Only with prices you enter | No sanctioned public source carries NPC vendor prices (see `data-sources.md`). The desk can run vendor loops on a price table you fill in yourself, each entry marked unconfirmed until you check it in game. |

## Arbitrage

| Idea | Verdict | Why |
| --- | --- | --- |
| Longer loops | Build, wave 2 | Round trips of three or more swaps, and loops through Divine, with the same "thinnest swap" depth check as today. |
| More recipes | Build, wave 2, as recipes you enter | The item export has no recipe data, and the wiki blocks automated reading. We add a local recipe list you can extend from the desk. The recipes the desk already derives stay as they are. Every new recipe starts unconfirmed. |
| Recipe discovery from prices | Measure first, wave 4 | We look for pairs whose price ratio stays fixed. The output is a list of candidates to check in game, never a recipe the desk trades on. |
| Live resting-order watch | Build, wave 3 | An alert fires when a round trip has orders resting to be taken and its edge clears your threshold. |
| Cross-league arbitrage of attention | After the sync, wave 4 | The digests already carry Hardcore, Standard and Ruthless markets. We compare the same markets across them. |

## Pricing models

| Idea | Verdict | Why |
| --- | --- | --- |
| Two-market model | Build, wave 1 | Each item gets one value plus a Divine-market premium that moves slowly. The fit is causal and comes with a look-ahead test. |
| Premium forecasting | Measure first, wave 4 | We test whether the premium follows the league day the same way across leagues. |
| Order-book inference | Measure first, wave 4 | We test whether the changes in resting stock predict the next hours' price. If they do not, we say so and ship nothing. |
| Spread estimation | Measure first, wave 4 | We estimate the spread from what is published. The README already says validating it needs checks in game. The typed trade check can collect those checks. |
| Regime detection | Measure first, wave 4 | Foulborn Exalted Orb on day 6 is the known case. A detector must flag it early without flagging every quiet market. |
| Item relationships | Measure first, wave 4 | We look for leaders, laggards and baskets on the full history. We keep only what holds across leagues. |
| Uncertainty everywhere | Build, wave 1, then spread through later waves | The fair value filter already tracks its own uncertainty. Wave 1 exposes it as a range. Margins and estimates then carry ranges as each page is touched. |

## League start

| Idea | Verdict | Why |
| --- | --- | --- |
| Launch-day playbook | After the sync, wave 4 | Hour by hour for the first 72 hours of each past league. It needs those starts on disk. |
| Patch-note impact | Only with tags you enter | No machine-readable patch-note source is open to the desk. A tag list you fill in yourself is possible later. |
| Next-league rehearsal | Build, wave 4 | The desk is built "as of" a past hour and played forward. This is the same causality rule the backtests follow, so it is a natural fit. It is a large UI task, so it comes late. |
| Divine timing | After the sync, wave 4 | It comes from the Divine-by-league-day curves the League start page already draws. |
| Meta shifts | After the sync, wave 4 | We compare each item's demand growth with every past league at the same day. |
| Event leagues | After the sync, wave 4 | Short events become their own family of precedents, so they stop skewing the challenge-league ones. |

## Backtesting and strategy

| Idea | Verdict | Why |
| --- | --- | --- |
| Queue model | Build, wave 3 | We use the published resting stock as the queue ahead of you, instead of a flat share of volume. We test it on full history. |
| Gold-aware backtests | Build, wave 1 | Each order is charged Gold, and the run stops posting when the budget runs out. The fee is a parameter, because the schedule is not published. |
| Strategy search | Built, wave 4 | We sweep parameters and report what holds up on leagues the search did not see. It is built together with walk-forward evaluation. |
| Walk-forward evaluation | Built, wave 4 | We tune on earlier leagues and test on the next one. This needs several leagues on disk. |
| Portfolio view | Build, wave 3 | Several strategies share one bankroll and compete for its capital. |
| Inventory risk | Build, wave 3 | It shows what a plan stands to lose on what it holds, using the fair-value range from wave 1. |
| Session simulator | Build, wave 1 | Backtests follow a play schedule, such as "3 hours on weekday evenings". Orders stand while you are away, as `attention` already does. |

## The hideout warrior's desk

| Idea | Verdict | Why |
| --- | --- | --- |
| Order tracker | Build, wave 1 | You list the orders you post, and the desk says which to cancel, re-price or collect. Fill calibration in wave 3 builds on this. |
| Session journal | Build, wave 1 | You start and stop a session. It reports profit, Gold used and Chaos per hour against the plan. |
| Smarter alerts | Build, wave 3 | Alerts on plans, round trips and league precedents, not only on prices. |
| Phone view | Not building (owner's decision) | The server answers only this computer, and that stays a promise in the README. Alerts reach you through browser notifications instead. |
| Voice or toast overlay | Build, wave 3 | Browser notifications and spoken prompts from the hideout window. They need no new dependency and do nothing in the game. |
| Stash valuation | Build, wave 2 | You enter what you hold. The desk values it at fair value, with a range, and says which market pays more for each item. |
| Tax of time | Build, wave 2 | Each plan gets a count of orders and clicks, and a minutes estimate, next to its Chaos. |

## Data

| Idea | Verdict | Why |
| --- | --- | --- |
| Shared snapshots | Not building (owner's decision) | Local `snapshot` and `restore` already exist. The desk collects public data but does not upload or publish anything. |
| Finer data | Not possible | GGG's docs say the hourly digest is the only source, with no data from the current hour (see `data-sources.md`). |
| Trade-site prices | Not through the trade site; poe.ninja pending its terms | GGG's terms forbid using the trade site's undocumented API, and the public stash API is closed to local tools. poe.ninja publishes prices, but its terms could not be read automatically. If the owner confirms they allow it, we fetch at most hourly, cache, and credit poe.ninja. |
| Kingsmarch and Gold income | Not now as a model, partly in wave 1 | No public data source exists. The session journal records the Gold you earned, so your own sessions give Gold a value. |
| PoE 2 | Not now (owner's decision) | The desk and models are built and tested for PoE 1 only. We revisit this after the PoE 1 waves. |

## Community and sharing

| Idea | Verdict | Why |
| --- | --- | --- |
| Strategy sharing | Build, wave 3 | You export a strategy file with its cross-league results to a local file, and import one. Nothing is uploaded. |
| Public league report | After the sync, wave 4 | A command writes a "state of the economy" page per league day to a local folder. Where to publish it is up to you. |
| Anonymous fill data | Not building (owner's decision) | Pooling needs a server and uploads. The desk does not upload anything. |

## How a measurement passes

The "Measure first" ideas are claims about the market. We test seven or more of them on a handful of leagues, so one of them is likely to look good by chance alone. To guard against that, the rules below were written down before any real run. Their numbers are fixed. If we change a rule after seeing a result, that change counts as one more setting tried, and we record it here. `vaalstreet research` applies these rules. Each result prints its verdict, its numbers and the number of settings tried. The verdict is "pass", "fail" or "not enough data" for a claim, "candidates" for a discovery list, and "report only" for a description.

**Leagues.** Only challenge leagues count, and only those whose first hour is in the archive. A league that was already running when the archive starts has no known league day. Each league needs at least 14 days of history. Items that traded less than 10,000 Chaos over the whole league are left out, as `scorecard` leaves out thin items. The whole league's total decides it, so an item is in or out for the full league. A measurement fits or tunes only on *training leagues*, which are leagues that ended before the *test league* began. It is never evaluated on a league that overlaps a training league in time. Training leagues must count too: a league with an unknown start never trains anything. A forecasting measurement needs at least 3 test leagues, each with at least one training league before it, so that "a majority" means something. With fewer leagues, the verdict is "not enough data". Every result is reported for each test league and pooled across them.

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
