"""Follower: the machine follows a point an input moves (a drag, a probe tip,
another machine), through JogSession's moving targets.

The input reports where the point is, as an offset from where it took hold;
this turns each reading into jog.target(pos, velocity). The velocity is
estimated here from successive readings (smoothed, so a jittery link does not
shake the machine) and lets caPy extrapolate between readings, so the machine
tracks the point instead of chasing the last sample. On GRBL the session
sends the newest position as a go-to (core/jog.py).

    f = Follower(jog)
    f.start(status.mpos)               # take hold: offsets are from here
    f.move(dx, dy, feed=..)            # every reading (it is also the heartbeat)
    f.end(feed=..)                     # let go: a last go-to to the final point

Readings may carry the input's own clock (t_ms, any millisecond count that
only goes up): the velocity is then measured on it, not on arrival times.
A reading with no velocity becomes a plain go-to (no heartbeat needed while
the point rests); a moving target that stops being refreshed is braked by the
session's TTL.

No threads, no allocation per reading beyond two small lists; MicroPython-safe.
"""

from gcgo.core.clock import diff_ms, now_ms

AXES = "XYZ"
TAU_MS = 50          # velocity smoothing time constant
V_MIN = 0.05         # mm/s: a slower estimate is a point at rest
STALE_MS = 1000      # alive() is False after this long with no reading


class Follower:
    def __init__(self, jog, axes="XY", tau_ms=TAU_MS):
        self.jog = jog
        self.mask = [a in axes.upper() for a in AXES]
        self.tau = tau_ms
        self.active = False
        self.anchor = [0.0, 0.0, 0.0]
        self.pos = None               # the point, machine mm
        self.vel = [0.0, 0.0, 0.0]    # its smoothed velocity, mm/s
        self._t = 0                   # input clock of the last reading
        self._own = False             # _t is the input's clock (not now_ms)
        self._seen = 0                # now_ms() of the last reading
        self._rest = None             # the go-to last sent at rest

    def start(self, anchor) -> None:
        """Take hold: offsets from now on are from `anchor` (machine mm)."""
        self.anchor = [anchor[0], anchor[1], anchor[2]]
        self.pos = None
        self.vel = [0.0, 0.0, 0.0]
        self._rest = None
        self._seen = now_ms()
        self.active = True

    def move(self, dx=0.0, dy=0.0, dz=0.0, feed=0.0, t_ms=None) -> None:
        """A reading: the point is at anchor + (dx, dy, dz) mm. feed caps the
        machine's speed (mm/min, 0 = the axis maximum on caPy)."""
        if not self.active:
            return
        pos = [self.anchor[0] + dx, self.anchor[1] + dy, self.anchor[2] + dz]
        now = now_ms()
        own = t_ms is not None
        t = t_ms if own else now
        if self.pos is not None and own == self._own:
            dt = (t - self._t) if own else diff_ms(t, self._t)
            if dt > 0:
                k = min(1.0, dt / self.tau)
                for i in range(3):
                    inst = (pos[i] - self.pos[i]) * 1000.0 / dt
                    self.vel[i] += k * (inst - self.vel[i])
            elif dt < 0:                       # out of order: drop it
                return
        self.pos, self._t, self._own, self._seen = pos, t, own, now
        v = [self.vel[i] if self.mask[i] and abs(self.vel[i]) >= V_MIN else 0.0
             for i in range(3)]
        if v == [0.0, 0.0, 0.0]:
            # at rest: one go-to, not one per reading
            if self._rest == pos:
                return
            self._rest = pos
        else:
            self._rest = None
        self._send(pos, v, feed)

    def end(self, feed=0.0) -> None:
        """Let go: the machine goes to the last point and stops there."""
        if self.active and self.pos is not None:
            self._send(self.pos, (0.0, 0.0, 0.0), feed)
        self.active = False
        self.vel = [0.0, 0.0, 0.0]

    def alive(self) -> bool:
        """Held, and read within STALE_MS (a dead input is not)."""
        return self.active and diff_ms(now_ms(), self._seen) < STALE_MS

    def _send(self, pos, v, feed):
        m = self.mask
        self.jog.target(x=pos[0] if m[0] else None, y=pos[1] if m[1] else None,
                        z=pos[2] if m[2] else None, feed=feed,
                        vx=v[0], vy=v[1], vz=v[2])
