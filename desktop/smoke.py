"""Check a built `vaal-desk` program before it is packed into the app: it must start, serve the page,
start the screen check, and stop when its input closes.

    python desktop/smoke.py <path to vaal-desk> [--screen]

It runs the program on the test suite's synthetic archive in a temporary folder, with the background
download off, so it needs no network and touches no real data. `--screen` also requires the screen
check to come up, which proves its libraries and their model files are inside the build.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "tests")]


def main() -> None:
    exe, want_screen = sys.argv[1], "--screen" in sys.argv[2:]
    import conftest

    with tempfile.TemporaryDirectory() as tmp:
        conftest.build_archive(Path(tmp), conftest.all_hours())
        env = {**os.environ, "VAAL_DATA_DIR": tmp, "NO_PROXY": "127.0.0.1,localhost"}
        args = [exe, "app", "--port", "0", "--no-sync", *([] if want_screen else ["--no-screen"])]
        child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=env, text=True, encoding="utf-8", errors="replace")
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            for line in child.stdout:
                print("  |", line.rstrip(), flush=True)
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()

        def wait_for(text: str, seconds: float, bad: str | None = None) -> str:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if bad and bad in line:
                    raise SystemExit(f"FAILED: the program said: {line.strip()}")
                if text in line:
                    return line
            raise SystemExit(f"FAILED: never saw {text!r}")

        try:
            url = wait_for("VAAL-DESK-READY", 120).split()[1]
            page = urllib.request.urlopen(url + "/", timeout=30).read().decode()
            assert "<title>Vaal Street" in page, "the page is not in the build"
            names = urllib.request.urlopen(url + "/api/names", timeout=60).read().decode()
            assert "Test Orb" in names, "the desk does not answer from the archive"
            if want_screen:
                wait_for("Press Ctrl+C here to stop.", 180, bad="screen check is off")
            child.stdin.close()
            code = child.wait(timeout=30)
            assert code == 0, f"exit code {code} after its input closed"
        finally:
            if child.poll() is None:
                child.kill()
    print("OK: it starts, serves the page, " + ("starts the screen check, " if want_screen else "") + "and stops.")


if __name__ == "__main__":
    main()
