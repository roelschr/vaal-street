"""Mirror GGG's public Currency Exchange history to disk.

One gzipped file per published hour, bytes exactly as served:
    <data>/raw/<realm>/<YYYY-MM>/<hour-start-unix>.json.gz
Progress is kept in <data>/raw/<realm>/cursor.json, so a run can be stopped and
resumed. The layout matches scripts/archive.mjs; either tool can continue the other.

Endpoint reference: https://www.pathofexile.com/developer/docs/reference
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from vaalstreet.paths import raw_dir

HOUR = 3600
DEFAULT_BASE_URL = "https://web.poecdn.com/api/currency-exchange"
REALM_PATH = {"poe1": "", "poe2": "/poe2"}
MAX_ATTEMPTS = 8


def _user_agent() -> str:
    return os.environ.get("POE_USER_AGENT", "vaal-street-archive/0.1 (personal research; local use)")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _retry_after(header: str | None) -> float | None:
    if not header:
        return None
    try:
        return max(1.0, float(header))
    except ValueError:
        pass
    try:
        return max(1.0, parsedate_to_datetime(header).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def fetch_digest(base_url: str, realm: str, hour_id: int | None) -> tuple[bytes, int]:
    """Return (raw body, next_change_id) for one hour. Backs off on 429 and 5xx."""
    url = f"{base_url}{REALM_PATH[realm]}" + ("" if hour_id is None else f"/{hour_id}")
    request = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    for attempt in range(MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
            next_id = int(json.loads(raw)["next_change_id"])
            if next_id % HOUR:
                raise ValueError(f"unexpected next_change_id {next_id}")
            return raw, next_id
        except urllib.error.HTTPError as err:
            # Any 4xx other than 429 will not fix itself; stop instead of hammering the API.
            if 400 <= err.code < 500 and err.code != 429:
                raise RuntimeError(f"HTTP {err.code} for {url}") from err
            wait = _retry_after(err.headers.get("Retry-After")) or min(1800, 30 * 2**attempt)
            reason = f"HTTP {err.code}"
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as err:
            wait = min(600, 10 * 2**attempt)
            reason = str(err)
        if attempt == MAX_ATTEMPTS:
            raise RuntimeError(f"giving up on {url}: {reason}")
        print(f"  {reason}; waiting {wait:.0f}s", flush=True)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def archive_realm(root: Path, realm: str, *, base_url: str = DEFAULT_BASE_URL, delay: float = 2.0) -> int:
    """Download every hour not yet on disk for one realm. Returns hours saved."""
    realm_dir = raw_dir(root) / realm
    realm_dir.mkdir(parents=True, exist_ok=True)
    cursor_path = realm_dir / "cursor.json"
    hour_id: int | None = json.loads(cursor_path.read_text())["next"] if cursor_path.exists() else None
    start = "the first published hour" if hour_id is None else _iso(hour_id)
    print(f"[{realm}] starting at {start}", flush=True)

    saved = 0
    while True:
        raw, next_id = fetch_digest(base_url, realm, hour_id)
        if hour_id is not None and next_id == hour_id:
            break  # caught up: this hour is not published yet
        hour = hour_id if hour_id is not None else next_id - HOUR
        month_dir = realm_dir / datetime.fromtimestamp(hour, timezone.utc).strftime("%Y-%m")
        month_dir.mkdir(exist_ok=True)
        _write_atomic(month_dir / f"{hour}.json.gz", gzip.compress(raw))
        _write_atomic(cursor_path, json.dumps({"next": next_id}).encode())
        hour_id = next_id
        saved += 1
        if saved % 50 == 0:
            print(f"[{realm}] {saved} hours saved, at {_iso(hour)}", flush=True)
        time.sleep(delay)
    print(f"[{realm}] up to date ({saved} new hours)", flush=True)
    return saved
