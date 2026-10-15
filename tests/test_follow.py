"""Follower: readings in, moving targets out (and the web follow pad)."""

import pytest

from gcgo.core.config import StatusConfig
from gcgo.core.follow import STALE_MS, Follower
from gcgo.core.jog import SOURCE_TTL_MS
from gcgo.frontends.web import server as srv
from test_jog import answer, on_session
from test_jog_grbl import grbl_session


def test_velocity_is_estimated_and_sent_with_the_target(port, streamer, clock):
    j = on_session(port, streamer, clock)
    f = Follower(j)
    f.start((10.0, 20.0, -1.0))
    f.move(0, 0, feed=1500)
    j.tick()
    assert port.lines() == ["T X10 Y20 F1500"]   # no velocity yet: a go-to
    answer(port, streamer, "ok")
    for k in range(1, 6):                        # +X at 10 mm/s, 16 ms readings
        clock.advance(16)
        f.move(0.16 * k, 0, feed=1500)
    j.tick()
    line = port.lines()[-1]
    assert line.startswith("T X10.8 VX") and line.endswith(" Y20 F1500")
    vx = float(line.split()[2][2:])
    assert 8.0 < vx <= 10.0                      # smoothed towards 10 (TAU_MS)


def test_the_input_clock_is_used_when_given(port, streamer, clock):
    j = on_session(port, streamer, clock)
    f = Follower(j, axes="X")
    f.start((0.0, 0.0, 0.0))
    f.move(0, t_ms=5000.0)
    clock.advance(200)                           # a late arrival...
    f.move(1.0, t_ms=5100.0)                     # ...but 100 ms on the input's clock
    assert f.vel[0] == pytest.approx(10.0)
    f.move(0.5, t_ms=5050.0)                     # out of order: dropped
    assert f.pos[0] == 1.0


def test_at_rest_one_go_to_then_quiet(port, streamer, clock):
    j = on_session(port, streamer, clock)
    f = Follower(j)
    f.start((0.0, 0.0, 0.0))
    f.move(2, 0)
    j.tick()
    answer(port, streamer, "ok")
    n = len(port.lines())
    for _ in range(20):                          # heartbeats at the same point
        clock.advance(50)
        f.move(2, 0)
        j.tick()
        answer(port, streamer, *["ok"] * j.inflight)
    assert len(port.lines()) <= n + 2            # the velocity decays to rest, then silence
    assert port.lines()[-1] == "T X2 Y0"


def test_end_goes_to_the_last_point(port, streamer, clock):
    j = on_session(port, streamer, clock)
    f = Follower(j)
    f.start((0.0, 0.0, 0.0))
    f.move(0, 0)
    clock.advance(16)
    f.move(0.5, 0.5, feed=900)
    j.tick()
    answer(port, streamer, "ok")
    f.end(feed=900)
    j.tick()
    assert port.lines()[-1] == "T X0.5 Y0.5 F900" and not f.active
    f.move(9, 9)                                 # let go: readings are ignored
    assert f.pos == [0.5, 0.5, 0.0]


def test_alive(clock):
    f = Follower(None)
    assert not f.alive()
    f.start((0, 0, 0))
    assert f.alive()
    clock.advance(STALE_MS)
    assert not f.alive()


def test_follower_on_grbl(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    f = Follower(j)
    f.start((0.0, 0.0, 0.0))
    f.move(0, 0, feed=1200)
    clock.advance(16)
    f.move(0.2, 0, feed=1200)
    j.tick()
    assert port.lines()[-1] == "$J=G53 G90 G21 X0.2 Y0 F1200"


# --- the web server's follow pad ---

@pytest.fixture
def server(streamer, port, clock, monkeypatch):
    monkeypatch.setattr(srv, "now_ms", clock)
    s = srv.WebServer(streamer, StatusConfig(), ".", ".")
    port.out.clear()
    return s


def test_follow_pad_frames(server, port, streamer, clock):
    answer(port, streamer, "<Idle|MPos:5.000,6.000,0.000|FS:0,0>")
    server.apply({"cmd": "jog_follow", "dx": 0, "dy": 0, "f": 800, "t": 100.0})
    assert port.lines() == ["$J"]                # takes hold, enters jog mode
    answer(port, streamer, "ok")
    server.apply({"cmd": "jog_follow", "dx": 1, "dy": -0.5, "f": 800, "t": 150.0})
    answer(port, streamer, *["ok"] * 2)          # the settings query
    server.jog.tick()
    assert any(l.startswith("T X6 VX") and " Y5.5 VY" in l for l in port.lines())
    server.apply({"cmd": "jog_follow", "dx": 1.2, "dy": -0.5, "f": 800, "t": 170.0,
                  "end": True})
    answer(port, streamer, *["ok"] * server.jog.inflight)
    server.jog.tick()
    assert port.lines()[-1] == "T X6.2 Y5.5 F800" and not server.follow.active


def test_a_quiet_pad_is_dropped_without_moving(server, port, streamer, clock):
    answer(port, streamer, "<Idle|MPos:0.000,0.000,0.000|FS:0,0>")
    server.apply({"cmd": "jog_follow", "dx": 0, "dy": 0, "t": 0.0})
    answer(port, streamer, "ok")
    clock.advance(STALE_MS)
    n = len(port.out)
    server._jog_idle_end()
    assert not server.follow.active and len(port.out) == n
