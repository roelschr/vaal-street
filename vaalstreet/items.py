"""Readable names and metadata for the base-item IDs the exchange publishes.

Metadata comes from the community RePoE export of the game's own data files:
name, a desk category, stack size, icon art path, and (for divination cards and
shards) what a full set turns into. Items the export does not know yet -
typically brand-new league items - fall back to a name derived from the ID.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from functools import lru_cache
from pathlib import Path

from vaalstreet.paths import items_file

SOURCES = {
    "poe1": "https://repoe-fork.github.io/base_items.min.json",
    "poe2": "https://repoe-fork.github.io/poe2/base_items.min.json",
}
ICON_URL = "https://web.poecdn.com/image/{art}.png"

_TAG_CATEGORIES = [
    ("essence", "Essences"),
    ("mushrune", "Oils"),
    ("catalyst", "Catalysts"),
    ("affliction_orb", "Delirium"),
    ("affliction_splinter", "Delirium"),
    ("currency_shard", "Shards"),
    ("legion_splinter", "Splinters"),
    ("thread", "Threads"),
    ("heist_coin", "Currency"),
]
_NAME_CATEGORIES = [
    (re.compile(r"\bTattoo\b"), "Tattoos"),
    (re.compile(r"^Runegraft\b"), "Runegrafts"),
    (re.compile(r"^Omen of\b"), "Omens"),
    (re.compile(r"^Allflame Ember\b"), "Embers"),
    (re.compile(r"\bScarab\b"), "Scarabs"),
    (re.compile(r"\bFossil$|\bResonator$"), "Delve"),
    (re.compile(r"Lifeforce$"), "Lifeforce"),
    (re.compile(r"\bSplinter$"), "Splinters"),
    (re.compile(r"\bShard$"), "Shards"),
    (re.compile(r"^(Lesser|Greater|Grand|Exceptional) Eldritch|^Eldritch "), "Eldritch"),
    (re.compile(r"Delirium Orb$"), "Delirium"),
    (re.compile(r"\bIncubator$"), "Incubators"),
]
_REWARD = re.compile(r"\{(?:(\d+)x )?([^{}]+)\}")


def categorize(item_id: str, name: str, item_class: str | None, tags: list[str]) -> str:
    if item_class == "DivinationCard":
        return "Divination Cards"
    for tag_part, category in _TAG_CATEGORIES:
        if any(tag_part in tag for tag in tags):
            return category
    for pattern, category in _NAME_CATEGORIES:
        if pattern.search(name):
            return category
    if item_class in ("MapFragment", "VaultKey", "Breachstone", "MiscMapItem") or "/MapFragments/" in item_id:
        return "Fragments"
    return "Currency"


def fallback_name(item_id: str) -> str:
    tail = re.sub(r"^(Currency|DivinationCard)", "", item_id.rsplit("/", 1)[-1])
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tail) or item_id


def _card_reward(description: str) -> tuple[int, str] | None:
    """`<currencyitem>{10x Divine Orb}` -> (10, "Divine Orb"). Only plain single-item rewards."""
    found = _REWARD.findall(description or "")
    if len(found) != 1:
        return None
    count, name = found[0]
    return int(count or 1), name.strip()


def build(export: dict) -> dict[str, dict]:
    """Reduce the RePoE base-item export to what the desk needs."""
    out: dict[str, dict] = {}
    for item_id, item in export.items():
        name = item.get("name")
        if not name or item.get("release_state") == "unreleased":
            continue
        props = item.get("properties") or {}
        art = ((item.get("visual_identity") or {}).get("dds_file") or "").removesuffix(".dds")
        entry = {
            "name": name,
            "class": item.get("item_class"),
            "category": categorize(item_id, name, item.get("item_class"), item.get("tags") or []),
            "stack": props.get("stack_size"),
            "art": art or None,
        }
        if item.get("item_class") == "DivinationCard":
            reward = _card_reward(props.get("description", ""))
            if reward and props.get("stack_size"):
                entry["set"] = {"need": props["stack_size"], "gives": reward[0], "reward": reward[1]}
        out[item_id] = entry
    # Shards: 20 make the orb they are named after.
    by_name = {entry["name"]: item_id for item_id, entry in out.items() if "/Currency/" in item_id}
    for item_id, entry in out.items():
        if entry["category"] != "Shards" or not entry["name"].endswith(" Shard"):
            continue
        stem = entry["name"][: -len(" Shard")]
        for target in (f"{stem} Orb", f"Orb of {stem}", f"{stem} of Kalandra", f"{stem}'s Orb"):
            if target in by_name:
                entry["set"] = {"need": 20, "gives": 1, "reward": target}
                break
    return out


def download(root: Path, realm: str) -> int:
    """Fetch the base-item export for a realm and keep the reduced metadata. Returns item count."""
    request = urllib.request.Request(SOURCES[realm], headers={"User-Agent": "vaal-street/2.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        export = json.load(response)
    reduced = build(export)
    target = items_file(root, realm)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_text(json.dumps(reduced, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    load.cache_clear()
    return len(reduced)


@lru_cache(maxsize=8)
def load(root: Path, realm: str) -> dict[str, dict]:
    path = items_file(root, realm)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def describe(root: Path, realm: str, item_id: str) -> dict:
    """Metadata for one item, with fallbacks for IDs the export does not know."""
    entry = load(root, realm).get(item_id)
    if entry:
        return entry
    name = fallback_name(item_id)
    return {"name": name, "class": None, "category": categorize(item_id, name, None, []), "stack": None, "art": None}
