"""Jog session: one owner for caPy's $J jog mode, fed by any number of inputs.

caPy's jog mode (bare `$J`, from Idle) takes text commands, each answered
`ok` / `error:16` (caPy doc/jog/README.md):

    V [Xn] [Yn] [Zn]       velocity, mm/s -- a COMPLETE state: an unnamed axis is 0
    U [Xn] [Yn] [Zn]       velocity on the NAMED axes only; the others keep theirs
    S <axis><mm> [Fn]      step one axis (steps accumulate); F = speed cap, mm/min
    T [Xn].. [VXn].. [Fn]  go to a machine position; VX/VY/VZ (mm/s) make it a
                           moving target, which must be resent as it moves
    C0 / C1                independent axes / straight-line (coordinated) jog
    R<frac>                rate knob: scales V/U/T speeds (not distances)

0x85 exits (brake to rest, re-seed). The dead-man is per axis: every moving
axis needs a line naming it (V, U, or T with its velocity word) within
$jog.keepalive (default 250 ms, fixed while in jog mode), or caPy brakes
everything and sends [MSG:Jog stopped -- no V line within jog.keepalive].
The planner runs $jog.cover ahead of the motors, so that stop can come up to
the cover early. The session reads both settings on entry and resends every
moving axis well inside the window.

Input devices never write to the wire. They hand this session what the
operator wants -- a velocity vector, a step, a target -- and the session turns
that into the fewest lines: only the newest velocity is sent (a stick that
outruns the link drops stale samples, never queues them), every moving axis is
resent while held, and at most MAX_INFLIGHT lines wait for their `ok`.

Several inputs can drive one session at once (a stick on X/Y, a dial on Z).
Each is named; an input owns an axis while it pushes it, and the most recent
push wins. Velocities go out as U lines naming the moving axes (plus any axis
just released, at 0), so a stick never disturbs a step or target running on
another axis; a step or target waits only while its own axis has a live
velocity. Each velocity input (and moving target) also has a TTL: an input
that stops refreshing (a closed browser tab, a dropped gamepad) is zeroed
here, so the session's resends can never keep a dead input's motion alive.

A caPy without U answers it error:16; the session then falls back to V (a
complete state) and holds every step and target while any velocity is live.

GRBL has no jog mode: it answers the bare `$J` error:3, and the session then
drives the same inputs with short `$J=` lines (Grbl wiki, "Jogging"):

    velocity   $J=G91 G21 X.. Y.. F..   one GRBL_BLOCK_MS of motion per line,
               sent while less than GRBL_LEAD_MS is queued ahead of the clock
    stop       0x85: GRBL brakes and flushes its planner -- on release, on an
               axis reversing, on a dead input or moving target
    step       $J=G91 G21 X<mm> F..
    target     $J=G53 G90 G21 X.. F..   a moving target is resent as a go-to
               to its newest position, paced like a velocity

GRBL plans (and answers ok for) a $J= that arrives while a cancel is braking,
then flushes it, so after a 0x85 nothing goes out until fresh status reports
show it out of Jog; and a line still waiting for its ok when the 0x85 went out
may be parsed after the brake, so the 0x85 is sent again once it is answered.
The queued lead bounds the speed GRBL can reach (it must be able to stop
inside it): v <= about 2 * accel * GRBL_LEAD_MS. C (coordinated) does not
apply (a $J= line is always straight) and R scales the velocities here.

Driven from the caller's loop like Streamer.pump(): call tick() every pass.
No threads, no blocking, MicroPython-safe.
"""

from gcgo.core.clock import diff_ms, now_ms

AXES = "XYZ"

# session states
OFF = "off"            # not in jog mode
ENTERING = "entering"  # $J sent, waiting for its ok
ON = "on"              # in jog mode

MAX_INFLIGHT = 2       # jog lines awaiting ok (each is < 48 bytes of the 127-byte RX)
KEEPALIVE_MS = 100     # resend moving axes at most this far apart...
KEEPALIVE_FRAC = 0.4   # ...within this fraction of $jog.keepalive...
COVER_FRAC = 0.5       # ...and of what is left of it after $jog.cover
KEEPALIVE_MIN_MS = 15
SOURCE_TTL_MS = 400    # a velocity input or moving target not refreshed this long is zeroed
RETRY_MS = 500         # after a refused $J (still braking, alarm), wait this long
SETTINGS_QUERY = "$settings.jog"

