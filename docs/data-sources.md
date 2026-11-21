# Vaal Street: rules for additional public data sources

Researched 2026-11-21 from primary sources. Pages that could not be read are named as gaps, not filled from memory.

Unreachable during this research: pathofexile.com/trade (HTTP 403), poe.ninja and poe.ninja/about (HTTP 403), and every poewiki.net page tried (Anubis bot-block page). I did not find GGG's forum "third-party tool policy" thread (forum thread 2077338 is an unrelated bug report).

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

## 2. Trade-site prices (uniques, gems, bases)

**Verdict: pathofexile.com/trade automation is not allowed. The Public Stash API is out of reach for a local tool. poe.ninja's rules are unclear (its site could not be read).**

Evidence:
- The trade site's API is not documented. The docs say: "Requests for access to any other internal website APIs or in-game resources will be denied." and "It is against our Terms of Use (section 7i) to reverse-engineer endpoints outside of this documentation." (https://www.pathofexile.com/developer/docs). The trade page itself returned 403, so I could not read its own terms.
- Public Stash API: "Required scope: service:psapi" (reference). Service scopes need "a confidential client with the client_credentials grant type"; public clients "Cannot use any service:* scopes"; confidential clients need "a registered domain controlled by the application owner. We cannot accept IP addresses or localhost domains" (https://www.pathofexile.com/developer/docs/authorization). Changelog 3.20.0: guest access was removed and you "must now use an OAuth client with the service:psapi scope." Registration: "We are currently unable to process new applications." Executable apps "must use a public OAuth client if interacting with our APIs." So a localhost desktop tool cannot get psapi under the rules as written.
- Stash data delay: "There is currently a 5-minute delay on results using this API." (reference). A 2017 staff post (Bex_GGG) says whitelisted public sites can get undelayed data by emailing contact@grindinggear.com: https://www.pathofexile.com/forum/view-thread/2036957
- Rate limits (all GGG APIs, https://www.pathofexile.com/developer/docs): limits "are dynamic and can change at any time". Headers: `X-Rate-Limit-Policy`, `X-Rate-Limit-Rules` (e.g. ip, account, client), `X-Rate-Limit-{rule}` = hits:period:restriction seconds, `X-Rate-Limit-{rule}-State` = current state, and `Retry-After` on 429. Responses in the 4xx range count towards an invalid-request threshold; "Exceeding these limits frequently will result in your application access being revoked."
- User-Agent: "Any application that interacts with our API must set an identifiable User Agent header prefixed using the following format: `OAuth {$clientId}/{$version} (contact: {$contact}) ...`".
- Bulk/scheduled polling: the docs give no polling frequency. The stash stream is read by following `next_change_id`.
- poe.ninja: the site and /about returned 403, so I could not read any official API documentation or terms. The only document I found is an unofficial community write-up (https://github.com/ayberkgezer/poe.ninja-API-Document). It lists `https://poe.ninja/api/data/currencyoverview?league=…&type=…` and `itemoverview` endpoints for uniques, gems, base types, scarabs, fossils, maps and more, and says nothing about caching, frequency or attribution. Its terms of use are **unknown**.

Conditions, if poe.ninja is used once its terms are confirmed:
- Read poe.ninja's own terms first (from a browser) and follow them.
- Fetch at most once per hour per league and type, cache locally, and send the desk's identifying User-Agent.
- Credit poe.ninja in the UI next to prices that come from it.
- Never call pathofexile.com/trade endpoints.

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
