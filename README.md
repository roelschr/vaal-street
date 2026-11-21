# Vaal Street

A local trading desk for the Path of Exile Currency Exchange. It mirrors the exchange's public hourly history, turns it into prices you can trust, and answers the questions a trader actually has: what is this worth, what can I flip right now, what converts into something dearer, what did this item do at this point of past leagues, and would my strategy have made money.

Everything runs on your machine. The only network traffic is read-only requests for the public exchange digests, an item-name export and item icons. No account, no tokens, no in-game automation.

PoE 1 is the focus. The data layer also mirrors PoE 2, but the desk and models are built and tested against PoE 1.

## Quick start

Requires Python 3.10+.

```sh
pip install -e .
python -m vaalstreet serve
```

Open **http://localhost:3000**. On first launch the desk downloads the last two weeks of exchange hours (a couple of minutes) and then keeps itself current, one new hour every hour.

To compare leagues you need more history. Either press **Download full history** on the League start page, or run:

```sh
python -m vaalstreet sync --all      # every hour since July 2024, newest first, resumable (~1.5 GB)
```

## The desk

| Page | What it answers |
| --- | --- |
| **Desk** | The hour at a glance: Divine price, best flips, sets worth completing, movers, your watchlist, fired alerts. Alerts can watch a price, a flip plan, a round trip with orders waiting to be taken, your own orders, or how an item stands against past leagues. |
| **Markets** | Every traded item: fair value and how sure the model is of it right now ("±x%"; hover for the range), 24h/7d change (blank when the earlier hours fall within a day of the item's first trade in this league, since launch prices are no level to compare with), what the Chaos market and the Divine market each pay, the gap between them, traded value, resting bids against resting offers. Click an item for its chart (fair value with the model's range shaded around it), flip plans, set recipes and the same league day in past leagues. |
| **Flips** | Ranked maker plans with the exact whole-number orders to post, margin, how often the levels are reached, expected units and profit per hour, capital needed, how crowded each side's queue is, the Gold each plan costs and the Chaos it earns per 1,000 Gold, and the minutes of your time it takes. Rank by profit per hour, per 1,000 Gold or per hour of your own time. Hover a plan's units per hour to see the share of the flow it assumes and where that share comes from; a ✓ means it was measured from your own orders. |
| **Arbitrage** | Conversions — divination card sets, shards and splinters that combine, three-for-one vendor upgrades of essences and oils (chained across tiers) — and your own recipes — against what they produce, and round trips of up to three swaps through item-for-item markets, Divine and vendor prices you entered, each with the Gold it costs and the Chaos it earns per 1,000 Gold. Choose up to one, two or three swaps; the path shows which steps are swaps, Divine trades or vendor purchases. Each row shows whether you tried it in game, with ✓ and ✗ to mark it worked or did not (and an optional note). Add or delete your own recipes and vendor prices at the bottom of the page. |
| **Ladder** | Growing a small purse: enter a starting purse and the hours you will play (or a target), and see what each purse size from 25 Chaos to 10 Divines can run, what opens up at each step, and when to switch plans as the purse grows. A projection, not a promise. |
| **League start** | The Divine price by league day across leagues, a playbook of what rose and fell over the next 3/7/14/30 days from today's league day, and what is cheap or dear compared with the same day of past leagues. |
| **Backtest lab** | Replay a strategy over any league and compare it with simply holding Divines. Options: whole lots, a Gold fee per unit and a Gold budget, the hours you play, and whether resting orders take a share of the volume or wait in line behind the stock already on offer. Add more strategies to run them together on one bankroll and see each one's profit, share of the profit, Gold and Chaos tied up. Results show the Gold spent, Chaos per 1,000 Gold, the most you held, and the worst and average bad-day drop. **Export** saves a single strategy's run as a file you can share; nothing is uploaded. |
| **Ledger** | Record your trades and the Gold they cost; positions are marked at fair value with realised and unrealised profit. Record the orders you post, and the desk says for each one whether to keep it, re-price it (with the ratio to post instead), cancel it (it now pays more, or asks less, than its market) or collect it; a fill adds the trade for you. Start and stop a session to see the profit, Gold spent and Chaos per hour it made, next to what the Flips session plan expected. Mark whether a recipe or round trip worked when you tried it in game; these checks hold for every league. A table shows how much of the flow your recorded orders actually got in each market, the share the desk now uses for them, and what that means in words. Add alerts of every kind (a price, a flip plan, a round trip with orders waiting, your own orders, or a league precedent) and see each one described in a sentence. **Stash**: type in what you hold, or value the positions your trades add up to; each line shows its worth with a low and a high, which market pays more for it and by how much, and a "thin market" flag when selling it all would take more than three hours of that market's volume, with totals and their range. |

