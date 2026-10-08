from gcgo.core.jog import (ENTERING, KEEPALIVE_MS, OFF, ON, RETRY_MS, SETTINGS_QUERY,
                           JogSession, resend_interval)


def answer(port, streamer, *replies):
    port.feed("".join(r + "\r\n" for r in replies))
    while port.any():                            # service() reads a bounded chunk
        streamer.service()


def settings_reply(keepalive=250, cover=20):
    return ("; --- jog ---",
            "$jog.cover=%d                  ; ms the same cover for a $J jog session" % cover,
            "$jog.keepalive=%d             ; ms a velocity lasts without a new line" % keepalive,
            "ok")


def on_session(port, streamer, clock, events=None, keepalive=250, cover=20):
    """Enter jog mode and answer the session's $settings.jog query."""
    j = JogSession(streamer, on_event=(events.append if events is not None else None))
    j.begin()
    answer(port, streamer, "ok")
    assert j.state == ON
    j.tick()
    assert port.lines()[-1] == SETTINGS_QUERY
    answer(port, streamer, *settings_reply(keepalive, cover))
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
    assert port.lines()[-2:] == [SETTINGS_QUERY, "U X5"]


def test_jog_replies_do_not_reach_the_frontend(port, streamer, clock):
    seen = []
    streamer.on_response = lambda i, r: seen.append(r)
    streamer.on_message = seen.append
    j = on_session(port, streamer, clock)
    assert seen == []                            # the settings query is the session's own
    j.vel("s", 1, 0, 0)
    j.tick()
    answer(port, streamer, "ok", "ok")           # one for U, one that is not ours
    assert seen == ["ok"] and j.inflight == 0


def test_only_the_newest_velocity_is_sent(port, streamer, clock):
    j = on_session(port, streamer, clock)
    for v in (1, 2, 3, 4, 5):
        j.vel("s", v, 0, 0)
        j.tick()
    assert port.lines() == ["U X1", "U X2"]     # two in flight, the rest coalesced
    answer(port, streamer, "ok", "ok")
    j.tick()
    assert port.lines()[-1] == "U X5"


