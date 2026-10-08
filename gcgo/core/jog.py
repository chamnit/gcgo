"""Jog session: one owner for caPy's $J jog mode, fed by any number of inputs.

caPy's jog mode (bare `$J`, from Idle) takes text commands, each answered
`ok` / `error:16`:

    V [Xn] [Yn] [Zn]       velocity, mm/s -- a COMPLETE state: an unnamed axis is 0
    S <axis><mm> [Fn]      step one axis (steps accumulate); F = speed cap, mm/min
    T [Xn] [Yn] [Zn] [Fn]  go to a machine position; F = speed cap, mm/min
    C0 / C1                independent axes / straight-line (coordinated) jog
    R<frac>                rate knob: scales V/S/T speeds (not distances)

0x85 exits (brake to rest, re-seed). A live velocity needs a fresh V line
within $jog.keepalive (default 250 ms) or caPy brakes it to rest itself.

Input devices never write to the wire. They hand this session what the
operator wants -- a velocity vector, a step, a target -- and the session turns
that into the fewest lines: only the newest velocity is sent (a stick that
outruns the link drops stale samples, never queues them), it is re-sent while
held so the keepalive never fires on a live stick, and at most MAX_INFLIGHT
lines wait for their `ok` at a time.

Several inputs can drive one session at once (a stick on X/Y, a dial on Z).
Each is named; an input owns an axis while it pushes it, and the most recent
push wins. Because V is a complete state, every V sent carries every input's
axes. Each velocity input also has a TTL: an input that stops refreshing (a
closed browser tab, a dropped gamepad) is zeroed here, so the session's own
keepalive can never keep a dead input's motion alive.

One limit of caPy's wire: V sets every axis, so a V line also cancels a step
or target still in progress. Steps and targets are therefore held back while
any velocity is live, and sent once it has been zeroed.

Driven from the caller's loop like Streamer.pump(): call tick() every pass.
No threads, no blocking, MicroPython-safe.
"""

from gcgo.core.clock import diff_ms, now_ms

AXES = "XYZ"

# session states
OFF = "off"            # not in jog mode
ENTERING = "entering"  # $J sent, waiting for its ok
ON = "on"              # in jog mode

MAX_INFLIGHT = 2       # jog lines awaiting ok (each is < 40 bytes of the 127-byte RX)
KEEPALIVE_MS = 100     # re-send a live V this often (caPy's default window is 250 ms)
SOURCE_TTL_MS = 400    # a velocity input not refreshed this long is zeroed
RETRY_MS = 500         # after a refused $J (still braking, alarm), wait this long


def _num(v):
    s = "%.3f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