**Hideout mode** (press `H`) opens a small always-on-top window with the top flips, your watchlist and alerts, so it can sit over the game. At the bottom of that window, turn on a notification and/or reading the title aloud for each alert that fires; both are off until you tick them, the browser asks for notification permission only when you tick the box, and the choice is remembered. The window checks for new alerts every minute and right after each new hour. Nothing is sent to the game. **Check an offer** (press `C` anywhere, including the hideout window, or use the button on Markets and on an item) opens a small panel: type the item, pick buy or sell and the Chaos or Divine market, enter the ratio as two whole numbers with the items first, and press Enter for go, caution or no-go with the reasons. It remembers the market and side you used last. Prices can be shown in Chaos or Divine anywhere. `/` jumps to search.

## How the numbers are made

**The data.** For every market (pair of items), league and hour, the exchange publishes the volume traded on each side, the lowest and highest executed ratio, and the lowest and highest stock on offer. There is no order book and no individual trades. An item usually has two markets that matter: against Chaos and against Divine.

**Prices.** The hourly price of a market is its volume-weighted average (Chaos paid ÷ items received). Divine-market prices are converted at that same hour's Divine price. The two markets of an item are kept apart, because they are not the same price: the Divine market is typically several percent dearer. The published lowest/highest ratios are not used to set levels — single-unit rounding and joke trades (a Divine for one Chaos) pollute them in most hours.

**Fair value.** A local-level Kalman filter on log price per item. The item's own returns are split into real movement and bounce (negative lag-one autocovariance), hours that traded more value count for more, and prints far outside the forecast are down-weighted. A print very far out is set aside until the next print confirms it, and only an hour that traded at least half its usual number of items can confirm it: two thin hours of joke trades in a row do not move the value. Parameters are re-fitted on trailing data only, so the series is causal: the value shown for an hour used nothing from after it.

You can check the model on any league: `python -m vaalstreet scorecard --league Mirage` compares fair value with the latest print as a predictor of where the item trades over the following six hours.

**Fair value range.** The desk keeps a range around each fair value, from one standard deviation below to one above. It says how sure the model is of the value *right now*: narrow for an item that trades steadily every hour, wide for a thin or jumpy one, and wider while an item does not trade. Like fair value, the range at an hour uses nothing from after it. It is not a forecast. Prices keep moving, so later prices often land outside it, and most often for busy items, whose range is narrowest. Measured on two months of real hours (the first week of a challenge league, and Standard), the next hour's price fell inside the range about one time in five for the 50 most traded items of the new league and a bit under half the time in Standard, and the next six hours' average price one time in ten and about half the time.

**Experiment: one value per item.** `python -m vaalstreet scorecard --two-market` tries a different model. Instead of pricing the Chaos market and the Divine market separately, it fits one value per item plus how much more the Divine market pays, a gap that changes only slowly. Prints from both markets then inform the same value, which helps most when one market trades only now and then. The table compares it with each market's own fair value, the blended fair value and the last print. The desk does not use this model yet.

**Flip plans.** For each item and pair of markets, the desk looks at where each market's *hourly average* has traded relative to fair value over the last two days. A plan buys at the level below which a given share of the buy market's volume traded, and sells at the level above which the same share of the sell market's volume traded — levels real volume went through, weighted so that an hour in which three units changed hands at a silly ratio cannot set them, and never more than about 1.5× from fair value. Three kinds: within the Chaos market, within the Divine market, and **Chaos → Divine**, which buys from players dumping loot for Chaos and sells to players paying Divines. The patience level with the highest expected profit is kept. Orders are given as whole-number ratios the exchange accepts. Each plan also shows its own last week: the same rule replayed through the backtest simulator on a fixed stake. Enter your bankroll and the page assembles a session plan from the plans that sold and made money that week, at most a quarter of the bankroll per item.

