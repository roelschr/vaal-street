"""What the screen check says of itself, shown on the desk's page.

In the Windows app the companion has no console. A press whose reading takes seconds, a press that
fails, and a screen check that turned itself off all looked the same on the page: nothing. So the
companion tells the desk when it waits, listens, reads, or is off and why, and the desk keeps that
in memory only, next to the readings. None of it carries anything read from the screen.
"""

from __future__ import annotations

import builtins
import threading
import time

import pytest

from conftest import call, exchange
from vaalstreet import desktop, screen
from vaalstreet import server as server_module

GOOD = exchange("Test Orb", "Chaos Orb", "1:2")


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def state_of(url):
    return call(f"{url}/api/readings")[1]["screen"]


def wait_for(test, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.02)
    return False


def test_the_desk_keeps_the_state_it_is_told_and_nothing_else(server):
    assert state_of(server) is None  # no companion has talked to this desk
    assert call(f"{server}/api/screen_state", {"state": "dancing"})[0] == 400
    keys = {"hotkey": "<ctrl>+<alt>+<f9>", "record_hotkey": "<ctrl>+<alt>+<f10>"}
    assert call(f"{server}/api/screen_state", {"state": "listening", **keys, "lines": ["secret"], "message": "x" * 900})[0] == 200
    state = state_of(server)
    assert state["state"] == "listening" and state["hotkey"] == "<ctrl>+<alt>+<f9>" and not state["quiet"]
    assert len(state["message"]) == 300 and "lines" not in state  # clipped, and only the known fields
    # The short answer for "nothing new" carries the state too: it changes without a new reading.
    assert call(f"{server}/api/screen_state", {"state": "reading", **keys})[0] == 200
    short = call(f"{server}/api/readings?since=0")[1]
    assert short["readings"] is None and short["screen"]["state"] == "reading"


def test_a_companion_that_stopped_talking_is_called_quiet(archive_root, tmp_path, monkeypatch):
    app = server_module.App(archive_root, sync=False)
    try:
        now = [1000.0]
        monkeypatch.setattr(server_module.time, "time", lambda: now[0])
        app.api_screen_state({"state": "listening"})
        now[0] += 30
        app.api_screen_state({"state": "listening"})  # said again: still the same state, since the first time
        assert app.screen_state()["t"] == 1000
        assert app.screen_state()["seen"] == 1030 and not app.screen_state()["quiet"]
        now[0] += server_module.SCREEN_QUIET_SECONDS + 1
        assert app.screen_state()["quiet"]
        app.api_screen_state({"state": "off", "message": "It was stopped."})
        now[0] += 10_000
        assert not app.screen_state()["quiet"]  # off is said once and stays true
    finally:
        app._stop.set()


class TellingDesk:
    def __init__(self):
        self.readings, self.states = [], []

    def names(self):
        return "New", ["Test Orb", "Chaos Orb", "Divine Orb"]

    def reading(self, report):
        self.readings.append(report)

    def state(self, state, message=None, **keys):
        self.states.append((state, message))


def test_a_press_that_cannot_read_the_screen_says_so_on_the_page():
    desk = TellingDesk()

    def no_screen():
        raise OSError("the screen is locked")

    with pytest.raises(OSError):
        screen.handle_press(no_screen, lambda image: GOOD, desk, lambda s: None)
    (report,) = desk.readings
    assert report["reading"] is None and report["lines"] == []
    assert report["error"] == "The screen could not be read (OSError: the screen is locked)."


def test_a_desk_that_takes_no_state_changes_nothing():
    class OldDesk:
        def state(self, state, message=None, **keys):
            raise RuntimeError("Not found")

    assert screen.tell_state(OldDesk(), "listening") is False
    assert screen.tell_state(object(), "listening") is False


