"""Check the packed app before it is released: started as a player starts it, it must bring the desk
up on its port, and closing it must stop the desk.

    python desktop/smoke_app.py <path to the packed app's program>

It runs on the test suite's synthetic archive in a temporary folder, with the background download
off and a temporary settings folder, so it needs no network and touches no real data.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "tests")]
URL = "http://127.0.0.1:3917/api/names"  # the first port desktop/main.js tries


def answers() -> bool:
    try:
        return b"Test Orb" in urllib.request.urlopen(URL, timeout=5).read()
    except (urllib.error.URLError, OSError):
        return False


def wait(want: bool, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if answers() == want:
            return True
        time.sleep(1)
    return False


def main() -> None:
    import conftest

    assert not answers(), "something already answers on the app's port"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data, user = Path(tmp) / "data", Path(tmp) / "user"
        conftest.build_archive(data, conftest.all_hours())
        env = {**os.environ, "VAAL_DATA_DIR": str(data), "VAAL_NO_SYNC": "1", "NO_PROXY": "127.0.0.1,localhost"}
        child = subprocess.Popen([sys.argv[1], f"--user-data-dir={user}"], env=env)
        try:
            assert wait(True, 120), "the app did not bring the desk up"
            # Ask the window to close, as a click on its X does; the app must then stop the desk itself.
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(child.pid)], check=False)
            else:
                child.send_signal(signal.SIGTERM)
            assert wait(False, 30), "the desk kept running after the app closed"
            child.wait(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/IM", "vaal-desk.exe"], check=False, capture_output=True)
    print("OK: the app brings the desk up, and closing it stops the desk.")


if __name__ == "__main__":
    main()