**Share of the flow.** How fast a plan fills depends on how much of the market's volume at your price comes to you. The desk assumes 10% until it has your own orders to learn from. For each order you track, it counts the units that traded at or beyond your price while the order stood (for a Divine-market order, at that hour's Divine price) and compares them with what you recorded as filled. An item's measured share is all its filled units divided by all those traded units, separately for its Chaos and Divine markets. An order still open counts once it has stood three hours or filled something. An order that filled completely may have been able to take more, so it can only pull the estimate down: the desk errs on the slow side. A few orders are a noisy guide, so the share a plan uses leans toward 10% as if 10% were three orders of its own: one order moves it a quarter of the way to what you measured, ten orders about three quarters. A plan uses the smaller share of its buy and sell markets, since the slower side sets the pace, and says where its share came from ("your fills, 4 orders" or "default 10%"). Only orders and fills up to the latest published hour count. `python -m vaalstreet fills` lists what your orders measured.

**Gold and time per plan.** Every order costs Gold for each item it buys or sells. For each item the desk uses the Gold per item you paid in your recorded trades and orders; if you have none for it, the fee you entered as your assumption on the Flips page; otherwise it says "Gold unknown" and leaves the Gold figures empty (the plan stays on the board). A flip buys and sells every unit, so it costs twice the fee per unit; Chaos per 1,000 Gold = 1,000 × (sell price − buy price) ÷ (2 × fee), and Gold per hour is that Gold at the units per hour the plan expects. A conversion pays the fee on every input bought and every result sold (Chaos needs no order on either side); a recipe of your own with several inputs pays for each. A round trip pays it on the first item bought, on each item given up in a swap (a Divine stop is an order like any other), and on the last item sold. A swap with a vendor is not an exchange order and costs no Gold, and Chaos you hand to a vendor never had to be bought. The game does not say what a swap costs, so the desk assumes it costs the fee of the item you give, as if you sold it. If any item on the path has no fee, the trip's Gold is unknown.

Each plan also counts the orders you post: two for a flip, one or two for a conversion, one more than the number of items for a round trip. Minutes = orders × minutes per order, a setting that starts at 1 and is your own guess. "Per hour of your time" assumes you post one batch of orders for each hour the market runs: it is profit per hour × 60 ÷ minutes.

Enter **Gold to spend this session** and the session plan takes plans by Chaos per 1,000 Gold instead. Gold is paid when an order is posted, so each plan counts against the budget the Gold to post its buy and sell orders once at the size of its stake: the units the stake buys, times the fee, for each side. Plans are added until the budget is spent; a stake the Gold left cannot fully pay for is shrunk, and dropped if not even one lot fits. Gold per hour stays on each plan as extra information. Plans whose Gold is unknown are not ranked: they are listed under "Gold unknown".

**Conversions.** Read from the game data: a divination card's stack size and reward; shards and splinters that combine into their item at a full stack; and essences and oils, where three of one tier sell to a vendor for one of the next (tiers come from the item IDs). Chains are followed up to three steps — nine of one tier for one of the tier two above. Each is compared with its result at fair value, and again "in a hurry" (buy the inputs in the dearer market, sell the result in the cheaper one). Try a recipe once in game before buying in bulk.

**Your own recipes and vendor prices.** No source the desk may read lists recipes or what NPC vendors sell, so the desk ships none beyond those above and lets you type in the ones you know: a recipe (say, 3 of one item and 1 of another make 2 of a third) or a vendor price (a vendor sells an item for 5 of a currency). They are kept with your ledger for every league. Your recipes are priced exactly like the built-in ones; vendor prices become swaps in round trips. Every conversion and round trip shows whether you marked it confirmed or failed in game, or still unchecked, and anything you typed in starts unchecked. Changing a count makes a new entry that starts unchecked again.

**Round trips.** Some items trade directly for each other in small side markets. The desk walks every path of up to three swaps — buy A, swap for B (then C, then D), sell — at the rates those markets typically traded at over the last day. A path never visits an item twice. A swap whose rate is more than three times off what the two items' own prices say is left out, and so is a Divine stop for an item whose Divine market is more than three times off its Chaos market: in thin markets that is joke trades (one Vaal Orb "fetching" five Orbs of Scouring every hour), not a trade you can repeat. They are thin, so each path also shows what went through its thinnest swap in a day and the value of orders resting there in the latest hour — whether the swap can be taken now or needs an order of your own.

