"""The Windows app's Python side as a program: the whole `vaalstreet` command line, frozen by PyInstaller.

The app shell starts it as `vaal-desk app`. The other commands work too, so the installed app can
also sync, restore a snapshot or answer a question from a console.
"""

from __future__ import annotations

import sys

from vaalstreet.cli import main

if __name__ == "__main__":
    # The shell reads this program's output through a pipe. Windows would pick an old code page for
    # it and fail on the first item name or "…" it cannot write, and Python would hold lines back
    # until a buffer fills, so the shell would wait for a ready line that was already printed.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    main()
