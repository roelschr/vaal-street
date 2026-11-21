# Ideas from issue #2: what we build, and in what order

This page gives a verdict on every idea in issue #2, "Ideas to explore next". There are five kinds of verdict:

- **Build**: it fits the desk and the data we have. The wave number says when.
- **Measure first**: it is a claim about the market, such as "resting stock predicts price". We test it on the full history once the sync is done. We ship it only if it holds up. "Tested, does not predict" is a valid result, and we record it here.
- **After the sync**: it needs the full league history on disk, which is still downloading.
- **Owner's decision**: it touched a promise the README makes (local only, read-only traffic, PoE 1). The owner has decided: collecting public data under each source's rules is fine; uploading or publishing is not; the server stays local; PoE 2 waits.
- **Not now**: no data source exists for it, and guessing would put made-up game facts into the desk.

Waves run in order. Within a wave, the tasks touch different files, so they can be built side by side.

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
| Screenshot trade check | Researching the policy | The owner asked us to read GGG's third-party tool policy first. It would also need screen capture and text recognition, which means new dependencies. The typed check covers the same decision in the meantime. |
| Fill calibration | Build, wave 3 | It needs orders you posted and what filled, which the order tracker records in wave 1. It then compares your fills with the plan's estimate, and tunes "share of the flow" per item once enough fills exist. |
| Recipe verification log | Build, wave 1 and 2 | Wave 1 stores "confirmed in game" per recipe. In wave 2, the Arbitrage page flags recipes nobody has confirmed. |

## Small bankrolls

| Idea | Verdict | Why |
| --- | --- | --- |
| Whole-lot backtests | Build, wave 1 | The simulator fills only whole items, at whole-number ratios a small purse can post. The flip page already does this when you enter a bankroll. |
| Bankroll ladder | Build, wave 3 | The same flips and backtests are run at a ladder of purse sizes, from a few dozen Chaos to a few Divines. The ladder shows what opens up at each step. We check it on full league history after the sync. |
| Compounding plan | Build, wave 3 | It is built with the ladder. It shows the purse sizes at which the session plan changes, and what to switch to at each one. |
| Vendor loops | Build if a public source has the prices | NPC vendor prices are not in the item export we use, and we will not type them in from memory. We are checking public game-data sources and their rules. |

## Arbitrage

| Idea | Verdict | Why |
| --- | --- | --- |
| Longer loops | Build, wave 2 | Round trips of three or more swaps, and loops through Divine, with the same "thinnest swap" depth check as today. |
| More recipes | Build, wave 2, as far as game data allows | We add the recipes the game-data export describes. Each new recipe starts unconfirmed and shows that flag until you check it in game. |
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
| Patch-note impact | Checking sources | Tagging leagues needs patch notes per league in a form a program can read. We are checking public sources. Otherwise a tag list you fill in yourself is possible later. |
| Next-league rehearsal | Build, wave 4 | The desk is built "as of" a past hour and played forward. This is the same causality rule the backtests follow, so it is a natural fit. It is a large UI task, so it comes late. |
| Divine timing | After the sync, wave 4 | It comes from the Divine-by-league-day curves the League start page already draws. |
| Meta shifts | After the sync, wave 4 | We compare each item's demand growth with every past league at the same day. |
| Event leagues | After the sync, wave 4 | Short events become their own family of precedents, so they stop skewing the challenge-league ones. |

## Backtesting and strategy

| Idea | Verdict | Why |
| --- | --- | --- |
| Queue model | Build, wave 3 | We use the published resting stock as the queue ahead of you, instead of a flat share of volume. We test it on full history. |
| Gold-aware backtests | Build, wave 1 | Each order is charged Gold, and the run stops posting when the budget runs out. The fee is a parameter, because the schedule is not published. |
| Strategy search | After the sync, wave 4 | We sweep parameters and report what holds up on leagues the search did not see. It is built together with walk-forward evaluation. |
| Walk-forward evaluation | After the sync, wave 4 | We tune on earlier leagues and test on the next one. This needs several leagues on disk. |
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
| Finer data | Checking sources | The exchange API is hourly. We are checking whether any sanctioned source is finer. If none is, this stays "not now". |
| Trade-site prices | Build, once the rules are clear (owner's decision) | The owner allows collecting public data under each source's rules. We are reading the trade site's and aggregators' rate limits and terms first. The README's list of outbound traffic will be updated. |
| Kingsmarch and Gold income | Not now as a model, partly in wave 1 | No data source exists for Gold income. The session journal can record the Gold you earned, so your own sessions give Gold a value. |
| PoE 2 | Not now (owner's decision) | The desk and models are built and tested for PoE 1 only. We revisit this after the PoE 1 waves. |

## Community and sharing

| Idea | Verdict | Why |
| --- | --- | --- |
| Strategy sharing | Build, wave 3 | You export a strategy file with its cross-league results to a local file, and import one. Nothing is uploaded. |
| Public league report | After the sync, wave 4 | A command writes a "state of the economy" page per league day to a local folder. Where to publish it is up to you. |
| Anonymous fill data | Not building (owner's decision) | Pooling needs a server and uploads. The desk does not upload anything. |
