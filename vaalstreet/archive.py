"""Mirror GGG's public Currency Exchange history to disk.

One gzipped file per published hour, bytes exactly as served:
    <data>/raw/<realm>/<YYYY-MM>/<hour-start-unix>.json.gz

The endpoint serves any completed hour by its start timestamp, so the archive is
a set of files rather than a cursor: every run works out which hours in the
requested window are missing and fetches those, newest first. That makes the desk
usable after a minute (recent hours land first) and lets a deep backfill be
stopped and resumed at will.

Endpoint reference: https://www.pathofexile.com/developer/docs/reference
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable

from vaalstreet.paths import markets_dir, raw_dir

HOUR = 3600
DEFAULT_BASE_URL = "https://web.poecdn.com/api/currency-exchange"
REALM_PATH = {"poe1": "", "poe2": "/poe2"}
# First hour each realm ever published (the exchange launched with Settlers / PoE2 EA).
FIRST_HOUR = {"poe1": 1722024000, "poe2": 1733511600}
# An hour is published shortly after it ends; asking earlier returns a 404.
PUBLISH_LAG = 300
MAX_ATTEMPTS = 6


class Unpublished(Exception):
    """The hour exists on the calendar but the exchange has not published it."""


USER_AGENT = "vaal-street/2.0 (+https://github.com/roelschr/vaal-street)"


def user_agent() -> str:
    """The identification every outbound request carries: the app, its version and where to reach
    its authors. `POE_USER_AGENT` overrides it. Exchange digests, the item export, icons and
    poe.ninja all use this one string."""
    return os.environ.get("POE_USER_AGENT", USER_AGENT)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def retry_after(header: str | None) -> float | None:
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


def hour_path(root: Path, realm: str, hour: int) -> Path:
    month = datetime.fromtimestamp(hour, timezone.utc).strftime("%Y-%m")
    return raw_dir(root) / realm / month / f"{hour}.json.gz"


def latest_published(now: float | None = None) -> int:
    """Start of the newest hour that should already be published."""
    now = time.time() if now is None else now
    return int((now - PUBLISH_LAG) // HOUR) * HOUR - HOUR


def have(root: Path, realm: str) -> set[int]:
    """Hour starts already on disk for a realm: as raw digests, or already indexed into Parquet
    (an archive restored from a snapshot has the Parquet without the raw files)."""
    hours: set[int] = set()
    realm_dir = raw_dir(root) / realm
    if realm_dir.is_dir():
        hours.update(int(p.name.split(".")[0]) for p in realm_dir.glob("*/*.json.gz"))
    for sidecar in (markets_dir(root) / f"realm={realm}").glob("month=*/hours.json"):
        hours.update(json.loads(sidecar.read_text()))
    return hours


def missing(root: Path, realm: str, start: int, end: int) -> list[int]:
    """Hours in [start, end] not on disk yet, newest first."""
    start = max(start - start % HOUR, FIRST_HOUR[realm])
    end = min(end - end % HOUR, latest_published())
    on_disk = have(root, realm)
    return [h for h in range(end, start - 1, -HOUR) if h not in on_disk]


def fetch_hour(base_url: str, realm: str, hour: int) -> bytes:
    """Return the raw digest for one hour. Backs off on 429 and 5xx."""
    url = f"{base_url}{REALM_PATH[realm]}/{hour}"
    request = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"})
    for attempt in range(MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            if "markets" not in json.loads(raw):
                raise ValueError("digest has no markets")
            return raw
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise Unpublished(hour) from err
            # Any other 4xx except 429 will not fix itself; stop instead of hammering the API.
            if 400 <= err.code < 500 and err.code != 429:
                raise RuntimeError(f"HTTP {err.code} for {url}") from err
            wait = retry_after(err.headers.get("Retry-After")) or min(900, 15 * 2**attempt)
            reason = f"HTTP {err.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, OSError) as err:
            wait = min(300, 5 * 2**attempt)
            reason = str(err)
        if attempt == MAX_ATTEMPTS:
            raise RuntimeError(f"giving up on {url}: {reason}")
        time.sleep(wait)
    raise AssertionError("unreachable")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def archive_hours(
    root: Path,
    realm: str,
    hours: Iterable[int],
    *,
    base_url: str = DEFAULT_BASE_URL,
    delay: float = 0.5,
    workers: int = 2,
    log: Callable[[str], None] | None = print,
    stop: threading.Event | None = None,
) -> int:
    """Download the given hours (skipping any that turn out unpublished). Returns hours saved."""
    hours = list(hours)
    saved = 0
    lock = threading.Lock()

    def one(hour: int) -> None:
        nonlocal saved
        if stop is not None and stop.is_set():
            return
        try:
            raw = fetch_hour(base_url, realm, hour)
        except Unpublished:
            return
        path = hour_path(root, realm, hour)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, gzip.compress(raw, 6))
        with lock:
            saved += 1
            if log and saved % 100 == 0:
                log(f"[{realm}] {saved}/{len(hours)} hours saved, at {_iso(hour)}")
        time.sleep(delay)

    if workers <= 1:
        for hour in hours:
            one(hour)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, hours))
    return saved


def archive_realm(
    root: Path,
    realm: str,
    *,
    start: int | None = None,
    end: int | None = None,
    base_url: str = DEFAULT_BASE_URL,
    delay: float = 0.5,
    workers: int = 2,
    log: Callable[[str], None] | None = print,
    stop: threading.Event | None = None,
) -> int:
    """Download every missing hour in [start, end] (default: all history), newest first."""
    todo = missing(root, realm, start or FIRST_HOUR[realm], end if end is not None else latest_published())
    if log:
        span = f"{_iso(todo[-1])} .. {_iso(todo[0])}" if todo else "nothing missing"
        log(f"[{realm}] {len(todo)} hours to fetch ({span})")
    saved = archive_hours(root, realm, todo, base_url=base_url, delay=delay, workers=workers, log=log, stop=stop)
    if log:
        log(f"[{realm}] done ({saved} new hours)")
    return saved
