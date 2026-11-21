"""Where things live on disk. Everything hangs off one data directory."""

from __future__ import annotations

import os
from pathlib import Path

REALMS = ("poe1", "poe2")


def data_dir() -> Path:
    """`VAAL_DATA_DIR` if set; `data/` in a source checkout; otherwise `~/.vaalstreet`."""
    override = os.environ.get("VAAL_DATA_DIR")
    if override:
        return Path(override)
    checkout = Path(__file__).resolve().parent.parent
    return checkout / "data" if (checkout / "pyproject.toml").exists() else Path.home() / ".vaalstreet"


def raw_dir(root: Path) -> Path:
    return root / "raw"


def markets_dir(root: Path) -> Path:
    return root / "parquet" / "markets"


def prices_dir(root: Path) -> Path:
    return root / "parquet" / "prices"


def items_file(root: Path, realm: str) -> Path:
    return root / "items" / f"{realm}.json"


def icons_dir(root: Path) -> Path:
    return root / "icons"


def ledger_file(root: Path) -> Path:
    return root / "desk.sqlite"


def strategies_dir(root: Path) -> Path:
    """Strategy code you imported with `vaalstreet strategy import`."""
    return root / "strategies"