GRBL_BLOCK_MS = 50     # GRBL: motion per velocity line
GRBL_LEAD_MS = 200     # GRBL: queue at most this much motion ahead
GRBL_POLL_MS = 30      # GRBL: status poll while waiting for a cancel to finish
GRBL_HALT_MS = 2000    # GRBL: give up waiting for that after this long
GRBL_FEED = 1000       # GRBL: mm/min for a step or target that names no F


def _num(v):
    s = "%.3f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def resend_interval(keepalive, cover=0.0):
    """How often to resend moving axes for caPy's $jog.keepalive and
    $jog.cover (ms)."""
    if keepalive <= 0:
        return KEEPALIVE_MS
    ms = min(KEEPALIVE_MS, keepalive * KEEPALIVE_FRAC, (keepalive - cover) * COVER_FRAC)
    return max(KEEPALIVE_MIN_MS, int(ms))


class JogSession:
    def __init__(self, streamer, on_event=None):
        self.s = streamer
        self.on_event = on_event      # on_event(text): errors and state changes
        self.state = OFF
        self.masked = True            # U available (False after a caPy refuses it)
        self.resend_ms = KEEPALIVE_MS # from $jog.keepalive / $jog.cover
        self._fifo = []               # verbs of our lines awaiting ok/error, oldest first
        self._src = {}                # input name -> [vx, vy, vz, refreshed_ms, ttl_ms]
        self._owner = [None, None, None]
        self._sent = [0.0, 0.0, 0.0]  # the velocity caPy has, per axis
        self._v_at = 0                # when moving axes last went out
        self._steps = [0.0, 0.0, 0.0]
        self._step_feed = [0.0, 0.0, 0.0]
        self._target = None           # (mask, [x, y, z], feed): a one-shot go-to
        self._moving = None           # [mask, pos, vel, feed, refreshed_ms, ttl_ms]
        self._moving_new = False
        self._moving_at = 0
        self._stop = 0                # axes to send at 0 (a moving target ended)
        self._knobs = []              # pending one-shot lines ("C1", "R0.5", the query)
        self._on_seq = 0              # status-report count when jog mode came on
        self._retry_at = 0            # no new $J before this (after a refusal)
        self._ka = None               # $jog.keepalive / $jog.cover as read on entry
        self._cover = 0.0
        self.grbl = False             # GRBL: no jog mode, short $J= lines
        self.feed = GRBL_FEED         # GRBL: F for a step or target without one
        self._rate = 1.0              # GRBL: the R knob, applied here
        self._gv = [0.0, 0.0, 0.0]    # GRBL: the velocity queued
        self._until = 0               # GRBL: queued motion runs until about then
        self._gend = None             # GRBL: where the last G53 line ends (None: unknown)
        self._halt = False            # GRBL: a 0x85 is braking
        self._halt_seq = 0            # GRBL: status count when it went out
        self._halt_at = 0
        self._poll_at = 0
        self._recancel = 0            # GRBL: lines unanswered when the 0x85 went out
        self._err, self._err_at = "", 0   # the last refusal reported
        streamer.jog = self           # Streamer routes our replies here

    # --- lifecycle ---

    @property
    def active(self) -> bool:
        return self.state != OFF

    @property
    def inflight(self) -> int:
        return len(self._fifo)

    def begin(self) -> None:
        """Enter jog mode. caPy accepts it from Idle only; the answer arrives
        through tick()/reply() (on_event reports a refusal)."""
        if self.state != OFF or diff_ms(now_ms(), self._retry_at) < 0:
            return
        if self.grbl:                  # nothing to enter
            self.state = ON
            self._until = now_ms()
            return
        self.state = ENTERING
        self._write("$J")

    def end(self) -> None:
        """Leave jog mode: caPy brakes to rest and re-seeds. Anything not yet
        sent is dropped; replies to lines already sent are still counted."""
        if self.state == OFF:
            return
        if self.grbl:
            self._cancel()
        else:
            self.s._send_realtime(b"\x85")
        self.state = OFF
        self._clear()

    def lost(self) -> None:
        """The controller left jog mode on its own (soft reset, alarm): forget
        the session without sending anything. Replies in flight are gone too."""
        self.state = OFF
        self._fifo = []
        self._clear()

    def _clear(self):
        self._src = {}
        self._owner = [None, None, None]
        self._sent = [0.0, 0.0, 0.0]
        self._steps = [0.0, 0.0, 0.0]
        self._target = None
        self._moving = None
        self._stop = 0
        self._knobs = []
        self._gv = [0.0, 0.0, 0.0]
        self._gend = None
        self._halt = False

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

    def target(self, x=None, y=None, z=None, feed=0.0,
               vx=0.0, vy=0.0, vz=0.0, ttl_ms=SOURCE_TTL_MS) -> None:
        """Go to a machine position on the named axes; the newest target wins.
        With a velocity (mm/s) on a named axis it is a moving target -- a
        follower: call it again as the target moves (each call is also its
        heartbeat, ttl_ms). A velocity on an axis with no position is ignored."""
        mask, pos, vel = 0, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        for i, (p, v) in enumerate(((x, vx), (y, vy), (z, vz))):
            if p is not None:
                mask |= 1 << i
                pos[i] = p
                vel[i] = v
        if not mask:
            return
        if vel != [0.0, 0.0, 0.0]:
            if self._moving:
                self._stop |= self._moving[0] & ~mask   # axes it no longer drives
            self._moving = [mask, pos, vel, feed, now_ms(), ttl_ms]
            self._moving_new = True
            if self._target and self._target[0] & mask:
                self._target = None                 # newest wins: no stale go-to after it
        else:
            if self._moving and self._moving[0] & mask:
                self._moving = None                 # a plain go-to takes over
            self._target = (mask, pos, feed)

    def coordinated(self, on: bool) -> None:
        if not self.grbl:
            self._knobs.append("C1" if on else "C0")

    def rate(self, frac: float) -> None:
        self._rate = max(0.0, frac)
        if not self.grbl:
            self._knobs.append("R" + _num(self._rate))

    # --- the loop ---

    def tick(self) -> None:
        """Send what is due. Call every pass of the driver loop."""
        if self.state != ON:
            return
        if self.grbl:
            self._tick_grbl()
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
        live = 0                               # axes with a velocity
        for i in range(3):
            if v[i] != 0.0:
                live |= 1 << i
        busy = live | (self._moving[0] if self._moving else 0)
        while self.inflight < MAX_INFLIGHT:
            changed = self._stop
            for i in range(3):
                if v[i] != self._sent[i]:
                    changed |= 1 << i
            due = live and diff_ms(now, self._v_at) >= self.resend_ms
            if self._knobs:
                self._write(self._knobs.pop(0))
            elif changed or due:
                self._send_velocity(v, live | changed, now)
            elif self._moving and (self._moving_new or
                                   diff_ms(now, self._moving_at) >= self.resend_ms):
                self._send_moving(now)
            elif not self.masked and busy:
                break                          # V would cancel a step or target
            elif self._target is not None and not self._target[0] & busy:
                mask, pos, feed = self._target
                self._target = None
                words = " ".join(AXES[i] + _num(pos[i]) for i in range(3) if mask & (1 << i))
                self._write("T " + words + (" F" + _num(feed) if feed else ""))
            else:
                i = self._next_step(busy)
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
        verb = self._fifo.pop(0) if self._fifo else ""
        err = line.startswith("error")
        if verb == "J" and self._recancel:
            # parsed after the 0x85 went out, it may have started a move
            self._recancel -= 1
            if not self._recancel:
                self.s._send_realtime(b"\x85")
                self._halt_seq = self.s._status_seq
        if verb == "$J":
            if self.state != ENTERING:
                return
            if line.startswith("error:3"):     # GRBL: "$J" is not a command
                self.grbl = True
                self.state = ON
                self._knobs = []
                self._until = now_ms()
                self._event("GRBL has no jog mode: jogging with short $J= lines")
                return
            self.state = OFF if err else ON
            self._on_seq = self.s._status_seq
            if err:   # drop what waited for the session: it must not move later
                self._clear()
                self._retry_at = now_ms() + RETRY_MS
                self._event("jog mode refused: " + line)
            else:
                self._ka, self._cover = None, 0.0
                self._knobs.insert(0, SETTINGS_QUERY)
                self._event("jog mode on")
        elif verb == SETTINGS_QUERY:
            # a caPy without the group answers an error: keep the default
            if not err and self._ka is not None:
                self.resend_ms = resend_interval(self._ka, self._cover)
        elif err and verb == "U" and self.masked:
            self.masked = False                # an older caPy: complete-state V
            self._sent = [0.0, 0.0, 0.0]       # resend the velocity as V
            self._event("caPy has no U (masked jog): using V, steps wait for the stick")
        elif err:
            # a held input repeats a refused line (a soft limit) every pass:
            # say it once a second
            now = now_ms()
            if line != self._err or diff_ms(now, self._err_at) >= 1000:
                self._err, self._err_at = line, now
                self._event("jog line refused: " + line)

    def note(self, line: str) -> bool:
        """A non-ok line from the controller while in jog mode (routed by
        Streamer). True = it was the session's own and is not shown."""
        if self._fifo and self._fifo[0] == SETTINGS_QUERY and line[:1] in ("$", ";"):
            key, _, val = line.partition("=")
            try:
                num = float(val.split(";", 1)[0])
            except ValueError:
                return True
            if key == "$jog.keepalive":
                self._ka = num
            elif key == "$jog.cover":
                self._cover = num
            return True
        if line.startswith("[MSG:Jog stopped"):
            # caPy braked an axis that went unrefreshed (a stalled loop or
            # link); if the input is still held, the next tick resends it
            self._sent = [0.0, 0.0, 0.0]
            self._moving_new = self._moving is not None
            self._event("caPy braked the jog: no update within $jog.keepalive")
        return False

    # --- internals ---

    def _send_velocity(self, v, axes, now):
        if self.masked:
            words = " ".join(AXES[i] + _num(v[i]) for i in range(3) if axes & (1 << i))
            self._write("U " + words)
        else:
            words = " ".join(AXES[i] + _num(v[i]) for i in range(3) if v[i] != 0.0)
            self._write("V " + words if words else "V")
        self._sent = [v[0], v[1], v[2]]
        self._stop = 0
        self._v_at = now

    def _send_moving(self, now):
        mask, pos, vel, feed = self._moving[:4]
        words = []
        for i in range(3):
            if mask & (1 << i):
                words.append(AXES[i] + _num(pos[i]))
                if vel[i]:
                    words.append("V" + AXES[i] + _num(vel[i]))
        self._write("T " + " ".join(words) + (" F" + _num(feed) if feed else ""))
        self._moving_new = False
        self._moving_at = now

    def _write(self, line):
        self.s.write_line(line, "jog")
        if line.startswith("$J="):
            self._fifo.append("J")
        else:
            self._fifo.append(line if line[:1] == "$" else line[:1])

    def _event(self, text):
        if self.on_event:
            self.on_event(text)

    def _expire(self, now):
        dead = [n for n, s in self._src.items() if s[4] and diff_ms(now, s[3]) > s[4]]
        for n in dead:
            self.release(n)
            self._event("jog input %s timed out" % n)
        m = self._moving
        if m and m[5] and diff_ms(now, m[4]) > m[5]:
            self._stop |= m[0]                 # stop its axes with a U at 0
            self._moving = None
            self._event("moving target timed out")

    def _reassign(self):
        # an axis left without an owner goes to another input still pushing it
        for i in range(3):
            if self._owner[i] is None or self._owner[i] not in self._src:
                self._owner[i] = None
                for n, s in self._src.items():
                    if s[i] != 0.0:
                        self._owner[i] = n
                        break

    # --- GRBL: short $J= lines ---

    def _tick_grbl(self):
        now = now_ms()
        self._expire(now)
        if self._halt and not self._halt_done(now):
            return
        r = self._rate
        v = self.velocity()
        v = (v[0] * r, v[1] * r, v[2] * r)
        live = v != (0.0, 0.0, 0.0)
        g = self._gv
        if self._stop or (g != [0.0, 0.0, 0.0] and (
                not live or g[0] * v[0] < 0 or g[1] * v[1] < 0 or g[2] * v[2] < 0)):
            self._stop = 0                     # released, reversed, or a dead input
            self._halt_start(now)
            return
        if diff_ms(self._until, now) < 0:
            self._until = now
        while self.inflight < MAX_INFLIGHT:
            room = diff_ms(self._until, now) < GRBL_LEAD_MS
            busy = live or self._moving is not None
            if live:
                if not room:
                    break
                self._grbl_velocity(v)
            elif self._moving and self._moving_new:
                if not room:
                    break
                mask, pos, vel, feed = self._moving[:4]
                self._moving_new = False
                self._grbl_goto(mask, pos, feed)
            elif self._target is not None and not live:
                mask, pos, feed = self._target
                self._target = None
                self._grbl_goto(mask, pos, feed)
            else:
                i = self._next_step(7 if busy else 0)
                if i < 0:
                    break
                mm, feed = self._steps[i], self._step_feed[i] or self.feed
                self._steps[i] = 0.0
                self._gend = None
                self._write("$J=G91 G21 " + AXES[i] + _num(mm) + " F" + _num(feed))
                self._until += int(abs(mm) / feed * 60000)

    def _grbl_velocity(self, v):
        dt = GRBL_BLOCK_MS / 1000.0
        words = [AXES[i] + _num(v[i] * dt) for i in range(3) if v[i] != 0.0]
        words = [w for w in words if w[1:] != "0"]
        self._until += GRBL_BLOCK_MS
        self._gv = [v[0], v[1], v[2]]
        if words:                              # (a crawl below 0.01 mm/s rounds away)
            speed = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
            self._gend = None
            self._write("$J=G91 G21 " + " ".join(words) + " F" + _num(speed * 60))

    def _grbl_goto(self, mask, pos, feed):
        if self._gend and all(self._gend[i] == pos[i] for i in range(3) if mask & (1 << i)):
            return                             # the queue already ends there
        feed = feed or self.feed
        start = self._gend or self.s.status.mpos
        d2 = 0.0
        for i in range(3):
            if mask & (1 << i):
                d2 += (pos[i] - start[i]) ** 2
        words = " ".join(AXES[i] + _num(pos[i]) for i in range(3) if mask & (1 << i))
        self._write("$J=G53 G90 G21 " + words + " F" + _num(feed))
        end = list(start)
        for i in range(3):
            if mask & (1 << i):
                end[i] = pos[i]
        self._gend = end
        self._until += int(d2 ** 0.5 / feed * 60000)

    def _cancel(self):
        """0x85, and again once every line still unanswered has been parsed."""
        self.s._send_realtime(b"\x85")
        self._recancel = self.inflight
        self._gv = [0.0, 0.0, 0.0]
        self._gend = None

    def _halt_start(self, now):
        self._cancel()
        self._halt = True
        self._halt_seq = self.s._status_seq
        self._halt_at = now
        self.s.request_status()
        self._poll_at = now + GRBL_POLL_MS

    def _halt_done(self, now):
        # two fresh reports out of Jog, and nothing unanswered that could move
        st = self.s.status.state
        if (not self._recancel and self.s._status_seq >= self._halt_seq + 2
                and not st.startswith("Jog")):
            self._halt = False
        elif diff_ms(now, self._halt_at) >= GRBL_HALT_MS:
            self._halt = False
            self._event("GRBL jog cancel: no status out of Jog, going on")
        elif diff_ms(now, self._poll_at) >= 0:
            self.s.request_status()
            self._poll_at = now + GRBL_POLL_MS
        if self._halt:
            return False
        self._until = now
        return True

    def _next_step(self, busy):
        for i in range(3):
            if self._steps[i] != 0.0 and not busy & (1 << i):
                return i
        return -1
