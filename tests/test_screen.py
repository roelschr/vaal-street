"""The screen check companion, without its optional libraries, a screen or a game.

The parser cases are recognised text in the game's own layout (`conftest.exchange`; the lines the
companion really got in game are in tests/test_screen_real_layout.py), with the usual misreads added
by hand: spaces dropped between words, O for 0, l or I for 1, stray spaces. Each states its answer.
"""

from __future__ import annotations

import builtins
import threading

import pytest

from conftest import call, exchange
from vaalstreet import screen
from vaalstreet.screen import PressGate, Reading, ScreenError, parse_screen

NAMES = ["Orb of Alteration", "Orb of Augmentation", "Orb of Alchemy", "Orb of Fusing", "Exalted Orb",
         "Divine Orb", "Chromatic Orb", "Test Orb", "Mirror Shard"]

GOOD = [
    # (recognised lines, expected (item, market, side, item_units, currency_units))
    (exchange("OrbofAlteration", "DivineOrb", "64:1"), ("Orb of Alteration", "divine", "buy", 64, 1)),
    # You give Exalted for Chaos: 140 Exalted for 1 Chaos.
    (exchange("ChaosOrb", "ExaltedOrb", "1:140"), ("Exalted Orb", "chaos", "sell", 140, 1)),
    (exchange("ChaosOrb", "OrbofFusing", "10:13"), ("Orb of Fusing", "chaos", "sell", 13, 10)),
    # O read for 0 and l for 1 in the numbers, but the names keep their letters.
    (exchange("Orb of Alteration", "Divine Orb", "6O : l"), ("Orb of Alteration", "divine", "buy", 60, 1)),
    # Divine against Chaos is the Divine Orb's own Chaos market.
    (exchange("Divine Orb", "Chaos Orb", "l : 148"), ("Divine Orb", "chaos", "buy", 1, 148)),
    # A zero read in a name, an I read for 1 in the ratio.
    (exchange("Chromatic 0rb", "Chaos Orb", "I0 : 1"), ("Chromatic Orb", "chaos", "buy", 10, 1)),
    # Two letters lost, but still far closer to Augmentation (0.94) than to Alteration (0.80).
    (exchange("Orb ofAu.entation", "DivineOrb", "50:1"), ("Orb of Augmentation", "divine", "buy", 50, 1)),
    # The two labels out of order: the wanted item is still the first name under them.
    (["Market Ratio", "IHave", "1:3", "IWant", "MirrorShard", "1:3", "DivineOrb", "PLACE ORDER"],
     ("Mirror Shard", "divine", "buy", 1, 3)),
]


@pytest.mark.parametrize("lines, expected", GOOD)
def test_parser_reads_realistic_recognition(lines, expected):
    r = parse_screen(lines, NAMES)
    assert (r.item, r.market, r.side, r.item_units, r.currency_units) == expected


def test_parser_takes_one_string_too():
    r = parse_screen("Market Ratio\nI Want\n64:1\nI Have\nOrb of Alteration\n64:1\nDivine Orb\n", NAMES)
    assert r.describe() == "buy 64 Orb of Alteration for 1 Divine"


BAD = [
    # (recognised lines, words the message must contain)
    (exchange("OrbofAlteration", "DivineOrb", "64 1"), "couldn't read the ratio"),
    (exchange("OrbofAlteration", "DivineOrb", "6#:1"), "couldn't read the ratio"),
    (["OrbofAlteration", "64:1", "DivineOrb"], "i want"),
    # Too damaged to tell Alteration from Augmentation: refuse, don't guess.
    (exchange("Orb of A", "DivineOrb", "64:1"), "couldn't read one of the two items"),
    (exchange("Orb of Fusing", "Exalted Orb", "1:2"), "neither side is chaos or divine"),
    (exchange("DivineOrb", "DivineOrb", "1:1"), "both sides"),
    (exchange("OrbofAlteration", "DivineOrb", "0:1"), "more than zero"),
]


@pytest.mark.parametrize("lines, words", BAD)
def test_parser_refuses_rather_than_guesses(lines, words):
    with pytest.raises(ValueError) as err:
        parse_screen(lines, NAMES)
    assert words in str(err.value).casefold()
    if isinstance(err.value, ScreenError) and "neither" not in words:
        assert "press the key again" in str(err.value) or "Market Ratio" in str(err.value) or "I Want" in str(err.value)


def test_name_matching_needs_a_clear_winner():
    assert screen.match_name("OrbofAlteratlon", NAMES) == "Orb of Alteration"
    assert screen.match_name("orb of augmentatoin", NAMES) == "Orb of Augmentation"
    assert screen.match_name("Orb", NAMES) is None
    assert screen.match_name("Glassblower's Bauble", NAMES) is None


