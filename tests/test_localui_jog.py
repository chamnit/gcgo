"""The local UI's wheel: one $J= per detent on GRBL, a jog-mode session with a
lead limit on caPy, and machine commands deferred until jog mode has ended."""

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
    port.out.clear()
    return u


def status(port, state, x):
    port.feed("<%s|MPos:%.3f,0.000,0.000|FS:0,0>\r\n" % (state, x))


def capy(ui, port):
    port.feed("[VER:0.1:caPy]\r\nok\r\n")
    ui.tick()
    assert ui._capy
    port.out.clear()


def sent(port):
    return [l for l in port.lines() if l != "?"]


def test_grbl_keeps_one_jog_line_per_detent(ui, port):
    ui.inp.q += ["cw", "ccw"]
    ui.tick()
    assert sent(port) == ["$J=G91 G21 X1 F800", "$J=G91 G21 X-1 F800"]


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
