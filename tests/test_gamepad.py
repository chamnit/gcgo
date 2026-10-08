import pytest

from gcgo.core.gamepad import SOURCE, GamepadJog


class Reader:
    name = "test pad"

    def __init__(self):
        self.axes, self.buttons, self.pressed, self.present = {}, set(), [], True

    def state(self):
        if not self.present:
            return None
        p, self.pressed = tuple(self.pressed), []
        return {"axes": dict(self.axes), "buttons": tuple(self.buttons), "pressed": p}

    def tap(self, b):
        self.pressed.append(b)


class Jog:
    """Records the calls a JogSession would get."""

    def __init__(self):
        self.calls, self.v = [], {}

    def begin(self):
        self.calls.append("begin")

    def end(self):
        self.calls.append("end")

    def vel(self, src, vx, vy, vz):
        self.v[src] = (vx, vy, vz)

    def release(self, src):
        self.calls.append("release")
        self.v.pop(src, None)

    def step(self, axis, mm):
        self.calls.append(("step", axis, mm))


@pytest.fixture
def rig():
    r, j = Reader(), Jog()
    return r, j, GamepadJog(r, feed=1200)


def test_left_stick_is_xy_with_up_positive(rig):
    r, j, g = rig
    r.axes = {"lx": 1.0, "ly": -1.0}             # right and up (Linux: up is -1)
    g.poll(j)
    vx, vy, vz = j.v[SOURCE]
    assert vx > 0 and vy > 0 and vz == 0
    assert (vx * vx + vy * vy) ** 0.5 == pytest.approx(20.0)   # 1200 mm/min
    assert "begin" in j.calls


def test_right_stick_is_z_at_half_feed(rig):
    r, j, g = rig
    r.axes = {"ry": -1.0}
    g.poll(j)
    assert j.v[SOURCE] == pytest.approx((0, 0, 10.0))


def test_centring_sends_one_zero(rig):
    r, j, g = rig
    r.axes = {"lx": 0.5}
    g.poll(j)
    r.axes = {"lx": 0.0}
    g.poll(j)
    assert j.v[SOURCE] == (0.0, 0.0, 0.0)
    del j.v[SOURCE]
    g.poll(j)
    assert SOURCE not in j.v                     # silent while centred


def test_steps_and_step_size(rig):
    r, j, g = rig
    for b in ("dright", "dleft", "dup", "ddown", "y", "a"):
        r.tap(b)
    g.poll(j)
    steps = [c for c in j.calls if c[0] == "step"]
    assert steps == [("step", "X", 1.0), ("step", "X", -1.0), ("step", "Y", 1.0),
                     ("step", "Y", -1.0), ("step", "Z", 1.0), ("step", "Z", -1.0)]
    r.tap("x")
    g.poll(j)
    r.tap("dright")
    g.poll(j)
    assert j.calls[-1] == ("step", "X", 10.0)


def test_speed_ranges(rig):
    r, j, g = rig
    r.tap("lb")
    r.tap("lb")
    g.poll(j)
    r.axes = {"lx": 1.0}
    g.poll(j)
    assert j.v[SOURCE][0] == pytest.approx(20.0 * 0.2)


def test_b_stops_until_the_sticks_centre(rig):
    r, j, g = rig
    r.axes = {"lx": 1.0}
    g.poll(j)
    r.tap("b")
    g.poll(j)
    assert j.calls[-1] == "end"
    j.calls.clear()
    g.poll(j)
    g.poll(j)
    assert "begin" not in j.calls                # stick still held: ignored
    r.axes = {}
    g.poll(j)
    r.axes = {"lx": 1.0}
    g.poll(j)
    assert "begin" in j.calls


def test_unplug_releases(rig):
    r, j, g = rig
    r.axes = {"lx": 1.0}
    g.poll(j)
    r.present = False
    g.poll(j)
    assert "release" in j.calls and SOURCE not in j.v


def test_buttons_held_at_connect_are_not_presses():
    class NoLatch(Reader):
        def state(self):
            s = super().state()
            del s["pressed"]
            return s
    r, j = NoLatch(), Jog()
    g = GamepadJog(r)
    r.buttons = {"y"}
    g.poll(j)
    assert not any(c[0] == "step" for c in j.calls)
    r.buttons = set()
    g.poll(j)
    r.buttons = {"y"}
    g.poll(j)
    assert ("step", "Z", 1.0) in j.calls
