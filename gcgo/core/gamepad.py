"""Gamepad jog bindings: named controls in, JogSession calls out.

Device-independent: a reader (gcgo/desktop/gamepad_linux.py, or a future
Bluetooth/HID one) turns its events into a snapshot of named controls, and
this maps them to jogging. Nothing here touches hardware, so the same feel
runs wherever a reader exists.

A snapshot is {"axes": {...}, "buttons": (held...), "pressed": (...)}, with
Xbox names (a reader maps its own device onto them):
    axes     lx ly rx ry  (-1..1, up = -1 as Linux reports it)
    buttons  a b x y lb rb back start ls rs, and the d-pad as dleft dright dup ddown
    pressed  every button pressed since the last snapshot -- latched by the
             reader so a tap shorter than the poll is not lost (a reader that
             cannot latch may omit it; presses then come from snapshot edges)

Default bindings:
    left stick     X/Y velocity (a vector: direction kept, speed by deflection)
    right stick    Z velocity (up = Z+)
    d-pad          step X/Y by the step size
    y / a          step Z+ / Z-
    x              cycle the step size
    lb / rb        slower / faster speed range (a fraction of the max feed)
    b              stop: brake and leave jog mode
"""

from gcgo.core.jogmap import stick_to_velocity

SOURCE = "gamepad"
STEPS = (0.01, 0.1, 1.0, 10.0)          # mm
RANGES = (0.05, 0.2, 0.5, 1.0)          # fractions of the max feed
DEADBAND = 0.12                         # gamepad sticks rest off-centre more than a pendant's
_STEP_KEYS = {"dright": ("X", 1), "dleft": ("X", -1), "dup": ("Y", 1), "ddown": ("Y", -1),
              "y": ("Z", 1), "a": ("Z", -1)}


class GamepadJog:
    """A jog input: poll(jog) reads `reader` (anything with state() returning
    a snapshot or None, and a .name) and drives the session."""

    def __init__(self, reader, feed=3000.0, z_feed=None, deadband=DEADBAND,
                 steps=STEPS, ranges=RANGES, on_event=None):
        self.reader = reader
        self.feed = feed                  # mm/min at full stick, top range
        self.z_feed = z_feed or feed / 2
        self.deadband = deadband
        self.steps = steps
        self.ranges = ranges
        self.step_i = steps.index(1.0) if 1.0 in steps else 0
        self.range_i = len(ranges) - 1
        self.on_event = on_event
        self._prev = None                 # the last snapshot, for press edges
        self._was_live = False
        self._stopped = False             # after b: ignore the sticks until centred

    def poll(self, jog) -> None:
        """Read the pad and drive `jog`. Call every driver pass."""
        pad = self.reader.state()
        if pad is None:
            if self._prev is not None:
                self._event("gamepad disconnected")
            if self._was_live:
                jog.release(SOURCE)
                self._was_live = False
            self._prev = None
            return
        if self._prev is None:
            self._event("gamepad connected: %s" % getattr(self.reader, "name", ""))
        ax, btn = pad["axes"], pad["buttons"]
        first = self._prev is None
        prev_btn = self._prev["buttons"] if self._prev else ()
        self._prev = pad
        pressed = pad.get("pressed")
        if pressed is None:      # a reader that does not latch: edges between snapshots
            # (a control already held when the pad connects is not a press)
            pressed = [] if first else [b for b in btn if b not in prev_btn]

        # sticks: one velocity per pass (complete state; zero releases)
        k = self.ranges[self.range_i]
        vx, vy, _ = stick_to_velocity(ax.get("lx", 0.0), -ax.get("ly", 0.0), 0.0,
                                      speed=self.feed * k / 60.0, deadband=self.deadband)
        _, _, vz = stick_to_velocity(0.0, 0.0, -ax.get("ry", 0.0),
                                     speed=self.z_feed * k / 60.0, deadband=self.deadband)
        live = vx != 0.0 or vy != 0.0 or vz != 0.0
        if self._stopped:
            if live:
                vx = vy = vz = 0.0
                live = False
            else:
                self._stopped = False
        if live:
            jog.begin()
        if live or self._was_live:
            jog.vel(SOURCE, vx, vy, vz)
        self._was_live = live

        for b in pressed:
            step = self.steps[self.step_i]
            if b in _STEP_KEYS:
                axis, sign = _STEP_KEYS[b]
                jog.begin()
                jog.step(axis, sign * step)
            elif b == "x":
                self.step_i = (self.step_i + 1) % len(self.steps)
                self._event("gamepad step %g mm" % self.steps[self.step_i])
            elif b in ("lb", "rb"):
                d = -1 if b == "lb" else 1
                self.range_i = max(0, min(len(self.ranges) - 1, self.range_i + d))
                self._event("gamepad speed %d%% of %g mm/min"
                            % (self.ranges[self.range_i] * 100, self.feed))
            elif b == "b":
                jog.end()
                self._stopped = True
                self._was_live = False

    def _event(self, text):
        if self.on_event:
            self.on_event(text)
