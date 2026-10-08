from gcgo.core.jog import ENTERING, KEEPALIVE_MS, OFF, ON, RETRY_MS, JogSession


def answer(port, streamer, *replies):
    port.feed("".join(r + "\r\n" for r in replies))
    streamer.service()


def on_session(port, streamer, clock, events=None):
    j = JogSession(streamer, on_event=(events.append if events is not None else None))
    j.begin()
    answer(port, streamer, "ok")
    assert j.state == ON
    port.out.clear()
    return j


def test_begin_waits_for_ok(port, streamer, clock):
    j = JogSession(streamer)
    j.vel("s", 5, 0, 0)
    j.tick()
    assert port.out == []                        # nothing before jog mode
    j.begin()
    assert j.state == ENTERING and port.lines() == ["$J"]
    answer(port, streamer, "ok")
    assert j.state == ON
    j.tick()
    assert port.lines()[-1] == "V X5"


def test_jog_replies_do_not_reach_the_frontend(port, streamer, clock):
    seen = []
    streamer.on_response = lambda i, r: seen.append(r)
    j = on_session(port, streamer, clock)
    j.vel("s", 1, 0, 0)
    j.tick()
    answer(port, streamer, "ok", "ok")           # one for V, one that is not ours
    assert seen == ["ok"] and j.inflight == 0


def test_only_the_newest_velocity_is_sent(port, streamer, clock):
    j = on_session(port, streamer, clock)
    for v in (1, 2, 3, 4, 5):
        j.vel("s", v, 0, 0)
        j.tick()
    assert port.lines() == ["V X1", "V X2"]     # two in flight, the rest coalesced
    answer(port, streamer, "ok", "ok")
    j.tick()
    assert port.lines()[-1] == "V X5"


def test_keepalive_resends_a_live_velocity_only(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.vel("s", 7, 0, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok")
    clock.advance(KEEPALIVE_MS - 1)
    j.tick()
    assert port.lines() == ["V X7"]
    clock.advance(1)
    j.tick()
    assert port.lines() == ["V X7", "V X7"]
    answer(port, streamer, "ok")
    j.vel("s", 0, 0, 0)
    j.tick()
    answer(port, streamer, "ok")
    clock.advance(10 * KEEPALIVE_MS)
    j.tick()
    assert port.lines()[-1] == "V"               # one zero, then silence
    assert port.lines().count("V") == 1


def test_inputs_own_their_axes(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.vel("stick", 10, 20, 0, ttl_ms=0)
    j.vel("dial", 0, 0, -3, ttl_ms=0)
    assert j.velocity() == (10, 20, -3)
    j.vel("dial", 5, 0, -3, ttl_ms=0)            # a fresh push takes X
    assert j.velocity() == (5, 20, -3)
    j.vel("stick", 11, 20, 0, ttl_ms=0)          # still pushing: not a fresh push
    assert j.velocity() == (5, 20, -3)
    j.vel("dial", 0, 0, -3, ttl_ms=0)            # the dial lets go of X
    assert j.velocity() == (11, 20, -3)          # X falls back to the stick
    j.release("stick")
    j.vel("stick", 0, 0, 0)                      # a centred input stomps nothing
    assert j.velocity() == (0, 0, -3)


def test_silent_input_is_zeroed(port, streamer, clock):
    events = []
    j = on_session(port, streamer, clock, events)
    j.vel("web", 4, 0, 0, ttl_ms=400)
    j.tick()
    answer(port, streamer, "ok")
    clock.advance(401)
    j.tick()
    assert j.velocity() == (0.0, 0.0, 0.0)
    assert port.lines()[-1] == "V"
    assert any("timed out" in e for e in events)


def test_steps_wait_for_the_velocity_to_end(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.vel("s", 3, 0, 0, ttl_ms=0)
    j.step("Z", 0.25)
    j.step("Z", 0.25)                            # steps add up
    j.tick()
    answer(port, streamer, "ok")
    j.tick()
    assert not any(l.startswith("S") for l in port.lines())
    j.release("s")
    j.tick()
    answer(port, streamer, "ok")
    j.tick()
    assert port.lines()[-2:] == ["V", "S Z0.5"]


def test_target_knobs_and_feed(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.rate(0.5)
    j.coordinated(True)
    j.target(x=12.5, z=-1, feed=600)
    for _ in range(3):
        j.tick()
        answer(port, streamer, "ok", "ok")
    assert port.lines() == ["R0.5", "C1", "T X12.5 Z-1 F600"]


def test_end_sends_jog_cancel_and_clears(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.vel("s", 9, 0, 0)
    j.end()
    assert port.out[-1] == b"\x85" and j.state == OFF
    assert j.velocity() == (0.0, 0.0, 0.0)


def test_refusal_backs_off(port, streamer, clock):
    events = []
    j = JogSession(streamer, on_event=events.append)
    j.begin()
    answer(port, streamer, "error:8 ; Not idle")
    assert j.state == OFF and "refused" in events[-1]
    port.out.clear()
    j.begin()
    assert port.out == []                        # no retry storm
    clock.advance(RETRY_MS)
    j.begin()
    assert port.lines() == ["$J"]


def test_controller_leaving_jog_mode_is_noticed(port, streamer, clock):
    events = []
    j = on_session(port, streamer, clock, events)
    answer(port, streamer, "<Jog|MPos:0,0,0|FS:0,0>", "<Jog|MPos:0,0,0|FS:0,0>")
    j.tick()
    assert j.state == ON
    answer(port, streamer, "<Alarm|MPos:0,0,0|FS:0,0>")
    j.tick()
    assert j.state == OFF and "ended by the controller (Alarm)" in events[-1]
