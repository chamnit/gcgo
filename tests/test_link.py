"""A serial link that drops mid-session (a pulled cable, a power-cycled
controller): nothing dies on it, the job and the jog end, owed replies are
answered, and the web server reconnects and sets the controller up again."""

import os
import pty

import pytest

from conftest import FakePort
from gcgo.core.config import StatusConfig
from gcgo.core.jog import JogSession
from gcgo.core.protocol import ERROR, LINK_LOST, Streamer
from gcgo.frontends.web import server as srv
from test_jog import answer


class DropPort(FakePort):
    """A port that can go away (up=False) and come back (back=True)."""

    def __init__(self):
        super().__init__()
        self.up = True
        self.back = False

    def is_open(self):
        return self.up

    def reopen(self):
        if self.back:
            self.up = True
        return self.up


@pytest.fixture
def port():
    return DropPort()


@pytest.fixture
def job(tmp_path):
    p = tmp_path / "job.nc"
    p.write_text("G1 X1 F100\n" * 50)
    return p


def test_streamer_ends_the_job_and_the_jog(streamer, port, job):
    jog = JogSession(streamer)
    jog.begin()
    streamer.begin(str(job), status_interval=0)
    streamer.pump()
    assert streamer.state == "running"
    port.up = False
    assert streamer.pump() == ERROR
    assert not jog.active
    streamer.service()                      # idempotent, no raise


def test_owed_functions_get_link_lost(streamer, port):
    got = []
    streamer.write_line("$settings.all", got.append)
    streamer.write_line("G0 X1")            # an MDI line: nothing to call
    port.up = False
    streamer.service()
    assert got == [LINK_LOST] and streamer._owed == []


@pytest.fixture
def server(streamer, port, clock, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "now_ms", clock)
    s = srv.WebServer(streamer, StatusConfig(), str(tmp_path), ".")
    answer(port, streamer, "ok")            # its $13=0
    port.out.clear()
    return s


def drive(server):
    if server._link_ok():
        server._drive()


def msgs(server):
    return [o["line"] for o in server.pending if o["type"] == "msg"]


def test_web_job_stops_and_link_comes_back(server, streamer, port, clock, job):
    server.apply({"cmd": "run", "file": "job.nc"})
    drive(server)
    assert streamer.state == "running"
    server.pending.clear()
    port.up = False
    drive(server)
    assert any(m.startswith("serial link lost — job stopped") for m in msgs(server))
    assert server.status_obj()["state"] == srv.NO_LINK
    assert not server._job
    port.out.clear()

    server.pending.clear()
    server.apply({"cmd": "mdi", "line": "G0 X1"})
    assert msgs(server) == ["not sent: serial link down"]
    clock.advance(srv.LINK_RETRY_MS)
    drive(server)                           # still gone
    assert port.out == []

    port.back = True
    clock.advance(srv.LINK_RETRY_MS)
    drive(server)
    assert "serial link back" in msgs(server)
    assert server.status_obj()["state"] == ""        # not the old state
    assert "$13=0" not in port.lines()      # not while a GRBL may be rebooting
    clock.advance(srv.LINK_SETUP_MS)
    drive(server)
    assert "$13=0" in port.lines()
    answer(port, streamer, "ok")
    port.out.clear()
    server.apply({"cmd": "mdi", "line": "G0 X1"})
    assert port.lines() == ["G0 X1"]


def test_web_settings_read_in_flight_ends_with_an_error(server, streamer, port):
    server.apply({"cmd": "ctl_settings"})
    port.up = False
    drive(server)
    end = [o for o in server.pending if o["type"] == "ctl_settings"]
    assert end and end[0]["error"] == LINK_LOST
    assert server._ctl is None
    server.pending.clear()
    server.apply({"cmd": "ctl_set", "key": "jog.cover", "value": "20"})
    assert [o["reply"] for o in server.pending if o["type"] == "ctl_set"] == [LINK_LOST]


def test_web_jog_is_dropped(server, streamer, port):
    server.apply({"cmd": "jog_vel", "x": 1, "y": 0, "z": 0, "f": 600})
    assert server.jog.active
    port.up = False
    drive(server)
    assert not server.jog.active
    server.apply({"cmd": "jog_vel", "x": 1, "y": 0, "z": 0, "f": 600})
    assert not server.jog.active and port.lines() == ["$J"]


def test_driver_survives_an_exception(server, monkeypatch):
    import asyncio

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(server, "_drive", boom)
    server.clients = []

    async def run():
        t = asyncio.ensure_future(server.driver())
        await asyncio.sleep(0.05)
        assert not t.done()
        t.cancel()
    asyncio.run(run())
    assert any("driver error" in m for m in msgs(server))


def test_pyserial_transport_on_a_pty_that_goes_away(tmp_path):
    pytest.importorskip("serial")
    from gcgo.desktop.transport import PySerialTransport
    link = str(tmp_path / "tty")
    m, sl = pty.openpty()
    os.symlink(os.ttyname(sl), link)
    t = PySerialTransport(link, timeout=0.05)
    os.close(sl)
    os.write(m, b"ok\n")
    assert t.read(64) == b"ok\n"
    os.close(m)                              # the far end is gone
    t.write(b"?")
    t.read(64)
    t.any()
    assert not t.is_open()
    assert t.read(64) == b"" and t.any() == 0 and t.readinto(bytearray(8)) == 0
    os.remove(link)
    assert not t.reopen()                    # not back yet
    m, sl = pty.openpty()
    os.symlink(os.ttyname(sl), link)
    assert t.reopen() and t.is_open()
    t.write(b"$I\n")
    assert os.read(m, 16) == b"$I\n"
    t.close()
    os.close(sl)
    os.close(m)