Two more kinds of swap can join a path. One goes through Divine: sell A for Divines in A's Divine market and buy C with them in C's. That only pays when A's Divine market is dearer than its Chaos market by more than C's is, so the items on either side of Divine are bought or sold in their Chaos markets, and a path through Divine is shown only when it beats selling A for Divines and stopping there (that is already a cross flip). Divine markets are deep, so to keep them from crowding out the side markets only the best Divine path is shown for each item you start with and each item you buy with the Divines. The other is a vendor price you entered: it works one way, at any volume, so a path's depth comes from its exchange swaps, or from the markets you buy and sell in when the vendor is the only swap.

**Trade check.** Type an offer you see in the order window — the item, the market, whether you would buy or sell, and the ratio with the items first ("64 items for 1 Divine") — and the desk answers go, caution or no-go. The offer is judged against the value of *its own market*, because the Chaos and Divine markets of an item are different prices; a Divine-market ratio is converted to Chaos at the current Divine price. A buyer gets "go" when the price is under that value by more than the model's uncertainty (never less than 2%, since whole-number ratios and Gold fees eat that much), and "no-go" when it is over by as much; a seller the other way round; in between is "caution". A market that traded in fewer than a quarter of the last day's hours, or not in the last six, never gets more than caution. The answer also shows the offer against the item's overall fair value and range, whether it beats, matches (within 4%) or misses the flip plan's order for that side and market, the market's volume per hour, and how many hours of that volume the stock on offer is.

**Stash.** Enter what you hold, or use the positions your recorded trades add up to. Each line is valued at fair value with a low and a high, and at the Chaos market's and the Divine market's own prices, so you see which market pays more and by how much. Each line also says how many hours of that market's volume it is; above three hours, selling it all at that price will push the price. The total's low and high add up every line's low and high, which assumes every price is wrong in the same direction at once — wider than the truth, on purpose. The stash you enter is kept in your ledger file.

**Small bankrolls.** The exchange trades whole lots: "64 decks for 1 Divine" cannot be sold by someone holding ten decks. Enter your bankroll and every flip is re-cut to whole-number orders that purse can post, its margin recomputed from the prices those orders really state; plans that cannot be run are dropped, and below the price of one Divine everything stays in the Chaos markets. The Arbitrage page filters to conversions and round trips the bankroll can start.

**Bankroll ladder.** `ladder` runs all of that at a series of purse sizes: 25, 50, 100, 200 and 400 Chaos, then 1, 2, 5 and 10 Divines at today's Divine price, sorted by what they are worth in Chaos. For each step it shows how many flips, conversions and round trips that purse can start, what opens up that the step below could not run (for example, "Divine-market flips open at 2 Divines" when one lot there costs about 285 Chaos), and the session plan's Chaos per hour next to what the same plans made over the last week. That last-week figure is the replay's return on a 1,000 Chaos stake, scaled to the stake the plan puts in; small lots round worse than big ones, so take it as a rough guide. "Hours to next" divides the gap to the next step by the lower of the two rates for each plan. Then it plans a session from your purse: run these plans until the purse reaches the next step, then switch to the plans that fit the bigger purse. The purse grows at that same cautious rate, so the plan is a projection from last week's prices, not a promise.

**League start.** Leagues are aligned on days since their first trade. The playbook takes every item on today's league day in each past league and measures its move over the chosen horizon, in Divine by default (Chaos loses value through a league, so Chaos returns flatter everything). Every row shows the per-league outcomes and how many leagues it rests on. A league whose start is not on disk is left out rather than misaligned.