class JogSession:
    def __init__(self, streamer, on_event=None):
        self.s = streamer
        self.on_event = on_event      # on_event(text): errors and state changes
        self.state = OFF
        self.inflight = 0             # our lines sent, ok/error not yet seen
        self._src = {}                # input name -> [vx, vy, vz, refreshed_ms, ttl_ms]
        self._owner = [None, None, None]
        self._sent_v = (0.0, 0.0, 0.0)
        self._v_at = 0                # when the last V went out
        self._steps = [0.0, 0.0, 0.0]
        self._step_feed = [0.0, 0.0, 0.0]
        self._target = None           # (mask, [x, y, z], feed)
        self._knobs = []              # pending one-shot lines ("C1", "R0.5")
        self._on_seq = 0              # status-report count when jog mode came on
        self._retry_at = 0            # no new $J before this (after a refusal)
        streamer.jog = self           # Streamer routes our replies here

    # --- lifecycle ---

    @property
    def active(self) -> bool:
        return self.state != OFF

    def begin(self) -> None:
        """Enter jog mode. caPy accepts it from Idle only; the answer arrives
        through tick()/reply() (on_event reports a refusal)."""
        if self.state != OFF or diff_ms(now_ms(), self._retry_at) < 0:
            return
        self.state = ENTERING
        self._write("$J")

    def end(self) -> None:
        """Leave jog mode: caPy brakes to rest and re-seeds. Anything not yet
        sent is dropped; replies to lines already sent are still counted."""
        if self.state == OFF:
            return
        self.s._send_realtime(b"\x85")
        self.state = OFF
        self._clear()

    def lost(self) -> None:
        """The controller left jog mode on its own (soft reset, alarm): forget
        the session without sending anything. Replies in flight are gone too."""
        self.state = OFF
        self.inflight = 0
        self._clear()

    def _clear(self):
        self._src = {}
        self._owner = [None, None, None]
        self._sent_v = (0.0, 0.0, 0.0)
        self._steps = [0.0, 0.0, 0.0]
        self._target = None
        self._knobs = []

    # --- what inputs call ---

    def vel(self, source, vx=0.0, vy=0.0, vz=0.0, ttl_ms=SOURCE_TTL_MS) -> None:
        """Input `source` wants this velocity (mm/s) on the axes it is pushing;
        0 on an axis releases it. Call it every time the input is read -- the
        call is also the input's heartbeat (ttl_ms; 0 = no TTL, for inputs that
        are sure to send their own release)."""
        old = self._src.get(source)
        v = (vx, vy, vz)
        for i in range(3):
            was = old[i] if old else 0.0
            if v[i] != 0.0 and (was == 0.0 or self._owner[i] is None):
                self._owner[i] = source       # a fresh push takes the axis
            elif v[i] == 0.0 and self._owner[i] == source:
                self._owner[i] = None
        self._src[source] = [vx, vy, vz, now_ms(), ttl_ms]
        if vx == 0.0 and vy == 0.0 and vz == 0.0:
            del self._src[source]
        self._reassign()

    def release(self, source) -> None:
        """Input `source` is gone (disconnected, mode switched)."""
        self.vel(source, 0.0, 0.0, 0.0)

    def step(self, axis, mm, feed=0.0) -> None:
        """Move `axis` ("X"/"Y"/"Z") by mm. Steps add up; feed caps the speed
        (mm/min, 0 = the axis maximum)."""
        i = AXES.index(axis.upper())
        self._steps[i] += mm
        self._step_feed[i] = feed

    def target(self, x=None, y=None, z=None, feed=0.0) -> None:
        """Go to a machine position on the named axes; the newest target wins."""
        mask, pos = 0, [0.0, 0.0, 0.0]
        for i, p in enumerate((x, y, z)):
            if p is not None:
                mask |= 1 << i
                pos[i] = p
        if mask:
            self._target = (mask, pos, feed)

    def coordinated(self, on: bool) -> None:
        self._knobs.append("C1" if on else "C0")

    def rate(self, frac: float) -> None:
        self._knobs.append("R" + _num(max(0.0, frac)))

    # --- the loop ---

    def tick(self) -> None:
        """Send what is due. Call every pass of the driver loop."""
        if self.state != ON:
            return
        # caPy reports Jog for the whole mode, at rest too. Two fresh reports
        # in another state mean it ended without us (an alarm, a reset).
        st = self.s.status.state
        if self.s._status_seq >= self._on_seq + 2 and not st.startswith("Jog"):
            self.lost()
            self._event("jog mode ended by the controller (%s)" % st)
            return
        now = now_ms()
        self._expire(now)
        v = self.velocity()
        live = v != (0.0, 0.0, 0.0)
        while self.inflight < MAX_INFLIGHT:
            if self._knobs:
                self._write(self._knobs.pop(0))
            elif v != self._sent_v or (live and diff_ms(now, self._v_at) >= KEEPALIVE_MS):
                words = " ".join(AXES[i] + _num(v[i]) for i in range(3) if v[i] != 0.0)
                self._write("V " + words if words else "V")
                self._sent_v = v
                self._v_at = now
            elif live:
                break                          # steps/targets wait for the stick
            elif self._target is not None:
                mask, pos, feed = self._target
                self._target = None
                words = " ".join(AXES[i] + _num(pos[i]) for i in range(3) if mask & (1 << i))
                self._write("T " + words + (" F" + _num(feed) if feed else ""))
            else:
                i = self._next_step()
                if i < 0:
                    break
                mm, feed = self._steps[i], self._step_feed[i]
                self._steps[i] = 0.0
                self._write("S " + AXES[i] + _num(mm) + (" F" + _num(feed) if feed else ""))

    def velocity(self):
        """The merged velocity: each axis from the input that owns it."""
        out = [0.0, 0.0, 0.0]
        for i in range(3):
            src = self._src.get(self._owner[i])
            if src:
                out[i] = src[i]
        return (out[0], out[1], out[2])

    def reply(self, line: str) -> None:
        """An ok/error for one of our lines (routed by Streamer)."""
        if self.inflight:
            self.inflight -= 1
        err = line.startswith("error")
        if self.state == ENTERING:
            self.state = OFF if err else ON
            self._on_seq = self.s._status_seq
            if err:   # drop what waited for the session: it must not move later
                self._clear()
                self._retry_at = now_ms() + RETRY_MS
            self._event("jog mode refused: " + line if err else "jog mode on")
        elif err:
            self._event("jog line refused: " + line)

    # --- internals ---

    def _write(self, line):
        self.s._send_raw(line)
        self.inflight += 1

    def _event(self, text):
        if self.on_event:
            self.on_event(text)

    def _expire(self, now):
        dead = [n for n, s in self._src.items() if s[4] and diff_ms(now, s[3]) > s[4]]
        for n in dead:
            self.release(n)
            self._event("jog input %s timed out" % n)

    def _reassign(self):
        # an axis left without an owner goes to another input still pushing it
        for i in range(3):
            if self._owner[i] is None or self._owner[i] not in self._src:
                self._owner[i] = None
                for n, s in self._src.items():
                    if s[i] != 0.0:
                        self._owner[i] = n
                        break

    def _next_step(self):
        for i in range(3):
            if self._steps[i] != 0.0:
                return i
        return -1
