"""Readable names for the base-item IDs the exchange publishes.

Names come from the community RePoE export of the game's own data files. Items the
export does not know yet (typically brand-new league items) fall back to a name
derived from the ID in SQL (see db.py).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from vaalstreet.paths import items_file

SOURCES = {
    "poe1": "https://repoe-fork.github.io/base_items.min.json",
    "poe2": "https://repoe-fork.github.io/poe2/base_items.min.json",
}


def download(root: Path, realm: str) -> int:
    """Fetch the base-item export for a realm and keep id -> name/class. Returns item count."""
    request = urllib.request.Request(SOURCES[realm], headers={"User-Agent": "vaal-street/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        export = json.load(response)
    names = {
        item_id: {"name": item["name"], "class": item.get("item_class")}
        for item_id, item in export.items()
        if item.get("name")
    }
    target = items_file(root, realm)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return len(names)


def load(root: Path, realm: str) -> dict[str, dict]:
    path = items_file(root, realm)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