Precedents come from the same family of league, and only from leagues that have ended. The desk sorts every league into a family from its name and dates: challenge leagues; their Hardcore, Ruthless and Hardcore Ruthless versions (from the name's prefix); Standard and the other permanent leagues; private leagues; and **event leagues**. An event is a short league that starts at least a week after a challenge league and runs inside its life, as Phrecia ran next to Settlers, or one whose name says "Event". A new challenge league only starts when the last one ends, so that is enough to tell them apart without a list of past events. Challenge leagues learn only from past challenge leagues and events only from past events, so a short event does not skew what a new challenge league is expected to do.

**Launch day.** `playbook --launch` (and the League start page's data) goes hour by hour through the first 72 hours of each past league in the family. For every item it shows when its market first traded ("wakes up"), what it did first (rose or fell 5% from its first three traded hours, or stayed flat), and where it stood at hours 6, 24 and 72 against its first-day value, the volume-weighted average of the league's first day. Across leagues it gives the median of each, and how many leagues agree on the first move. With fewer than two past leagues whose first 72 hours are on disk, it says so instead of pretending to a pattern.

**When to buy Divines.** `divine-timing` looks at the Divine's daily price in Chaos in each past league of the family, finds the day it was cheapest (when Chaos bought the most Divines), and names the three-day stretch that holds the most of those days, for example "In 4 of 5 past leagues the Divine was cheapest in Chaos between day 1 and day 3". It always says how many leagues that rests on, and where today's league day stands.

**Meta shifts.** `meta` compares each item's demand, the units traded in the last 24 hours against league day 1, with the same league hours in every past league of the family. Units, not Chaos, because Chaos loses value through a league; day 1 rather than day 0, because the first day is a scramble. An item whose demand grew at least 1.5 times and faster than in any past league is a candidate for a meta shift: players are using it for something they did not before. Each row shows the growth against the past leagues' lowest, median and highest, and how many leagues that is.

**Other leagues' prices.** `cross-league` puts the same item, on the same hour, next to its price in this league's Hardcore, Ruthless and Hardcore Ruthless versions and in Standard. Each league has its own Chaos, worth a different number of Divines, so items are compared in each league's own Divines, and the Chaos and Divine markets are compared separately. The Divine's price in Chaos gets its own line. Over the last week, the gap is taken per day (the median of the hours both leagues traded), and it is called **structural** when it held on at least 3 days and 12 hours, kept the same sign on 80% of the days, is at least 3%, and moves less from day to day than its own size. A gap that flips or closes is **noise**, and one that stays within 3% is **none**. Items cannot move between leagues, so a gap is not a trade: it tells you which differences are real when you read prices from another league, and which will close.

**Backtests.** One league is replayed hour by hour. Orders posted at hour *t* meet hour *t+1*.

- A resting order fills at its limit only in an hour whose average price traded through it, and never at a better price.
- A market order pays the hour's average plus slippage (default 3%).
- A fill takes at most a set share of that hour's volume (default 10%).
- The queue model (`--fill-model queue`) replaces that share with a waiting line. When you post an order, the stock already on offer on your side of that market is the queue ahead of you. For a sell, that is the items on offer. For a buy, it is the Chaos bid for the item, counted in items at your price. The exchange publishes the lowest and the highest stock of each hour, and the backtest takes the lowest, because it is what rested through the whole hour. Your order fills only after that much volume has traded through your price, counting from when you posted. Moving an order to a new price sends it to the back of the queue. Making it bigger keeps its place, which flatters it a little. Once the queue ahead of you has traded, your order can take all the volume that trades through your price, so a thin queue can fill faster than the 10% share. Market orders do not queue and keep the 10% share. The exchange does not explain the stock figures, so this is a guess: if they count orders at every price, the queue is too long for a sharp price; taking the hour's lowest, and treating unknown stock as none, can make it too short. Strategies that move their prices every visit, like `swing`, rejoin the back each time.
- No leverage and no shorting. Sizing is on a fixed bankroll unless you ask to compound. A buy that finds no cash left does not count as a fill. Earlier versions counted it, so fill counts of runs that ran out of cash are lower now; their profit is the same.
- `attention` is how many hours pass between your visits; orders stand in between.
- A play schedule (`--schedule "mon-fri 19-22"`) limits your visits to the hours you actually play. Times are UTC, and the end hour is not included, so that example is three hours each weekday evening. A visit happens when an hour's prices come out, which is when that hour ends. Inside a session, `attention` still sets how often you come back. Your orders keep standing between sessions.
- Whole lots (`--whole-lots`) make the backtest trade like a real purse, the same way the Flips page does when you enter a bankroll. Each order becomes a whole-number ratio, like "33 Chaos for 10", that your cash can pay for. The ratio is rounded in your favour, so a buy never states more than your limit and a sale never states less. It fills only in whole lots, at the price that ratio states. An order you cannot afford even one lot of is never posted. A Divine-market order states whole Divines, so what it costs in Chaos moves with the Divine price until it fills. Market orders fill whole items.
- Several strategies can share one bankroll (`--portfolio`). Each one sees only its own items and orders, and sizes its orders as if the bankroll were its own. They compete for the same Chaos, the same Gold budget and the same share of each hour's volume. Conflicts are settled in the order you list them: every hour the first strategy's orders fill first, and the next ones fill from what is left. The result shows the total and each strategy's profit, its share of the profit, its Gold, and the most and average Chaos it had tied up in items.
- Every run also measures the risk of what you hold. Each hour, it values your items at fair value and works out the **worst drop**: what your holdings would lose on a bad day for these items, about 1 day in 20. A bad day comes from how much each item's fair value moved hour to hour over the last 7 days, stretched to how long you expect to hold (`--risk-hours`, default 24). The result shows the most you held, the worst drop at any hour and the average one over the run. It assumes every item has its bad day at once, which overstates a calm market, and a crash can do worse than a 1-in-20 day. For an item's first half day of prices the drop counts as zero.
- You can share a strategy (`--export file.json`). The file holds the strategy, its settings, the backtest options, the result for every league you ran, the desk version and, for your own strategy file, its code. Nothing is uploaded; passing the file on is up to you. `strategy import file.json` shows what a file holds. A file with code shows that code under a warning, because the code runs on your computer with your permissions when you backtest it. It is saved to the `strategies` folder only when you add `--yes`, and never over an existing file.
- **Strategy search** (`search swing --grid 'reach=0.2,0.3;top=20,40'`) tries every combination of the settings you list, and the run options attention, schedule, whole lots, fill model, participation, slippage and Gold per unit, up to 200 combinations. It judges them the way you would have used them at the time. The leagues are put in order of their start. Each league is tested with the setting that did best on the leagues that had already *ended* before it began, so a league still running at its start, such as Standard or the tail of the previous league, is never used to choose. The report shows, for each tested league, the chosen setting's result there next to the strategy's default settings and holding Divines, the average over those leagues, how many settings were tried and how often the choice changed from one league to the next. The best result on the earlier leagues is shown only as "fit on earlier leagues": it is the best of many tries, so it flatters. The score it maximises is the return over holding Divines by default (`--score return` for the Chaos return, `--score drawdown` for return over the worst drop). A schedule contains commas, so give the grid as JSON for it: `--grid '{"schedule": ["sat,sun 10-14", "mon-fri 19-22"]}'`. Each setting runs once per league; on a two-week league a run takes a few seconds, so a large grid over many leagues can take a while. `--export` saves the setting it would pick for the next league, with the table.
- Gold (`--gold-per-unit`, `--gold-budget`) is charged for every unit you post. Moving an order to a new price or making it bigger is a new post and costs Gold again. Leaving it alone, or letting it shrink, costs nothing. We assume a cancelled order gives no Gold back, which is the cautious choice. Once the budget is spent, no new orders go up, but orders already standing can still fill. The result shows the Gold spent and the Chaos earned per 1,000 Gold.

**Alerts.** Alerts are checked once an hour, when a new hour's prices arrive. A price alert fires when a value crosses your threshold. A flip-plan alert fires when a plan for your item, or for any item, reaches your margin and profit per hour. A round-trip alert fires when a round trip reaches your edge and the orders waiting at its thinnest swap are worth at least the Chaos you set, so there is something to take right now. An order alert fires when one of your recorded orders should be re-priced, cancelled or collected. A league alert fires when an item is cheaper or dearer than on the same day of past leagues by your percentage, or when past leagues moved it by at least your percentage over the next days. An alert rings once when its condition starts. It rings again only after the condition has gone away and come back, and never twice for the same thing within 6 hours, so a price hovering on your line does not ring every hour. Each alert writes a short title that can be read aloud, such as "Test Orb filled, post the sell".

### What the model cannot see

Read flip and backtest figures as a ceiling, not a forecast.

- **Gold.** Every order costs Gold, and the fee schedule is not published. The desk does not guess it: it learns the Gold per item from the trades and orders you record in the Ledger (the middle value of your records), and shows the fee as unknown until you have recorded some, unless you enter an assumed fee yourself. Fees can differ by item and market in ways your records do not show, and what a swap between two items costs is assumed, not known. Backtests charge a Gold fee per unit that you choose, because the real fee for each item is not known to them. Gold is often the real limit on how much you can flip, which is why plans can be ranked by Chaos per 1,000 Gold.
- **Queue position.** Orders at the same ratio fill in time order. The desk shows how many hours of volume already rest on each side, but cannot know where you would stand. The backtest's queue model guesses your place from the published stock on your side, which may count orders at worse prices than yours.
- **Hourly granularity.** A level is counted as reached only when the whole hour's average traded through it, which is conservative; the path inside the hour is unknown.
- **Small samples.** There have only been a handful of leagues since the exchange launched. League-start patterns are precedents, not odds.

Check the in-game order book before committing size.

## Command line

Everything the desk shows is also available from the shell.

```sh
python -m vaalstreet status                      # leagues and coverage
python -m vaalstreet price "Divine Orb"          # latest hourly prices, both markets
python -m vaalstreet flips --kind cross          # maker plans for the live league
python -m vaalstreet fills                       # how fast your tracked orders filled, against what plans assume
python -m vaalstreet check "Orb of Alteration" --market divine --side buy --ratio "64 items = 1 divine" --stock 300
                                                 # go / no-go for an offer in the order window (items first)
python -m vaalstreet stash set "Divine Orb" 3     # what you hold; stash alone lists it, stash --ledger values your positions
python -m vaalstreet arb --swaps 3               # sets against their rewards, and round trips
python -m vaalstreet recipe add --give "5 Some Card" --get "1 Some Orb"   # a recipe of your own
python -m vaalstreet recipe add --vendor --give "3 Chaos Orb" --get "Some Shard"   # a vendor price
python -m vaalstreet ladder --from 25 --hours 6  # what each purse size can run, and when to switch plans
python -m vaalstreet playbook --horizon 14       # what past leagues did next from today's league day
python -m vaalstreet playbook --launch           # the first 72 hours of past leagues, item by item
python -m vaalstreet divine-timing               # on which league days the Divine was cheapest in Chaos
python -m vaalstreet meta                        # items in more demand than any past league at this point
python -m vaalstreet cross-league                # the same items in Hardcore, Ruthless and Standard: real gaps or noise
python -m vaalstreet backtest swing --league Mirage --league Allflame --attention 4
python -m vaalstreet backtest swing --fill-model queue   # orders wait behind the stock already on offer
python -m vaalstreet backtest swing 'reversion:{"entry_z": -2.5}' --portfolio
                                                 # two strategies on one bankroll; the first listed fills first
python -m vaalstreet backtest examples/cheap_vs_divine.py:CheapVsDivine --league Mirage --league Allflame --export mine.json
python -m vaalstreet search swing --grid 'reach=0.2,0.3;attention=1,4' --leagues Mirage,Settlers,Keepers
                                                 # each league tested with the setting that won on leagues that ended before it
python -m vaalstreet strategy import mine.json   # shows the code; add --yes to save it to your strategies folder
python -m vaalstreet scorecard --league Mirage   # is fair value better than the last print?
python -m vaalstreet research orderbook          # experiment: does resting stock predict the price? (or: research all)
python -m vaalstreet sql "SELECT league, family, hours FROM leagues ORDER BY first_hour"
```

`--league` defaults to the live challenge league. Run any command with `-h` for its options.

**Experiments.** `research` tests claims about the market that the desk does not act on yet. Examples are "changes in resting stock predict the price", "the Divine market's premium follows the league day" and "this detector catches crashes early". Each claim must meet rules that were written down before any real run, in `docs/ideas.md` under "How a measurement passes". In short, a forecast is tuned on earlier leagues and must beat both fair value and the last print on later ones, in most leagues and pooled, by a margin that chance cannot explain even after counting every setting tried. Each claim prints "pass", "fail" or "not enough data", with its numbers and the number of settings tried. Until the archive holds several full leagues with a known start, most results say "not enough data". The `relationships` and `recipes` measurements print "candidates": items to check in game, never trades. `spread`, `queue-vs-share` and `ladder-rates` print "report only", because they only describe the data and the desk's own models.

### Your own strategy

A strategy is a class with `step(ctx)`. It sees only what was published up to the current hour.

```python
# examples/cheap_vs_divine.py (shortened)
import numpy as np

class CheapVsDivine:
    """Buy liquid items under fair value in the Chaos market, offer them 8% up in the Divine market."""

    def step(self, ctx):
        if ctx.t < 48:
            return
        traded = np.nansum(ctx.history("value", 48), axis=0)
        liquid = traded >= np.sort(traded)[-30]
        fair = ctx.fair
        units = np.where(liquid, 0.03 * ctx.bankroll / fair, 0.0)
        ctx.quote_buys("r", np.where(liquid, fair * 0.96, np.nan), units)       # "r" = Chaos market
        ctx.quote_sells("b", ctx.average_cost * 1.08, ctx.position)             # "b" = Divine market
```

```sh
python -m vaalstreet backtest examples/cheap_vs_divine.py:CheapVsDivine --league Allflame
python -m vaalstreet backtest examples/cheap_vs_divine.py:CheapVsDivine --params '{"markup": 0.12}'
```

`ctx` offers `fair`, `z` (how surprising this hour's print was), `history(column, hours)`, `swing(leg, reach)`, `position`, `average_cost`, `cash`, `bankroll`, and `quote_buys` / `quote_sells` / `buy` / `sell`. Built-in strategies (`hold`, `swing`, `reversion`, `momentum`) are in `vaalstreet/backtest.py` and are short enough to read as examples.

### In a notebook

```python
from vaalstreet import connect
from vaalstreet.panel import Panel
from vaalstreet import pricing

con = connect()                                   # DuckDB views over the Parquet archive
con.sql("SELECT * FROM league_prices WHERE name = 'Divine Orb' AND league = 'Mirage' LIMIT 5")

panel = Panel.load(con, "Mirage", ("price", "value"))   # dense hour x item arrays
fair = pricing.fair_value(panel["price"], panel["value"])
```

Views: `markets` (as published), `legs` (each market from both sides), `prices` (per item, league and hour, with both markets), `leagues`, `items`, `league_prices` (prices plus names and league day).

## Files

All data lives under `data/` in the checkout (`~/.vaalstreet` if installed as a package; override with `VAAL_DATA_DIR`).

```
data/raw/poe1/<YYYY-MM>/<hour>.json.gz     the exchange's digests, bytes as served
data/parquet/markets|prices/...            derived, rebuilt from raw at any time
data/items/poe1.json                       item names, categories, set recipes
data/icons/                                item icons, cached on first view
data/strategies/                           strategy code you imported with `strategy import`
data/desk.sqlite                           your ledger, orders, sessions, recipe checks, watchlist, alerts, stash — the only file worth backing up
```

To move the archive to another machine without downloading it again, `python -m vaalstreet snapshot <folder>` packs the indexed history into 9 MB parts, and `python -m vaalstreet restore <folder>` rebuilds it from them.

Other settings: `PORT` (default 3000) and `POE_USER_AGENT` (the identification string sent with exchange requests). The server binds to `127.0.0.1` and refuses requests from other sites.

```
vaalstreet/archive.py   download         vaalstreet/desk.py      market table, flips, arbitrage
vaalstreet/ingest.py    raw -> Parquet   vaalstreet/leagues.py   league families and league-start analytics
vaalstreet/db.py        DuckDB views     vaalstreet/backtest.py  simulator and strategies
vaalstreet/panel.py     dense arrays     vaalstreet/ledger.py    trades, orders, sessions, recipes and checks, alerts, stash
vaalstreet/pricing.py   models           vaalstreet/server.py    local API; vaalstreet/web/ is the UI
vaalstreet/check.py     trade check and stash valuation
vaalstreet/ladder.py    bankroll ladder and compounding plan
vaalstreet/calibrate.py share of the flow learned from your orders
vaalstreet/sharing.py   strategy files: export and import
vaalstreet/search.py    strategy search, judged walk-forward on later leagues
vaalstreet/research.py  experiments with pass rules fixed in advance
```

Run the tests with `pip install -e .[dev] && pytest`. They build a small synthetic exchange archive with known structure and check every layer against it; none touch the network.

## License and credits

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.

The code is released under the [MIT License](LICENSE). That license does not cover the following, which stay with their owners:

- Path of Exile, its item names and item icons are the property of Grinding Gear Games. Icons are fetched from GGG's CDN at run time to label items in this free, non-commercial tool; none are distributed with it.
- Exchange history comes from GGG's documented [Currency Exchange API](https://www.pathofexile.com/developer/docs/reference).
- Item metadata comes from the community [RePoE](https://repoe-fork.github.io/) export of the game's data files.
- Geist and Geist Mono are distributed under the font licenses in `vaalstreet/web/fonts/`.
