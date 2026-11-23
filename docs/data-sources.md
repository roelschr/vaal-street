# Vaal Street: rules for additional public data sources

Researched 2026-11-21 from primary sources. Pages that could not be read are named as gaps, not filled from memory.

Unreachable during this research: pathofexile.com/trade (HTTP 403), poe.ninja and poe.ninja/about (HTTP 403; the owner later saved poe.ninja's API reference from a browser, see section 2), and every poewiki.net page tried (Anubis bot-block page). I did not find GGG's forum "third-party tool policy" thread (forum thread 2077338 is an unrelated bug report).

Core sources:
- GGG developer docs, section "API Policies and Third-Party Requirements": https://www.pathofexile.com/developer/docs
- Authorization: https://www.pathofexile.com/developer/docs/authorization
- Reference: https://www.pathofexile.com/developer/docs/reference
- Changelog: https://www.pathofexile.com/developer/docs/changelog
- Terms of Use: https://www.pathofexile.com/legal/terms-of-use-and-privacy-policy

---

## 1. Screenshot trade check (hotkey → screenshot → text recognition → go/no-go overlay)

**Verdict: allowed with conditions.** The developer docs permit executable apps that run separately from the game and read the screen, with conditions. They never mention text recognition (OCR) by name, so it falls under "reading from the screen".

Evidence (https://www.pathofexile.com/developer/docs, "Executable apps that run independently from the game"):
- "Ordinarily we only allow the creation of applications that run completely separate from the game."
- "Apps that use information sourced from the screen, the clipboard, or the game's logs must follow our overlay rules".
- Overlay rules: "You may only read this information as long as the user is aware of what you are doing with that data." and "Reading from the screen or clipboard may only be performed once per manual key press."
- Macro rules: "Macros must be invoked manually by the user". Timers, file-change triggers and "reading the screen" are excluded as triggers. "Each macro invocation must have one set function (it cannot cycle between actions)." "The resulting function may only perform one action that interacts with the game (sending a single chat message or command counts as one action)."
- The docs do not set separate rules for overlay windows. The screen-reading rules above are the only constraint they give.
- Apps that "interact with the game or game files": "This behaviour is strictly against our Terms of Use (sections 7b, 7c, 7i)" and "will result in immediate account termination."
- Terms of Use section 7: 7b no modifying the client "or its data"; 7c no "automated software or 'bots'"; 7e no connecting to servers except through the game client; 7i no reverse engineering. https://www.pathofexile.com/legal/terms-of-use-and-privacy-policy
- The privacy notice says anti-cheat collects the running process list and memory/driver information to detect cheating (read only as a summary, not verbatim).

Comparable tools:
- Awakened PoE Trade (https://github.com/SnosMe/awakened-poe-trade) and its PoE 2 fork Exiled Exchange 2 (https://github.com/Kvan7/Exiled-Exchange-2) are hotkey price-check overlays. Their README and DEVELOPING.md files do not say how they read items (clipboard or screen), and make no statement about GGG approval or the Terms. Answering that needs their source code, or the Exiled Exchange 2 FAQ (https://kvan7.github.io/Exiled-Exchange-2/faq), which I did not read. I found no primary source that names a tool doing OCR of the Currency Exchange window.

Conditions for an implementation:
- One screenshot and one recognition pass per manual key press. No timer, no polling, no auto-trigger on window changes or log-file changes.
- The overlay only displays. It sends no keys, clicks or chat to the game, and does not move the mouse.
- Capture with ordinary operating-system screen capture. Do not read game memory, inject into the client, hook its rendering, or modify or read packed game files.
- Tell the user plainly what is captured and what it is used for. Keep screenshots local and don't keep them by default.
- If public, show "This product isn't affiliated with or endorsed by Grinding Gear Games in any way." (developer docs).
- Open risk: GGG does not mention OCR by name, and I did not find a staff forum post about it.

## 1b. Stack check (the player's own copy key → one clipboard read → what the stack is worth)

Added 2026-11-23. **Verdict: allowed with conditions**, under the same overlay rules as section 1: "Apps that use information sourced from the screen, the clipboard, or the game's logs must follow our overlay rules", "You may only read this information as long as the user is aware of what you are doing with that data." and "Reading from the screen or clipboard may only be performed once per manual key press." (https://www.pathofexile.com/developer/docs, re-read 2026-11-23). The docs' changelog for 3.30.0, dated 12/11/2026, says: "Updated the Policy wording around macros and overlays to be clearer about when they apply."

How the desk does it (`vaalstreet/clipboard.py`, `vaalstreet screen --stacks`):
- The player presses the game's copy key. The companion sends no key, so the macro rules, which cover "Automation of key-strokes (or other inputs) that affect the game", do not come into play. Awakened PoE Trade and Exiled Exchange 2 press the copy key for the player; the desk does not.
- One read of the clipboard's contents per press. Awakened PoE Trade reads the clipboard every 48 ms for up to 500 ms after one press (`main/src/shortcuts/HostClipboard.ts`, read 2026-11-23); the desk does not copy that. It waits for Windows' clipboard sequence number to change and then reads once. The sequence number counts changes and holds nothing of the contents.
- It is off unless the player starts the companion with `--stacks`, and the start-up text says what is read and when.
- A press counts only while the game is the window in front, told by its window class `POEWindowClass`, as Exile UI finds the game. The window's title is not used: a browser tab or a folder window can also be titled "Path of Exile". Text that does not start with "Item Class: " is dropped without being shown or sent.

Open risks:
- That waiting on the sequence number and then reading once satisfies "once per manual key press" is this project's reading of the rule. GGG has not said so.
- Nothing here has run on Windows or against the game: not the key detection, the wait, the window check or the copied text's layout, which was taken from the two tools' parsers ("Stack Size: 2,448/40").
- If the game writes the clipboard before the companion hears the key, the companion sees no change and says that nothing was copied. It never values the item copied before.
- If the game's window class is not `POEWindowClass` on some client, the companion never sees the game in front and stays silent. That is the safe side, and the class was taken from Exile UI's source, not checked on a running game.

## 2. Trade-site prices (uniques, gems, bases)

**Verdict: pathofexile.com/trade automation is not allowed. The Public Stash API is out of reach for a local tool. poe.ninja's economy endpoints are allowed under its usage rules, and the desk uses them for card rewards (see "poe.ninja" below).**

Evidence:
- The trade site's API is not documented. The docs say: "Requests for access to any other internal website APIs or in-game resources will be denied." and "It is against our Terms of Use (section 7i) to reverse-engineer endpoints outside of this documentation." (https://www.pathofexile.com/developer/docs). The trade page itself returned 403, so I could not read its own terms.
- Public Stash API: "Required scope: service:psapi" (reference). Service scopes need "a confidential client with the client_credentials grant type"; public clients "Cannot use any service:* scopes"; confidential clients need "a registered domain controlled by the application owner. We cannot accept IP addresses or localhost domains" (https://www.pathofexile.com/developer/docs/authorization). Changelog 3.20.0: guest access was removed and you "must now use an OAuth client with the service:psapi scope." Registration: "We are currently unable to process new applications." Executable apps "must use a public OAuth client if interacting with our APIs." So a localhost desktop tool cannot get psapi under the rules as written.
- Stash data delay: "There is currently a 5-minute delay on results using this API." (reference). A 2017 staff post (Bex_GGG) says whitelisted public sites can get undelayed data by emailing contact@grindinggear.com: https://www.pathofexile.com/forum/view-thread/2036957
- Rate limits (all GGG APIs, https://www.pathofexile.com/developer/docs): limits "are dynamic and can change at any time". Headers: `X-Rate-Limit-Policy`, `X-Rate-Limit-Rules` (e.g. ip, account, client), `X-Rate-Limit-{rule}` = hits:period:restriction seconds, `X-Rate-Limit-{rule}-State` = current state, and `Retry-After` on 429. Responses in the 4xx range count towards an invalid-request threshold; "Exceeding these limits frequently will result in your application access being revoked."
- User-Agent: "Any application that interacts with our API must set an identifiable User Agent header prefixed using the following format: `OAuth {$clientId}/{$version} (contact: {$contact}) ...`".
- Bulk/scheduled polling: the docs give no polling frequency. The stash stream is read by following `next_change_id`.
- Earlier research could not read poe.ninja (HTTP 403) and found only an unofficial write-up (https://github.com/ayberkgezer/poe.ninja-API-Document). That is superseded by poe.ninja's own API reference, below.

Conditions that still hold:
- Never call pathofexile.com/trade endpoints.

### poe.ninja

Source: poe.ninja's API reference, https://poe.ninja/docs/api, saved by the owner from a browser on 2026-11-21 (the site still refuses automated reads).

What their docs allow:
- "The economy overview endpoints documented on this page are the supported public surface of the poe.ninja API." For PoE 1 these are `GET /poe1/api/economy/leagues`, `/poe1/api/economy/exchange/current/overview?league=…&type=…`, `/poe1/api/economy/stash/current/item/overview?league=…&type=…` and `/poe1/api/economy/stash/current/currency/overview?league=…&type=…`. The league parameter is an id from the leagues endpoint.
- Not allowed: "The builds / profiles API, and every other non-economy endpoint (character, Path of Building, authentication), are internal … not available for third-party use." "Clients that misbehave against these endpoints, or make excessive use of the economy endpoints, will be blocked."
- No stability guarantee: "There is no versioning and no SLA, and breaking changes can happen to the economy endpoints without notice."
- Usage guidelines: responses are "HTTP-cached (roughly 5 minutes, ETag-based). Use conditional requests and respect the cache headers"; "PoE 1 overviews roughly every 15 minutes, PoE 2 roughly hourly"; "Send a descriptive User-Agent that identifies your app and a contact"; "Be reasonable with concurrency and volume"; "Don't use the API to directly replicate the site."
- Apps: "Desktop apps and other clients should proxy these requests through their own backend rather than calling the endpoints directly from end-user machines. That lets you cache responses, set a proper User-Agent, and control request volume."

How the desk uses it (`vaalstreet/ninja.py`):
- Only the economy endpoints above, and in practice only the stash item overview lists that the card rewards need (the six unique lists, `SkillGem`, `BaseType`) plus the leagues list. The client can read the exchange and stash currency overviews too, but the desk does not ask for them today.
- At most one request per list per hour (`MIN_INTERVAL`), counted from every attempt, success or failure, and kept on disk, so restarts and an unreachable poe.ninja do not cause extra requests. Every repeat request is conditional (`If-None-Match` with the last ETag); a 304 keeps the cached body.
- One request at a time, with a two-second pause between requests. On 429 or 5xx it backs off, honouring `Retry-After`; a wait longer than a minute is not slept out. After a refusal or an outage the desk pauses every list, for the `Retry-After` time or at least 15 minutes, so a rate limit on one list does not lead to requests for the others. Other 4xx answers are not retried until the next hour.
- User-Agent: `vaal-street/2.0 (+https://github.com/roelschr/vaal-street)`, the same string as every other request the desk makes, overridable with `POE_USER_AGENT`.
- The league id comes from poe.ninja's leagues list, matched to our league name. If there is no match, the desk asks for no prices and shows none; it never falls back to their first entry.
- If poe.ninja cannot be reached, the last answer is used and marked stale. Answers live in `ninja.sqlite` in the data directory, which can be deleted at any time.
- Credit: every row priced this way carries "prices from poe.ninja" and "reward priced by poe.ninja (stash listings, not the exchange)", with the listing count and the age of the price. The README's license and credits section names poe.ninja.
- Not replicating the site: the desk shows a price only next to the card set it values, never a poe.ninja price list. The arbitrage API returns these rows as `ninja_conversions`, apart from the exchange-priced `conversions`.
- It can be turned off with `python -m vaalstreet ninja off` (the `ninja_prices` setting). It is on by default, as the owner decided.

The proxy note, honestly: the desk's own local server acts as the backend their docs ask for. It caches responses, sets the User-Agent and controls the volume, and its API handlers read only the cache. But the desk runs on each player's machine, so **each install still calls poe.ninja itself**, at most once an hour per list (about nine conditional requests an hour while the desk runs), not through one shared server. The owner accepted this. If poe.ninja objects, the setting turns it off, and a shared backend would be the next step.

## 3. Other public game data

**Verdict: unclear. RePoE carries none of the listed items. poewiki.net could not be read.**

RePoE (fork), "Repository of Path of Exile resources for tool developers" (https://repoe-fork.github.io/). Published PoE 1 files: stat_translations, stats, mods, crafting_bench_options, npc_master, gems, gem_tags, base_items, tags, item_classes, essences, default_monster_stats, characters, flavour, fossils, mod_types, cluster_jewels, cluster_jewel_notables, cost_types, active_skill_types, uniques (.json). The repo README also lists mercenary_* files and says formats "may change at any time" (https://github.com/repoe-fork/repoe).
- Licence: the code is MIT, but "Contents of generated files (all files in the `data` directory) are owned by Grinding Gear Games and shall not be used or published without being in accordance with their terms of use." (https://github.com/repoe-fork/repoe/blob/master/LICENSE.md)

| Data | RePoE | poewiki.net |
|---|---|---|
| NPC vendor currency prices | no file | unread (blocked) |
| Fragment sets, scarab/fossil/catalyst/incubator recipes | no recipe file (fossils.json describes fossils, not recipes) | unread |
| Harvest lifeforce swaps | no file | unread |
| Kingsmarch outputs | no file | unread |
| Currency Exchange gold fees | no file; the GGG API reference I read does not describe gold fees either | unread |
| Patch notes per league | no | unread (Version_history blocked) |

poewiki.net: the licence (Path_of_Exile_Wiki:Copyrights), the Cargo tables and any bot policy all sit behind an Anubis bot-block page. That block is itself a sign that the wiki discourages automated fetching. Its licence and API rules are **unknown**. A human should check them in a browser before any code queries the wiki.

Conditions:
- Treat RePoE data as GGG's under GGG's terms. Fine for local use; don't redistribute it.
- Don't scrape poewiki.net while it serves a bot challenge. Use its Cargo API only if its published policy allows it, with a User-Agent and low frequency, and credit it according to its licence.
- Patch notes: GGG's own forum announcements are the primary source. Not researched here.

## 4. Finer than hourly Currency Exchange data

**Verdict: none.** The hourly digest is the only sanctioned source.

Evidence (https://www.pathofexile.com/developer/docs/reference): the endpoint gives "aggregate Currency Exchange trade history across all leagues grouped into hourly digests"; "there isn't any way to get data from the current hour"; "we may, at a later date, remove old history entries"; and callers "should wait until the next hourly boundary before calling this endpoint again." Changelog 3.29.0: "The Currency Exchange endpoint is now available publicly. Use the CDN API to load cached copies of the data." (`https://web.poecdn.com/api/currency-exchange[/<realm>][/<id>]`).

Conditions: keep polling once per hour, after the hour boundary. Follow the rate-limit headers, and keep the archive, because old history may be removed.
