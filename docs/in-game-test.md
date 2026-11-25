# In-game test: stack check, screen check, search string

Everything the desk does with prices is tested offline against a synthetic archive, and the pages
were clicked through in a headless browser. Three things cannot be tested that way, because they
depend on the game's own window and on Windows: the stack check (reading a copied stack), the
screen check (reading the exchange window), and the search string (pasted into the game). They
were written from other tools' source code and from descriptions. This script is the first test
against the live game.

It takes about 30 minutes. Do the steps in order; each one says what to do, what you should see,
and what to send back when it differs. Nothing here sends anything to the game: you press every key.

## What you need

- The game in English, in windowed or borderless windowed mode.
- The desk running in WSL from the top of the stack (branch `stack-docs`).
- The companion running under Windows Python 3.10 to 3.12 from the same branch.

## Setup

### 1. The desk, in WSL, on a copy of your data

The test writes practice orders and stash lines. A copy keeps them out of your real ledger.

```sh
mkdir -p ~/vaal-test
cp -r data/parquet data/items data/icons ~/vaal-test/     # in the checkout that holds your data
git switch stack-docs                                      # in a checkout of this branch
VAAL_DATA_DIR=~/vaal-test python -m vaalstreet serve --port 3111 --no-sync
```

Open `http://localhost:3111` in a browser on Windows. Press `H` for the hideout window and keep
it visible: readings and verdicts appear there. `--no-sync` means the prices are as old as the
copy, which does not matter for this test.

### 2. The companion, on Windows

In PowerShell, in a Windows copy of the repository:

```
git fetch origin
git switch stack-docs
py -3.12 -m venv .venv-screen
.venv-screen\Scripts\activate
pip install -e ".[screen]"
```

With uv, skip the three lines after `git switch` and start every companion command below with
`uv run --python 3.12 --extra screen` in place of the bare `python`. A plain `uv run python` uses
uv's own environment, which lacks the screen libraries, and stops with "Missing rapidocr_onnxruntime".

If the game runs as administrator, start PowerShell as administrator too.

## Part A: the stack check

Start it with the diagnostic switch:

```
python -m vaalstreet screen --desk http://localhost:3111 --stacks --why
```

