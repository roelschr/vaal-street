"""The desk and the screen check in one process (`desktop.py`), as the Windows app runs them."""

from __future__ import annotations

import builtins
import os
import threading
import time

import pytest

from tests.conftest import call
from vaalstreet import desktop, screen, server


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def test_start_serves_on_a_free_port_and_stop_frees_it(archive_root):
    desk = server.start(0, archive_root, sync=False)
    try:
        assert desk.port and desk.url == f"http://127.0.0.1:{desk.port}"
        assert call(f"{desk.url}/api/status")[0] == 200
    finally:
        desk.stop()
    again = server.start(desk.port, archive_root, sync=False)  # the port is free again
    again.stop()
    with pytest.raises(Exception):
        call(f"{desk.url}/api/status")


def test_a_taken_port_is_explained(archive_root):
    desk = server.start(0, archive_root, sync=False)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            server.start(desk.port, archive_root, sync=False)
    finally:
        desk.stop()


def test_one_process_runs_both_and_stops_when_its_input_closes(archive_root, monkeypatch):
    seen, lines = {}, []

    def fake_companion(url, root, gate, options, say):
        seen.update(url=url, root=root, options=options, status=call(f"{url}/api/names")[0])
        gate.stopped.wait(30)
        seen["stopped"] = True

    monkeypatch.setattr(desktop, "companion", fake_companion)
    read_end, write_end = os.pipe()
    with os.fdopen(read_end) as stdin:
        worker = threading.Thread(target=desktop.run, args=(0, archive_root), daemon=True,
                                  kwargs={"sync": False, "options": {"stacks": True}, "stdin": stdin, "say": lines.append})
        worker.start()
        deadline = time.monotonic() + 20
        while "status" not in seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worker.is_alive()  # an open input keeps it running
        os.close(write_end)  # the app shell went away
        worker.join(20)
    assert not worker.is_alive()
    # The first line is the one the app shell waits for, and the companion got the same address.
    assert lines[0] == f"{desktop.READY} {seen['url']}" and seen["url"].startswith("http://127.0.0.1:")
    assert seen["status"] == 200 and seen["root"] == archive_root and seen["options"] == {"stacks": True}
    deadline = time.monotonic() + 5
    while "stopped" not in seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen.get("stopped")
    with pytest.raises(Exception):  # the desk is gone too
        call(f"{seen['url']}/api/status")


def test_missing_libraries_turn_the_screen_check_off_and_leave_the_desk(server, tmp_path, monkeypatch):
    real_import = builtins.__import__

    def no_libraries(name, *args, **kwargs):
        if name.split(".")[0] in ("mss", "pynput", "rapidocr_onnxruntime"):
            raise ImportError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_libraries)
    said = []
    desktop.companion(server, tmp_path, screen.PressGate(), {}, said.append)
    assert len(said) == 1 and said[0].startswith("The screen check is off.") and "vaalstreet[screen]" in said[0]
    assert call(f"{server}/api/status")[0] == 200


def test_the_companion_waits_for_market_data_and_never_starts_without_it(tmp_path, monkeypatch):
    # A desk over an empty folder, as on the app's first start before the first download ends.
    desk = server.start(0, tmp_path, sync=False)
    started, said = [], []
    monkeypatch.setattr(screen, "run", lambda *a, **k: started.append(a))
    gate = screen.PressGate()
    worker = threading.Thread(target=desktop.companion, args=(desk.url, tmp_path, gate, {}, said.append, 0.05))
    try:
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive() and not started
        assert len(said) == 1 and "once the desk has market data" in said[0]  # said once, not every look
        gate.stop()
        worker.join(5)
        assert not worker.is_alive() and not started
    finally:
        gate.stop()
        desk.stop()


def test_run_passes_the_gate_through_to_the_companion(server, tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(screen, "run", lambda root, url, **kw: got.update(root=root, url=url, **kw))
    gate = screen.PressGate()
    desktop.companion(server, tmp_path, gate, {"stash": True, "stacks": True}, lambda _: None)
    assert got == {"root": tmp_path, "url": server, "gate": gate, "stash": True, "stacks": True}
