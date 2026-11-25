"""`--why`: a Ctrl+C that is ignored because another window is in front can say so.

Without it, "nothing happens" in a first test against the game cannot be told apart from "the key
was never heard". The clipboard is not read either way.
"""

from __future__ import annotations

from types import SimpleNamespace

from vaalstreet import clipboard, screen
from vaalstreet.clipboard import handle_copy


class OtherWindow:
    """A clipboard stand-in with a browser in front. It counts reads: there must be none."""

    def __init__(self, front="Chrome_WidgetWin_1"):
        self.front, self.reads = front, 0

    def front_class(self):
        return self.front

    def game_in_front(self):
        return self.front == clipboard.GAME_CLASS

    def sequence(self):
        return 7

    def read(self):
        self.reads += 1
        return "secret"


def test_an_ignored_press_is_silent_unless_asked():
    clip, said = OtherWindow(), []
    assert handle_copy(clip, 7, desk=None, say=said.append) is None
    assert said == [] and clip.reads == 0


def test_why_names_the_window_in_front_and_reads_nothing():
    clip, said = OtherWindow(), []
    assert handle_copy(clip, 7, desk=None, say=said.append, why=True) is None
    assert clip.reads == 0
    assert said == ['\nCtrl+C heard, but ignored: the window in front is "Chrome_WidgetWin_1", not the game '
                    '("POEWindowClass"). Nothing was read.']
    clip, said = OtherWindow(front=""), []
    handle_copy(clip, 7, desk=None, say=said.append, why=True)
    assert 'the window in front is "no window"' in said[0]


def test_the_windows_reader_reports_the_class_it_compares():
    """The real reader, on stand-in Windows libraries: `game_in_front` is true exactly when the class
    name it reports is the game's."""
    def user32(name):
        def class_name(window, buffer, size):
            buffer.value = name
            return len(name)
        return SimpleNamespace(
            GetClipboardSequenceNumber=lambda: 1, GetForegroundWindow=lambda: 5, GetClassNameW=class_name,
            OpenClipboard=lambda w: 0, GetClipboardData=lambda f: None, CloseClipboard=lambda: 1)

    kernel32 = SimpleNamespace(GlobalLock=lambda h: None, GlobalUnlock=lambda h: 1)
    game = clipboard.WindowsClipboard(user32(clipboard.GAME_CLASS), kernel32)
    other = clipboard.WindowsClipboard(user32("CabinetWClass"), kernel32)
    assert game.front_class() == clipboard.GAME_CLASS and game.game_in_front()
    assert other.front_class() == "CabinetWClass" and not other.game_in_front()


def test_cli_passes_why_to_the_companion(monkeypatch, tmp_path):
    from vaalstreet import cli

    seen = []
    monkeypatch.setenv("VAAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(screen, "run", lambda *args: seen.append(args[-1]))
    cli.main(["screen", "--stacks"])
    cli.main(["screen", "--stacks", "--why"])
    assert seen == [False, True]
