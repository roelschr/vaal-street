"""A search string for the game's search box that finds the items you trade, and no others.

What is known, and what is not
------------------------------
Another tool, Exile UI, copies a string of the form `^(a|b|c)$` for its bookmarked items, where
each item is its name with spaces replaced by `.`, and a name containing " of " is shortened to
`.*` plus the part after " of ". Its documentation says the string is meant for the in-game
exchange's search box, which reads it as a regular expression. The game's search boxes are commonly
said to cut a search at 50 characters. Neither fact has been checked in game for this desk, so the
limit is a number the player can change, and the page says plainly that the string is untested.

How a string is built
---------------------
Each wanted item gets the shortest pattern that, tried with Python's `re` against every item name
the desk knows for the league (whole name, ignoring case), matches that item and no other. The
candidates are `.*<last word>`, `.*<the part after " of ">`, `<first word>.*` and the full name;
shorter wins, and on a tie the earlier one. Any character that is not a letter or a digit (a space,
an apostrophe, a hyphen, a bracket) becomes `.`, which matches it and keeps the pattern free of
characters a regular expression would read as commands. So "Orb of Alchemy" is `.*Alchemy`, but
with both Deafening Essences known "Deafening Essence of Greed" needs its full name: `.*Greed`
would also find the Shrieking one, and `Deafening.*` the Deafening Essence of Hatred.

The patterns are then packed into as few `^(a|b|c)$` strings as the length limit allows (largest
first, each into the first string with room). An item whose pattern does not fit even alone is
reported, never dropped in silence.
"""

from __future__ import annotations

import re

DEFAULT_LIMIT = 50
# "^(" and ")$" around the patterns, which are joined by "|".
WRAP = 4


def _safe(text: str) -> str:
    """Letters and digits kept, anything else one `.`, so the text is literal and needs no escaping."""
    return "".join(c if c.isalnum() else "." for c in text)


def candidates(name: str) -> list[str]:
    """The patterns to try for one name, in order of preference on a tie."""
    words = name.split()
    out = []
    if len(words) > 1:
        out.append(".*" + _safe(words[-1]))
    if " of " in name:
        out.append(".*" + _safe(name.split(" of ", 1)[1]))
    if len(words) > 1:
        out.append(_safe(words[0]) + ".*")
    out.append(_safe(name))
    return list(dict.fromkeys(out))  # the same pattern twice ("Orb of Fusing": last word = part after "of")


def matches(pattern: str, name: str) -> bool:
    return re.fullmatch(pattern, name, re.IGNORECASE) is not None


def pattern_for(name: str, known: list[str] | set[str]) -> str:
    """The shortest candidate that matches `name` and no other known name.

    The full name always matches itself. Should it match another name too (names that differ only in
    punctuation), it is still the best there is, and the caller's check reports that item.
    """
    others = {k for k in known if k.casefold() != name.casefold()}
    good = [p for p in candidates(name) if matches(p, name) and not any(matches(p, o) for o in others)]
    return min(good, key=len) if good else _safe(name)  # min keeps the first of equal lengths


def wrap(patterns: list[str]) -> str:
    return "^(" + "|".join(patterns) + ")$"


def build(wanted: list[str], known: list[str] | set[str], limit: int = DEFAULT_LIMIT) -> dict:
    """Search strings for `wanted` among `known` names, each at most `limit` characters long.

    Returns {"strings": [{"text", "length", "items"}], "too_long": [{"name", "pattern", "length"}],
    "patterns": [{"name", "pattern"}], "limit"}. Every wanted name is in exactly one string's
    `items` or in `too_long`.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < WRAP + 1:
        raise ValueError(f"The length limit must be a whole number of at least {WRAP + 1}.")
    wanted = list(dict.fromkeys(w for w in wanted if w))
    known = set(known) | set(wanted)
    chosen = [(name, pattern_for(name, known)) for name in wanted]
    too_long = [{"name": n, "pattern": p, "length": len(wrap([p]))} for n, p in chosen if len(wrap([p])) > limit]
    bins: list[list[tuple[str, str]]] = []
    for name, pattern in sorted((c for c in chosen if len(wrap([c[1]])) <= limit), key=lambda c: -len(c[1])):
        for b in bins:
            if len(wrap([p for _, p in b] + [pattern])) <= limit:
                b.append((name, pattern))
                break
        else:
            bins.append([(name, pattern)])
    order = {name: k for k, name in enumerate(wanted)}  # each string lists its items in the order asked
    strings = []
    for b in bins:
        b.sort(key=lambda c: order[c[0]])
        text = wrap([p for _, p in b])
        strings.append({"text": text, "length": len(text), "items": [n for n, _ in b]})
    strings.sort(key=lambda s: order[s["items"][0]])
    return {"strings": strings, "too_long": too_long, "patterns": [{"name": n, "pattern": p} for n, p in chosen],
            "limit": limit}