def test_moving_axes_are_resent_and_a_release_is_sent_once(port, streamer, clock):
    j = on_session(port, streamer, clock)
    assert j.resend_ms == KEEPALIVE_MS
    j.vel("s", 7, -2, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok")
    clock.advance(KEEPALIVE_MS - 1)
    j.tick()
    assert port.lines() == ["U X7 Y-2"]
    clock.advance(1)
    j.tick()
    assert port.lines() == ["U X7 Y-2", "U X7 Y-2"]   # every moving axis, every time
    answer(port, streamer, "ok")
    j.vel("s", 7, 0, 0, ttl_ms=0)                # Y let go: named once at 0
    j.tick()
    answer(port, streamer, "ok")
    assert port.lines()[-1] == "U X7 Y0"
    j.vel("s", 0, 0, 0)
    j.tick()
    answer(port, streamer, "ok")
    clock.advance(10 * KEEPALIVE_MS)
    j.tick()
    assert port.lines()[-1] == "U X0"            # one zero, then silence
    assert port.lines().count("U X0") == 1


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
    assert port.lines()[-1] == "U X0"
    assert any("timed out" in e for e in events)


def test_steps_run_beside_a_velocity_on_another_axis(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.vel("s", 3, 0, 0, ttl_ms=0)
    j.step("Z", 0.25)
    j.step("Z", 0.25)                            # steps add up
    j.step("X", 1.0)                             # X is moving: this one waits
    j.tick()
    assert port.lines() == ["U X3", "S Z0.5"]
    answer(port, streamer, "ok", "ok")
    j.tick()
    assert not any(l.startswith("S X") for l in port.lines())
    j.release("s")
    j.tick()
    answer(port, streamer, "ok")
    j.tick()
    assert port.lines()[-2:] == ["U X0", "S X1"]


def test_go_to_waits_only_for_its_own_axes(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.vel("s", 0, 4, 0, ttl_ms=0)
    j.target(x=12.5, z=-1, feed=600)
    j.tick()
    assert port.lines() == ["U Y4", "T X12.5 Z-1 F600"]
    answer(port, streamer, "ok", "ok")
    j.target(y=3)                                # Y is moving
    j.tick()
    assert port.lines()[-1] == "T X12.5 Z-1 F600"


def test_knobs(port, streamer, clock):
    j = on_session(port, streamer, clock)
    j.rate(0.5)
    j.coordinated(True)
    j.tick()
    assert port.lines() == ["R0.5", "C1"]


def test_moving_target_is_resent_and_expires(port, streamer, clock):
    events = []
    j = on_session(port, streamer, clock, events)
    j.target(x=20, y=5, vx=5, feed=3000)
    j.tick()
    assert port.lines() == ["T X20 VX5 Y5 F3000"]
    answer(port, streamer, "ok")
    j.target(x=20.5, y=5, vx=5, feed=3000)       # the follower's next reading
    j.target(x=21, y=5, vx=5, feed=3000)
    j.tick()
    assert port.lines()[-1] == "T X21 VX5 Y5 F3000"   # newest only
    answer(port, streamer, "ok")
    clock.advance(KEEPALIVE_MS)
    j.tick()
    assert port.lines()[-1] == "T X21 VX5 Y5 F3000"   # resent like a velocity
    answer(port, streamer, "ok")
    j.step("X", 1)                               # X belongs to the target
    j.step("Z", 1)
    j.tick()
    assert port.lines()[-1] == "S Z1"
    answer(port, streamer, "ok")
    clock.advance(401)
    j.tick()
    # its axes stop at 0, and the X step that waited for them goes right after
    assert port.lines()[-2:] == ["U X0 Y0", "S X1"]
    assert "moving target timed out" in events


def test_older_capy_without_u_falls_back_to_v(port, streamer, clock):
    events = []
    j = on_session(port, streamer, clock, events)
    j.vel("s", 5, 0, 0, ttl_ms=0)
    j.step("Z", 1)
    j.tick()
    assert port.lines() == ["U X5", "S Z1"]
    answer(port, streamer, "error:16", "ok")
    assert not j.masked and "using V" in events[-1]
    j.step("Z", 1)
    j.tick()
    assert port.lines()[-1] == "V X5"            # complete state now
    answer(port, streamer, "ok")
    j.tick()
    assert port.lines()[-1] == "V X5"            # and the step waits for the stick
    j.release("s")
    j.tick()
    answer(port, streamer, "ok")
    j.tick()
    assert port.lines()[-2:] == ["V", "S Z1"]


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
    answer(port, streamer, "error:9 ; Not idle")
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


def test_resend_interval():
    assert resend_interval(250, 20) == 100       # the defaults
    assert resend_interval(50, 0) == 20          # 40 % of the keepalive
    assert resend_interval(50, 20) == 15         # half of what the cover leaves
    assert resend_interval(250, 80) == 85
    assert resend_interval(100, 90) == 15        # the floor
    assert resend_interval(0, 20) == KEEPALIVE_MS   # keepalive off


def test_resend_follows_the_settings(port, streamer, clock):
    j = on_session(port, streamer, clock, keepalive=50, cover=0)
    assert j.resend_ms == 20
    j.vel("s", 3, 0, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok")
    clock.advance(20)
    j.tick()
    assert port.lines() == ["U X3", "U X3"]


def test_capy_without_the_settings_group(port, streamer, clock):
    events = []
    j = JogSession(streamer, on_event=events.append)
    j.begin()
    answer(port, streamer, "ok")
    j.tick()
    answer(port, streamer, "error:3 ; Invalid statement")
    assert j.state == ON and j.resend_ms == KEEPALIVE_MS
    assert events == ["jog mode on"]             # no "refused" noise


def test_capy_braking_a_held_stick_resends_at_once(port, streamer, clock):
    events = []
    shown = []
    streamer.on_message = shown.append
    j = on_session(port, streamer, clock, events)
    j.vel("s", 6, 0, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok")
    answer(port, streamer, "[MSG:Jog stopped -- no V line within jog.keepalive]")
    j.tick()
    assert port.lines() == ["U X6", "U X6"]      # not waiting for the next resend
    assert "braked" in events[-1] and shown      # the controller's words still shown
