"""The local UI's wheel: a jog session with a lead limit (caPy's jog mode, or
$J= lines on GRBL), and machine commands deferred until jog mode has ended."""

import pytest

from gcgo.core.config import StatusConfig
from gcgo.frontends import localui as L


class Disp:
    width, height = 128, 64

    def fill(self, c): pass
    def rect(self, *a): pass
    def text(self, *a): pass
    def show(self): pass


class Inp:
    def __init__(self):
        self.q = []

    def poll(self):
        q, self.q = self.q, []
        return q


@pytest.fixture
def ui(streamer, port, clock, monkeypatch):
    monkeypatch.setattr(L, "now_ms", clock)
    streamer.connect = lambda: ""
    u = L.LocalUI(streamer, StatusConfig(), ".", Disp(), Inp())
    u.begin()
    port.feed("ok\r\n" * len(streamer._owed))   # answer the setup lines
    streamer.service()
    port.out.clear()
    return u


def status(port, state, x):
    port.feed("<%s|MPos:%.3f,0.000,0.000|FS:0,0>\r\n" % (state, x))


def capy(ui, port):
    """Nothing to do: the session finds caPy when its $J is answered ok."""
    port.out.clear()


def grbl(ui, port):
    """The first detent's $J is answered error:3: GRBL."""
    ui.inp.q += ["cw"]
    ui.tick()
    assert sent(port) == ["$J"]
    port.feed("error:3\r\n")
    ui.tick()
    assert ui.jog.grbl


def sent(port):
    return [l for l in port.lines() if l != "?"]


def test_grbl_detents_become_short_jogs(ui, port):
    grbl(ui, port)
    assert sent(port)[1:] == ["$J=G91 G21 X1 F800"]   # the first detent
    port.feed("ok\r\n")
    ui.inp.q += ["cw", "cw"]
    ui.tick()                                         # read the detents
    ui.tick()                                         # send them
    assert sent(port)[-1] == "$J=G91 G21 X2 F800"     # detents add up


def test_grbl_detents_past_the_lead_are_dropped(ui, port):
    status(port, "Idle", 0.0)
    ui.tick()
    grbl(ui, port)
    lead = L.DIAL_LEAD_MM + 800 / 60.0 * L.DIAL_LAG_S
    ui.inp.q += ["cw"] * 20
    ui.tick()
    assert ui._dial_target[0] == pytest.approx(int(lead))


def test_grbl_zero_after_the_wheel(ui, port, clock):
    grbl(ui, port)
    port.feed("ok\r\n")
    ui.inp.q += ["x_hold"]
    ui.tick()
    assert port.out[-1] == b"\x85"                    # brake what is queued
    status(port, "Idle", 1.0)
    ui.tick()
    assert sent(port)[-1] == "G10 L20 P0 X0"


def test_capy_detents_become_steps(ui, port):
    capy(ui, port)
    ui.inp.q += ["cw", "cw"]
    ui.tick()
    assert sent(port) == ["$J"]
    port.feed("ok\r\n")
    ui.tick()
    assert sent(port)[1:] == ["$settings.jog", "S X2 F800"]   # the detents, summed


def test_detents_past_the_lead_are_dropped(ui, port):
    capy(ui, port)
    status(port, "Idle", 0.0)
    ui.tick()
    lead = L.DIAL_LEAD_MM + 800 / 60.0 * L.DIAL_LAG_S  # 1 mm detents
    ui.inp.q += ["cw"] * 20
    ui.tick()
    assert ui._dial_target[0] == pytest.approx(int(lead))
    status(port, "Jog", 3.0)                          # the axis catches up a bit
    ui.tick()
    ui.inp.q += ["cw"] * 20
    ui.tick()
    assert ui._dial_target[0] == pytest.approx(3 + int(lead))


def test_zero_waits_for_jog_mode_to_end(ui, port, clock):
    capy(ui, port)
    ui.inp.q += ["cw"]
    ui.tick()
    port.feed("ok\r\n")
    ui.tick()
    ui.inp.q += ["x_hold"]
    ui.tick()
    assert port.out[-1] == b"\x85"
    assert "G10 L20 P0 X0" not in sent(port)
    status(port, "Jog", 1.0)                          # still braking
    ui.tick()
    assert "G10 L20 P0 X0" not in sent(port)
    status(port, "Idle", 1.0)
    ui.tick()
    assert sent(port)[-1] == "G10 L20 P0 X0"


def test_jog_mode_ends_once_on_target_and_idle(ui, port, clock):
    capy(ui, port)
    status(port, "Idle", 0.0)
    ui.tick()
    ui.inp.q += ["cw"]
    ui.tick()
    port.feed("ok\r\n")
    ui.tick()
    clock.advance(L.JOG_IDLE_MS + 1)
    ui.tick()
    assert ui.jog.active                              # not there yet
    status(port, "Jog", 1.0)
    ui.tick()
    assert not ui.jog.active and port.out[-1] == b"\x85"
