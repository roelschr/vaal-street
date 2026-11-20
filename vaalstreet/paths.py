"""Where things live on disk. Everything hangs off one data directory."""

from __future__ import annotations

import os
from pathlib import Path

REALMS = ("poe1", "poe2")


def data_dir() -> Path:
    override = os.environ.get("VAAL_DATA_DIR")
    return Path(override) if override else Path(__file__).resolve().parent.parent / "data"


def raw_dir(root: Path) -> Path:
    return root / "raw"


def markets_dir(root: Path) -> Path:
    return root / "parquet" / "markets"


def items_file(root: Path, realm: str) -> Path:
    return root / "items" / f"{realm}.json"
