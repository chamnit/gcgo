"""JogSession on GRBL: no jog mode, so short $J= lines and 0x85."""

from gcgo.core.jog import (GRBL_BLOCK_MS, GRBL_HALT_MS, GRBL_LEAD_MS, ON,
                           JogSession)
from test_jog import answer

CANCEL = b"\x85"


def grbl_session(port, streamer, clock, events=None):
    """Begin as against GRBL: the bare $J is answered error:3."""
    j = JogSession(streamer, on_event=(events.append if events is not None else None))
    j.begin()
    assert port.lines() == ["$J"]
    answer(port, streamer, "error:3")
    assert j.grbl and j.state == ON
    port.out.clear()
    return j


def report(port, streamer, state, n=1):
    answer(port, streamer, *["<%s|MPos:0.000,0.000,0.000|FS:0,0>" % state] * n)


def test_grbl_is_detected_and_says_so(port, streamer, clock):
    events = []
    j = grbl_session(port, streamer, clock, events)
    assert "GRBL" in events[-1]
    j.tick()
    assert port.out == []                        # no settings query on GRBL
    j.end()
    port.out.clear()
    j.begin()                                    # known now: nothing to enter
    assert j.state == ON and port.out == []


def test_velocity_goes_out_as_paced_short_jogs(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.vel("s", 10, -5, 0, ttl_ms=0)              # mm/s
    j.tick()
    assert port.lines() == ["$J=G91 G21 X0.5 Y-0.25 F670.82",
                            "$J=G91 G21 X0.5 Y-0.25 F670.82"]   # MAX_INFLIGHT
    answer(port, streamer, "ok", "ok")
    j.tick()
    sent = len(port.lines())
    assert sent == GRBL_LEAD_MS // GRBL_BLOCK_MS   # the lead is full
    j.tick()
    assert len(port.lines()) == sent
    answer(port, streamer, *["ok"] * (sent - 2))
    clock.advance(GRBL_BLOCK_MS)                 # one block ran: one more goes
    j.tick()
    assert len(port.lines()) == sent + 1


def test_release_cancels_and_waits_for_the_brake(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok", "ok")
    j.release("s")
    j.tick()
    assert port.out[-2:] == [CANCEL, b"?"]       # brake, and ask when it is done
    port.out.clear()
    j.step("X", 1)                               # must not be flushed by the brake
    j.tick()
    assert port.out == []
    report(port, streamer, "Jog", 2)             # still braking
    j.tick()
    assert port.out == []
    clock.advance(30)
    j.tick()
    assert port.out == [b"?"]                    # polls while it waits
    report(port, streamer, "Idle", 2)
    j.tick()
    assert port.lines()[-1] == "$J=G91 G21 X1 F1000"


def test_a_line_unanswered_at_the_cancel_gets_a_second_cancel(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    j.tick()                                     # two lines, no ok yet
    j.release("s")
    j.tick()
    assert port.out.count(CANCEL) == 1
    answer(port, streamer, "ok")
    assert port.out.count(CANCEL) == 1
    answer(port, streamer, "ok")                 # the last one is parsed: brake again
    assert port.out.count(CANCEL) == 2
    report(port, streamer, "Idle", 2)
    j.tick()
    assert not j._halt


def test_reversal_cancels_but_a_turn_does_not(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok", "ok")
    j.vel("s", 10, 10, 0, ttl_ms=0)              # a new axis joins: no brake
    clock.advance(GRBL_LEAD_MS)
    j.tick()
    assert CANCEL not in port.out
    assert port.lines()[-1] == "$J=G91 G21 X0.5 Y0.5 F848.528"
    answer(port, streamer, "ok", "ok")
    j.vel("s", -10, 10, 0, ttl_ms=0)             # X reverses
    j.tick()
    assert port.out[-2] == CANCEL
    report(port, streamer, "Idle", 2)
    j.tick()
    assert port.lines()[-1] == "$J=G91 G21 X-0.5 Y0.5 F848.528"


def test_dead_input_is_braked(port, streamer, clock):
    events = []
    j = grbl_session(port, streamer, clock, events)
    j.vel("web", 10, 0, 0, ttl_ms=400)
    j.tick()
    answer(port, streamer, "ok", "ok")
    clock.advance(401)
    j.tick()
    assert CANCEL in port.out and "timed out" in events[-1]


def test_go_to_and_moving_target(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.feed = 600
    j.target(x=12.5, z=-1)
    j.tick()
    assert port.lines() == ["$J=G53 G90 G21 X12.5 Z-1 F600"]
    answer(port, streamer, "ok")
    port.out.clear()
    clock.advance(10000)
    j.target(x=20, vx=5, feed=3000)              # a follower
    j.tick()
    assert port.lines() == ["$J=G53 G90 G21 X20 F3000"]
    answer(port, streamer, "ok")
    j.tick()
    assert len(port.lines()) == 1                # unchanged: nothing to resend
    j.target(x=20.5, vx=5, feed=3000)
    j.target(x=21, vx=5, feed=3000)
    j.tick()
    assert port.lines()[-1] == "$J=G53 G90 G21 X21 F3000"   # newest only
    answer(port, streamer, "ok")
    j.target(x=21.2, feed=3000)                  # let go: a last go-to, no brake
    j.tick()
    assert port.lines()[-1] == "$J=G53 G90 G21 X21.2 F3000"
    assert CANCEL not in port.out


def test_rate_scales_velocity_and_c_is_ignored(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.rate(0.5)
    j.coordinated(True)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    j.tick()
    assert port.lines()[0] == "$J=G91 G21 X0.25 F300"


def test_a_refusal_is_reported_once_a_second(port, streamer, clock):
    events = []
    j = grbl_session(port, streamer, clock, events)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    for _ in range(5):
        j.tick()
        answer(port, streamer, *["error:15"] * j.inflight)
        clock.advance(GRBL_BLOCK_MS)
    assert sum("refused" in e for e in events) == 1


def test_halt_gives_up_without_status(port, streamer, clock):
    events = []
    j = grbl_session(port, streamer, clock, events)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    j.tick()
    answer(port, streamer, "ok", "ok")
    j.release("s")
    j.tick()
    clock.advance(GRBL_HALT_MS)
    j.tick()
    assert not j._halt and "going on" in events[-1]


def test_end_after_unanswered_lines_cancels_twice(port, streamer, clock):
    j = grbl_session(port, streamer, clock)
    j.vel("s", 10, 0, 0, ttl_ms=0)
    j.tick()
    j.end()
    assert port.out.count(CANCEL) == 1
    answer(port, streamer, "ok", "ok")
    assert port.out.count(CANCEL) == 2
