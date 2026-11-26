"""The desk and the screen check in one process, for the Windows app.

Run by hand, the desk (`serve`) and the screen check (`screen`) are two programs in two consoles,
often on two operating systems. The Windows app wants one thing to start and one thing to stop, so
this module runs both in a single process: the desk answers on a background thread, the companion
listens for its keys on another, and the main thread only waits for the end.

The companion still talks to the desk through the desk's own HTTP API, exactly as it does from a
second program. That is deliberate: the API is where a rehearsal swaps in the practice ledger, and
where the rules about what a reading may contain are enforced. Calling the desk's objects directly
would step around both.

The app shell that starts this process learns the desk's address from one line on standard output
(`READY`), and keeps the process's standard input open. When the input closes, the shell is gone,
and this process stops too, so no desk is left running without a window.
"""

from __future__ import annotations

import functools
import sys
import threading
from pathlib import Path
from typing import Callable

from vaalstreet import screen, server

READY = "VAAL-DESK-READY"  # followed by the desk's address; the app shell waits for this line
WAIT_SECONDS = 3.0  # between two looks at whether the desk has market data yet


def companion(url: str, root: Path, gate: screen.PressGate, options: dict,
              say: Callable[[str], None] = print, pause: float = WAIT_SECONDS) -> None:
    """Run the screen check against the desk at `url` until `gate` is stopped.

    On a first start the desk has no market data for a couple of minutes, and the companion cannot
    know any item names until it does, so this waits. A missing library or any other failure turns
    the screen check off and says why; the desk carries on without it.
    """
    client = screen.DeskClient(url)
    told = False
    while not gate.stopped.is_set():
        try:
            client.names()
            break
        except RuntimeError as err:
            if not told:
                say(f"The screen check starts once the desk has market data ({err})")
                screen.tell_state(client, "waiting", "It starts once the desk has market data.")
                told = True
            gate.stopped.wait(pause)
    else:
        return
    try:
        screen.run(root, url, gate=gate, **options)
    except SystemExit as err:  # the optional libraries are missing
        say(f"The screen check is off. {err}")
        screen.tell_state(client, "off", str(err))
    except Exception as err:
        say(f"The screen check is off: {type(err).__name__}: {err}")
        screen.tell_state(client, "off", f"{type(err).__name__}: {err}")


def _watch(stream, done: threading.Event) -> None:
    try:
        for _ in stream:
            pass
    except (OSError, ValueError):
        pass
    done.set()


def run(port: int = 3000, root: Path | None = None, *, sync: bool = True, screen_check: bool = True,
        options: dict | None = None, stdin=None,
        say: Callable[[str], None] = functools.partial(print, flush=True)) -> None:
    """Start the desk and the screen check, and stop both when standard input closes or on Ctrl+C."""
    desk = server.start(port, root, sync=sync)
    gate = screen.PressGate()
    done = threading.Event()
    try:
        say(f"{READY} {desk.url}")
        say(f"Vaal Street desk: {desk.url}   (data in {desk.app.root})")
        if screen_check:
            threading.Thread(target=companion, args=(desk.url, desk.app.root, gate, options or {}, say),
                             name="vaal-screen", daemon=True).start()
        stdin = sys.stdin if stdin is None else stdin
        if stdin is not None:  # a program built without a console has none; it then runs until it is ended
            threading.Thread(target=_watch, args=(stdin, done), name="vaal-stdin", daemon=True).start()
        # The wait wakes twice a second: on Windows a wait with no end never hears Ctrl+C.
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        gate.stop()
        desk.stop()
