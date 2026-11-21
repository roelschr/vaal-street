"""Strategy files you can hand to a friend: a strategy, its settings and how it did, as JSON.

`export` writes one local file holding the strategy spec, its parameters, the backtest
options, one summary per league it was run on, the desk's version and, for a strategy of
your own (`path/to/file.py:ClassName`), its source code. Nothing is uploaded; passing the
file on is up to you.

`read` and `save` are the other end. A strategy file can carry Python, and Python runs with
your permissions, so importing never runs or imports the code: `read` only parses the JSON
and checks it, the CLI shows the source and asks for `--yes`, and `save` writes the source
as a new file in the data directory's `strategies` folder, never over an existing file.
"""

from __future__ import annotations

import json
import re
from importlib import metadata
from pathlib import Path

FORMAT = "vaalstreet-strategy"
FORMAT_VERSION = 1


def code_version() -> str:
    try:
        return metadata.version("vaalstreet")
    except metadata.PackageNotFoundError:  # a source checkout that was never installed
        project = Path(__file__).resolve().parent.parent / "pyproject.toml"
        found = re.search(r'^version = "([^"]+)"', project.read_text(), re.M) if project.is_file() else None
        return found.group(1) if found else "unknown"


def export(path: Path, spec: str, params: dict, options: dict, summaries: list[dict]) -> dict:
    """Write a strategy file and return what was written."""
    from vaalstreet.backtest import STRATEGIES

    doc: dict = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "code_version": code_version(),
        "strategy": spec,
        "params": params,
        "options": options,
        "results": summaries,
    }
    if spec not in STRATEGIES:
        file, _, cls = spec.partition(":")
        source = Path(file)
        if not source.is_file():
            raise ValueError(f"Cannot export {spec!r}: {file} is not a file.")
        doc["class"] = cls or "Strategy"
        doc["file"] = source.name
        doc["strategy"] = f"{source.name}:{doc['class']}"  # not the folder: it would name your home directory
        doc["source"] = source.read_text(encoding="utf-8")
    Path(path).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return doc


def read(path: Path) -> dict:
    """Parse and check a strategy file without running anything in it."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"Cannot read {path}: {err}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError(f"{path} is not a Vaal Street strategy file.")
    if not isinstance(doc.get("params", {}), dict):
        raise ValueError(f"{path}: params must be a JSON object.")
    if "source" in doc:
        if not isinstance(doc["source"], str) or not str(doc.get("class", "")).isidentifier():
            raise ValueError(f"{path}: the source or the class name is not valid.")
    else:
        from vaalstreet.backtest import STRATEGIES

        if doc.get("strategy") not in STRATEGIES:
            raise ValueError(f"{path} names a strategy that is neither built in nor included as code.")
    return doc


def file_name(doc: dict) -> str:
    """A safe file name for the source: the sender's name stripped to letters, digits and '_'."""
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", Path(str(doc.get("file") or "strategy")).stem).strip("_") or "strategy"
    return f"{stem}.py"


def save(doc: dict, folder: Path) -> Path:
    """Write the source into `folder` under a new name; never overwrites a file."""
    if "source" not in doc:
        raise ValueError("This strategy is built in; there is no code to save.")
    folder.mkdir(parents=True, exist_ok=True)
    name = file_name(doc)
    target = folder / name
    n = 2
    while target.exists():
        target = folder / f"{Path(name).stem}_{n}.py"
        n += 1
    if target.resolve().parent != folder.resolve():
        raise ValueError("Refusing to write outside the strategies folder.")
    target.write_text(doc["source"], encoding="utf-8")
    return target


def run_command(doc: dict, path: Path | None = None) -> str:
    """The command that backtests the strategy with the settings it was shared with."""
    spec = f"{path}:{doc['class']}" if path is not None else doc["strategy"]
    parts = ["python -m vaalstreet backtest", _quote(spec)]
    if doc.get("params"):
        parts += ["--params", _quote(json.dumps(doc["params"]))]
    return " ".join(parts)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