| # | Do this | You should see, in the companion's console | If not, send back |
|---|---|---|---|
| A1 | Click into the game. Open your currency stash tab, hover the pile of an item you hold many of, press **Ctrl+C**. | `Copied in game. Read: 2,448 Portal Scroll` (your item and count), then lines with its worth and the better market. The hideout window shows the same under "What the screen check read". | Everything the console printed. If it printed nothing at all, say so: the key was not heard. |
| A2 | If A1 printed `Ctrl+C heard, but ignored: the window in front is "…"`. | This means the key works but the game's window is not recognised. | The exact name in quotes. That is the one fact needed to fix it. |
| A3 | If A1 printed `Ctrl+C heard in game, but nothing was copied`. | The game wrote the clipboard later than half a second, or not at all. | Whether the item was under the cursor, and whether pasting into Notepad right after shows the item text. |
| A4 | In Notepad, paste what the game copied in A1. | Text that starts with `Item Class:` and has a line `Stack Size: 2,448/40`. | The pasted text, as it is. This is the layout the reader assumes. |
| A5 | Hover an item that does not stack (a map or a unique) and press Ctrl+C. | `Read: 1 <name>`, then either a worth or `No item called '<name>' traded in <league>` (right for anything the Currency Exchange does not trade, such as a unique). | The console lines. |
| A6 | Hover a stack and press **Ctrl+Alt+C** (the game's copy with details). | The same reading as with Ctrl+C. | The console lines. |
| A7 | Switch to a browser, select any text, press Ctrl+C. | `Ctrl+C heard, but ignored: the window in front is "…"`. Nothing else. | Anything else it printed. It must never show what you copied. |
| A8 | Back in game, with nothing under the cursor, press Ctrl+C. | `Ctrl+C heard in game, but nothing was copied`. | The console lines. |

Now stop the companion (Ctrl+C in its console) and start it with `--stash` in place of `--stacks`.
Open the Ledger page of the desk and look at the Stash panel while you do these:

| # | Do this | You should see | If not, send back |
|---|---|---|---|
| A9 | Copy the pile from A1 again. | `Stash: Portal Scroll set to 2,448.` and `More than a full stack of 40, so it is the pile in your tab: it never adds.` The Stash panel has the line, with "Counted: 1 min ago". | The console lines and what the panel shows. |
| A10 | Move a small stack of the same item (fewer than a full stack) into your inventory and copy that. | `Stash: Portal Scroll 2,448 + 12 = 2,460 (another stack).` | The console lines. |
| A11 | Copy the same small stack again. | `Stash unchanged: the same count as the copy before…` | The console lines. |
| A12 | Drop the small stack back into the pile and copy the pile. | `Stash: Portal Scroll set to 2,460.` It does not become 4,920. | The console lines. |
| A13 | Does a count above a full stack ever appear outside a one-space tab (for example in a quad tab or the inventory)? | No. | Where you saw one. The pile rule depends on this. |

## Part B: the screen check

Stop the companion and start it without `--stacks`:

```
python -m vaalstreet screen --desk http://localhost:3111
```

Skip `--calibrate` for this test. Without it the companion reads your whole main screen, which
works wherever the exchange window sits (it moves when the inventory or the stash is open). A
saved rectangle only makes a reading faster. If you already ran `--calibrate`, run it once more
and press Enter at the prompt: that goes back to the whole screen.

Open the Currency Exchange in game. Pick an item you want and Chaos Orbs as what you have.

| # | Do this | You should see | If not, send back |
|---|---|---|---|
| B1 | With the exchange window open and nothing typed, press **Ctrl+Alt+F9**. | `Read: buy 64 Orb of Alteration for 1 Divine` (your item and the market's ratio), then Go, Caution or No-go with reasons. | The console lines. |
| B2 | Look at "What the screen check read" in the hideout window. | The reading from B1, with the lines the text recognition returned. | **Copy those lines and send them, whatever happened.** They show the window's real layout, which nobody has seen yet. |
| B3 | Swap the two sides (you have the item, you want Divines) and press the key. | `Read: sell …`. | The lines from the panel. |
| B4 | Type amounts into both fields of the window (for example 900 and 200). Do not place the order. Press the key. | In the panel, the lines include your two amounts. "Posted this order…" opens a form that starts from them: ratio "2 Chaos for 9", 900 items. | The lines from the panel, and what the form showed. If the form asked you to type the ratio, the amounts were not read: the lines will show why. |
| B5 | If a name was misread in any step ("Couldn't tell which item …"). | Click the misread line in the panel, choose the item, Save. Press the key again: it now reads. | The misread text and whether the second press worked. |
| B6 | Place a small real order in game. Then click "Posted this order…" on its reading and Record. | The order appears under Orders on the Ledger page with its advice. | What differed between the order in game and the one recorded. |
| B7 | Look at the exchange window's list of your own orders. | Nothing to see on the desk: this is a question. | Does each order show its item's **name as text**, or only an icon? Does it show how much has filled, and the Gold fee? A screenshot of the list would answer all three. |
| B8 | Press the key while the exchange window is closed. | `Couldn't find the "I Want" and "I Have" labels…` | The console lines. |

## Part C: the search string

On the desk's Flips page, enter a bankroll so the session plan appears, and click **Copy search
string** on it. The panel shows one or more strings of the form `^(Breach.*|.*Trove|.*Alchemy)$`.

| # | Do this | You should see | If not, send back |
|---|---|---|---|
| C1 | In the Currency Exchange, open the item picker, click its search box, paste the string. | Only the plan's items are shown or highlighted. | What it showed. If nothing matched, try the same string without `^(` and `)$`, and say which form worked. |
| C2 | Paste the same string into the stash's search box (a tab that holds some of those items). | Those items are highlighted. | What it highlighted. |
| C3 | In the panel, set the length limit to 200 and copy the longer single string. Paste it in both boxes. | Either it works, or the box cuts it. | How many characters the box kept. That is the real limit. |

## Part D: one real round (optional, 10 minutes)

1. On Flips, pick a cheap plan. Post its buy order in game, click **Posted…** under it, Record.
2. When some of it fills, open Ledger, click **Filled…**. Note what number the prompt starts from
   ("about N more may have filled") and how close it was.
3. The desk offers the plan's sell order. Post it in game, Record.
4. Say whether any step asked you to type something the desk already knew.

## What to send back, in one message

- The console output of parts A and B (copy the whole console).
- The recognised lines from the panel for B1, B3 and B4.
- The pasted item text from A4.
- The answers to A13, B7, C1, C2 and C3.

## Afterwards

Stop the desk and the companion. `~/vaal-test` can be deleted. To use the new tools on your real
data, start the desk as usual from this branch: it adds new tables to `desk.sqlite` (snapshots,
baskets, the names you taught the screen check) and one column to the orders table, and changes
nothing that is already there.
