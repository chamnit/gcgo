"""Lines the controller sends on its own while the terminal prompt waits are
shown as such, not as the next command's reply."""

from gcgo.core.config import StatusConfig
from gcgo.frontends import terminal as T


class Answering:
    """Wraps the fake port: each written line is answered with `reply`."""

    def __init__(self, port, reply):
        self.port, self.reply = port, reply
        w = port.write

        def write(data):
            w(data)
            if data.endswith(b"\n"):
                port.feed(self.reply)
        port.write = write


def test_streamer_unsolicited(port, streamer):
    port.feed("ALARM:1\r\n<Alarm|MPos:1.000,2.000,3.000|FS:0,0>\r\n[MSG:Reset to continue]\r\npart")
    assert streamer.unsolicited() == ["ALARM:1", "[MSG:Reset to continue]"]
    assert streamer.status.state == "Alarm" and streamer.status.mpos[0] == 1.0
    port.feed("ial\r\n")
    assert streamer.unsolicited() == ["partial"]     # a line split across reads


def test_mdi_shows_an_idle_alarm_before_the_reply(port, streamer, monkeypatch, capsys):
    Answering(port, "error:9\r\n")
    port.feed("ALARM:1\r\n")                     # a limit switch, while at the prompt
    typed = iter(["G0 X1", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    T._mdi_loop(streamer, StatusConfig())
    out = capsys.readouterr().out.splitlines()
    i = out.index("  [controller, while idle]")
    assert out[i + 1].startswith('  "ALARM:1"')
    assert out[i + 2].startswith('  "error:9"')  # the G0's own reply, after it


def test_mdi_status_is_not_an_old_alarm(port, streamer, monkeypatch, capsys):
    Answering(port, "")
    w = port.write
    port.write = lambda d: (w(d), port.feed("<Alarm|MPos:0,0,0|FS:0,0>\r\n") if d == b"?" else None)
    port.feed("ALARM:2\r\n")
    typed = iter(["?", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    T._mdi_loop(streamer, StatusConfig())
    out = capsys.readouterr().out
    assert '"<Alarm|MPos:0,0,0|FS:0,0>"' in out
    assert out.index('"ALARM:2"') < out.index('"<Alarm')