def test_verdict_title_is_short_and_keeps_the_verdict():
    title = screen.verdict_title({"verdict": "go", "name": "Chaos Orb", "price": 140, "gap": -0.031})
    assert title == "Go: Chaos Orb at 140, 3% under the market"
    long = screen.verdict_title({"verdict": "no-go", "name": "Blessing of Uul-Netol, the Breachlord's Gift",
                                 "price": 1234.5, "gap": 0.12})
    assert len(long) <= 60 and long.startswith("No go: ") and long.endswith("at 1.23e+03, 12% over the market")


class FakeDesk:
    def __init__(self):
        self.checks, self.events = [], []

    def names(self):
        return "New", NAMES

    def check(self, reading, league):
        self.checks.append((reading, league))
        return {"verdict": "go", "name": reading.item, "price": 0.6, "gap": -0.05, "reasons": ["Cheap.", "Busy."]}

    def event(self, title, body, league):
        self.events.append((title, body, league))


LINES = exchange("OrbofAlteration", "DivineOrb", "64:1")


def test_one_press_is_one_capture_one_check_one_event():
    desk, shots, said = FakeDesk(), [], []
    result = screen.handle_press(lambda: shots.append(1) or "image", lambda image: LINES, desk, said.append)
    assert shots == [1] and result["verdict"] == "go"
    assert desk.checks == [(Reading("Orb of Alteration", "divine", "buy", 64, 1), "New")]
    (title, body, league), = desk.events
    assert title == "Go: Orb of Alteration at 0.6, 5% under the market" and league == "New"
    assert body.startswith("Read: buy 64 Orb of Alteration for 1 Divine.")
    assert any("Read: buy 64" in s for s in said)


def test_unreadable_press_posts_nothing():
    desk, said = FakeDesk(), []
    assert screen.handle_press(lambda: "image", lambda image: ["IWant", "?"], desk, said.append) is None
    assert desk.checks == [] and desk.events == [] and "press the key again" in said[0]


def test_gate_captures_only_on_presses_and_drops_presses_while_busy():
    gate, captures = PressGate(), []
    started, release = threading.Event(), threading.Event()

    def work():
        captures.append(1)
        started.set()
        release.wait(5)

    thread = threading.Thread(target=gate.serve, args=(work,), daemon=True)
    thread.start()
    # No press: nothing happens on its own, however long we wait.
    assert not started.wait(0.3) and captures == []
    assert gate.press()
    assert started.wait(5)
    # A held key or a second press while the check runs is dropped, not queued.
    assert not gate.press() and not gate.press()
    release.set()
    for _ in range(100):  # let the check finish
        if not gate._busy.locked():
            break
        threading.Event().wait(0.01)
    started.clear()
    release.clear()
    assert gate.press() and started.wait(5)
    release.set()
    gate.stop()
    thread.join(5)
    assert captures == [1, 1] and not thread.is_alive()


def test_event_endpoint_reaches_the_hideout_poll(server):
    league = call(f"{server}/api/names")[1]["league"]
    status, names = call(f"{server}/api/names")
    assert status == 200 and "Test Orb" in names["names"]
    status, event = call(f"{server}/api/event", {"title": "Go: Test Orb at 9, 3% under the market " + "x" * 80,
                                                 "body": "Read: buy 1 Test Orb for 9 Chaos.", "league": league})
    assert status == 200 and len(event["title"]) <= 60
    events = call(f"{server}/api/events?league={league}")[1]["events"]
    assert events[-1]["title"] == event["title"] and events[-1]["body"].startswith("Read: buy 1")
    assert call(f"{server}/api/event", {"body": "no title"})[0] == 400
    # Still local only: a page on another site cannot post events.
    assert call(f"{server}/api/event", {"title": "x"}, headers={"Origin": "https://evil.example"})[0] == 403


def test_desk_client_checks_through_the_real_api(server):
    client = screen.DeskClient(server)
    league, names = client.names()
    reading = parse_screen(exchange("TestOrb", "ChaosOrb", "1:9"), names)
    result = client.check(reading, league)
    assert result["verdict"] == "go" and result["name"] == "Test Orb"


def test_cli_explains_the_missing_extra(monkeypatch, capsys, tmp_path):
    from vaalstreet import cli

    real_import = builtins.__import__

    def no_libraries(name, *args, **kwargs):
        if name.split(".")[0] in ("mss", "pynput", "rapidocr_onnxruntime"):
            raise ImportError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv("VAAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(builtins, "__import__", no_libraries)
    with pytest.raises(SystemExit) as err:
        cli.main(["screen"])
    assert 'pip install "vaalstreet[screen]"' in str(err.value)


def test_settings_and_region(tmp_path):
    assert screen.parse_region("600, 200 700 500") == {"left": 600, "top": 200, "width": 700, "height": 500}
    with pytest.raises(ValueError):
        screen.parse_region("600 200 700")
    screen.save_settings(tmp_path, {"region": None, "hotkey": "<f8>"})
    assert screen.load_settings(tmp_path)["hotkey"] == "<f8>"
    assert screen.load_settings(tmp_path / "none") == {}