class FakeKeys:
    """Stands in for pynput's keyboard module: keeps the keys it was given, hears nothing by itself."""

    def __init__(self):
        self.listener = None

    def GlobalHotKeys(self, hotkeys):  # noqa: N802, pynput's name
        self.listener = FakeListener(hotkeys)
        return self.listener


class FakeListener:
    def __init__(self, hotkeys):
        self.hotkeys, self.alive = hotkeys, False

    def start(self):
        self.alive = True

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive


def run_companion(server, tmp_path, monkeypatch, capture):
    keys = FakeKeys()
    monkeypatch.setattr(screen, "load_libraries", lambda: (None, keys, None))
    monkeypatch.setattr(screen, "make_recognise", lambda ocr: lambda image: GOOD)
    monkeypatch.setattr(screen, "make_capture", lambda mss, area: capture)
    monkeypatch.setattr(screen, "STATE_SECONDS", 0.05)
    gate = screen.PressGate()
    worker = threading.Thread(target=screen.run, args=(tmp_path, server), kwargs={"gate": gate}, daemon=True)
    worker.start()
    assert wait_for(lambda: (state_of(server) or {}).get("state") == "listening")
    return keys, gate, worker


def test_the_companion_says_it_listens_reads_and_stops(server, tmp_path, monkeypatch):
    reading, go_on, seen = threading.Event(), threading.Event(), []

    def capture():
        reading.set()
        seen.append(state_of(server)["state"])  # what the page shows while the screen is being read
        go_on.wait(10)
        return "image"

    keys, gate, worker = run_companion(server, tmp_path, monkeypatch, capture)
    try:
        state = state_of(server)
        assert (state["hotkey"], state["record_hotkey"], state["stacks"]) == (screen.DEFAULT_HOTKEY, screen.DEFAULT_RECORD_HOTKEY, False)
        keys.listener.hotkeys[screen.DEFAULT_HOTKEY]()  # one press
        assert reading.wait(10)
        go_on.set()
        assert wait_for(lambda: call(f"{server}/api/readings")[1]["readings"])
        assert seen == ["reading"]
        assert wait_for(lambda: state_of(server)["state"] == "listening")
        (row,) = call(f"{server}/api/readings")[1]["readings"]
        assert row["said"] and not row["error"]
    finally:
        go_on.set()
        gate.stop()
        worker.join(10)
    assert not worker.is_alive()
    assert (state_of(server)["state"], state_of(server)["message"]) == ("off", "It was stopped.")


def test_a_key_listener_that_ended_is_reported_as_off(server, tmp_path, monkeypatch):
    keys, gate, worker = run_companion(server, tmp_path, monkeypatch, lambda: "image")
    try:
        keys.listener.alive = False  # the listener's thread ended: no key will be heard
        assert wait_for(lambda: state_of(server)["state"] == "off")
        assert "key listener stopped" in state_of(server)["message"]
    finally:
        gate.stop()
        worker.join(10)


def test_missing_libraries_show_on_the_page_as_off(server, tmp_path, monkeypatch):
    real_import = builtins.__import__

    def no_libraries(name, *args, **kwargs):
        if name.split(".")[0] in ("mss", "pynput", "rapidocr_onnxruntime"):
            raise ImportError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_libraries)
    desktop.companion(server, tmp_path, screen.PressGate(), {}, lambda _: None)
    state = state_of(server)
    assert state["state"] == "off" and "vaalstreet[screen]" in state["message"]


def test_waiting_for_market_data_shows_on_the_page(tmp_path, monkeypatch):
    desk = server_module.start(0, tmp_path, sync=False)  # an empty folder: no market data yet
    gate = screen.PressGate()
    worker = threading.Thread(target=desktop.companion, args=(desk.url, tmp_path, gate, {}, lambda _: None, 0.05))
    try:
        worker.start()
        assert wait_for(lambda: (state_of(desk.url) or {}).get("state") == "waiting")
        assert "market data" in state_of(desk.url)["message"]
    finally:
        gate.stop()
        worker.join(5)
        desk.stop()
