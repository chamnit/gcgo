"""Portable local-UI front-end: SSD1306 OLED + rotary encoder + 4 buttons.

A standalone pendant (no WiFi, no host). Drives the same Streamer core as the
terminal/web front-ends through the non-blocking pump(), rendering to a Display
adapter and consuming high-level events from an Input adapter (ports/base.py).
No platform imports beyond os (file listing), so it runs on MicroPython on the
board and on CPython for the PNG preview (tools/preview_local.py).

Controls -- 4 buttons (X, Y, Z, Menu) + rotary encoder (turn + click):

  JOG (home)   X/Y/Z = pick axis;  long-press axis = zero it
               turn = jog active axis by step;  click = cycle step size
               (on caPy the wheel drives a jog-mode session, core/jog.py:
               detents add up and land exactly, and a wheel spun faster than
               the axis can follow drops detents instead of running on)
               Menu = open the menu
  MENU         turn = scroll;  click = select;  Menu = back to jog
               items: Files, Home ($H), Unlock ($X), Units, Reset
  FILES        turn = scroll;  click = open dir / pick file (-> confirm)
               ".." entry goes up;  Menu = back to menu
  CONFIRM      click = start the job;  Menu = cancel
  RUN          turn = feed override +/-10% (value shown);  click = reset 100%
               X = hold/resume;  Z = stop

Input events: "cw" "ccw" "click" "x" "y" "z" "x_hold" "y_hold" "z_hold" "menu".
"""

import os

from gcgo.core.clock import now_ms, diff_ms
from gcgo.core.config import REPORT_MM
from gcgo.core.gcode import validate_gcode
from gcgo.core.jog import JogSession
from gcgo.core.protocol import FINISHING

# 1-bit OLED palette: off (background) and on (lit pixel). Highlight = an
# inverse bar (fill FG, draw text in BG) since there's no color to work with.
BG = (0, 0, 0)
FG = (255, 255, 255)

AXES = ("X", "Y", "Z")
STEPS = (0.1, 1.0, 10.0, 100.0)
MENU = ("files", "home", "unlock", "units", "reset")
_GCODE_EXT = (".gcode", ".nc", ".g", ".gc", ".ngc")
DIAL_LEAD_MM = 2.0     # how far the wheel may run ahead of the axis (at least 2 detents)
DIAL_LAG_S = 0.15      # + the jog feed times this: the reported position is this old, worst case
JOG_IDLE_MS = 1500     # leave jog mode this long after the last detent, once on target
JOG_POLL_MS = 50       # status poll while jogging (the lead check reads the position)
AFTER_JOG_MS = 3000    # give up waiting for Idle after leaving jog mode


def _is_gcode(n):
    nl = n.lower()
    for e in _GCODE_EXT:
        if nl.endswith(e):
            return True
    return False


def _list(path):
    """Portable directory listing -> sorted [(name, is_dir)], dirs first,
    keeping only sub-directories and g-code files."""
    out = []
    try:
        try:                         # MicroPython: type flag is free
            for e in os.ilistdir(path):
                name, typ = e[0], e[1]
                isdir = bool(typ & 0x4000)
                if isdir or _is_gcode(name):
                    out.append((name, isdir))
        except AttributeError:       # CPython
            for name in os.listdir(path):
                isdir = os.path.isdir(path + "/" + name)
                if isdir or _is_gcode(name):
                    out.append((name, isdir))
    except OSError:
        pass
    out.sort(key=lambda t: (not t[1], t[0].lower()))
    return out


class LocalUI:
    def __init__(self, streamer, cfg, gdir, display, inp, jog_feed=800,
                 config_file=None):
        self.s = streamer
        self.cfg = cfg
        self.gdir = gdir.rstrip("/")
        self.d = display
        self.inp = inp
        self.jog_feed = jog_feed
        self.config_file = config_file

        self.mode = "jog"
        self.axis_i = 0
        self.step_i = 1
        self.cwd = ""
        self.entries = []
        self.sel = 0
        self.top = 0
        self.menu_sel = 0
        self.confirm = None   # rel path awaiting run confirmation
        self.loaded = None
        self.total = 0        # lines in the running job

        self._sig = None
        self._dirty = True
        self._poll_at = 0
        self.s.gc_collect = True

        # caPy jog mode for the wheel; GRBL keeps one $J= per detent
        self.jog = JogSession(streamer)
        self._capy = False
        self._dial_target = None   # where the wheel has sent each axis (machine mm)
        self._dial_at = 0
        self._after_jog = None     # an action waiting for jog mode to end
        self._after_seq = 0
        self._after_at = 0

    # ---- lifecycle ----
    def begin(self):
        self._capy = "capy" in self.s.connect().lower()
        self.s.on_message = self._on_message
        self.s.write_line(REPORT_MM)
        self.s.write_line("$I")    # caPy answers [VER:...:caPy]
        self._refresh_files()
        self._dirty = True

    def tick(self):
        """Call repeatedly from the main loop. Non-blocking."""
        if self.s.active:
            self.s.pump()
        elif self.mode == "run":
            # done (motion drained), errored, or stopped — the stop button lands
            # between pumps, so the end is handled here rather than after pump()
            self._end_run()
        else:
            self.s.service()
            self.jog.tick()
            self._jog_service()
            if diff_ms(now_ms(), self._poll_at) >= 0:
                self.s.request_status()
                ms = int((self.cfg.rate or 0.5) * 1000)
                if self.jog.active:
                    ms = min(ms, JOG_POLL_MS)
                self._poll_at = now_ms() + ms
        for ev in self.inp.poll():
            self._on_event(ev)
        sig = self._status_sig()
        if sig != self._sig:
            self._sig = sig
            self._dirty = True
        if self._dirty:
            self._render()
            self._dirty = False

    # ---- event handling ----
    def _on_event(self, ev):
        m = getattr(self, "_ev_" + self.mode, None)
        if m:
            m(ev)
        self._dirty = True

    def _ev_jog(self, ev):
        if ev in ("x", "y", "z"):
            self.axis_i = "xyz".index(ev)
        elif ev in ("x_hold", "y_hold", "z_hold"):
            line = "G10 L20 P0 %s0" % ev[0].upper()
            self._machine(lambda: self.s.write_line(line))
        elif ev in ("cw", "ccw"):
            self._dial(1 if ev == "cw" else -1)
        elif ev == "click":
            self.step_i = (self.step_i + 1) % len(STEPS)
        elif ev == "menu":
            self.menu_sel = 0
            self.mode = "menu"

    def _ev_menu(self, ev):
        if ev == "cw":
            self.menu_sel = min(self.menu_sel + 1, len(MENU) - 1)
        elif ev == "ccw":
            self.menu_sel = max(self.menu_sel - 1, 0)
        elif ev == "click":
            self._menu_act(MENU[self.menu_sel])
        elif ev == "menu":
            self.mode = "jog"

    def _ev_files(self, ev):
        n = len(self.entries)
        if ev == "cw":
            self.sel = min(self.sel + 1, max(n - 1, 0))
        elif ev == "ccw":
            self.sel = max(self.sel - 1, 0)
        elif ev == "click":
            if not self.entries:
                return
            name, isdir = self.entries[self.sel]
            if name == "..":
                self.cwd = self.cwd.rsplit("/", 1)[0] if "/" in self.cwd else ""
                self._refresh_files()
            elif isdir:
                self.cwd = (self.cwd + "/" + name) if self.cwd else name
                self._refresh_files()
            else:
                self.confirm = (self.cwd + "/" + name) if self.cwd else name
                self.mode = "confirm"
        elif ev == "menu":
            self.mode = "menu"

    def _ev_confirm(self, ev):
        if ev == "click":
            self._do_run(self.confirm)
        elif ev == "menu":
            self.mode = "files"

    def _ev_run(self, ev):
        if ev == "cw":
            self.s.feed_override_plus10()
        elif ev == "ccw":
            self.s.feed_override_minus10()
        elif ev == "click":
            self.s.feed_override_reset()
        elif ev == "x":
            (self.s.cycle_start if self._held() else self.s.feed_hold)()
        elif ev == "z":
            self.s.request_stop()

    # ---- helpers ----
    def _held(self):
        """The machine is in (or entering) a feed hold or door stop, read from
        its reported state so the X button always matches what it will do."""
        return self.s.status.state[:4] in ("Hold", "Door")

    def _menu_act(self, key):
        if key == "files":
            self._refresh_files()
            self.mode = "files"
        elif key == "home":
            self._machine(lambda: self.s.write_line("$H"))
            self.mode = "jog"
        elif key == "unlock":
            self._machine(lambda: self.s.write_line("$X"))
            self.mode = "jog"
        elif key == "units":
            self.cfg.units = "inch" if self.cfg.units == "mm" else "mm"
            if self.config_file:
                try:
                    self.cfg.save(self.config_file)
                except OSError:
                    pass
        elif key == "reset":
            self.jog.lost()          # a reset ends jog mode on the controller
            self._after_jog = None
            self.s.request_reset()
            self.mode = "jog"

    def _refresh_files(self):
        path = self.gdir + ("/" + self.cwd if self.cwd else "")
        self.entries = ([("..", True)] if self.cwd else []) + _list(path)
        self.sel = 0
        self.top = 0

    def _do_run(self, rel):
        if self.jog.active:
            self._machine(lambda: self._do_run(rel))
            return
        path = self.gdir + "/" + rel
        try:
            self.total = validate_gcode(path)
        except (OSError, ValueError):
            self.mode = "files"
            return
        self.loaded = rel
        self.s.begin(path, status_interval=self.cfg.rate)
        self.mode = "run"

    # ---- the wheel (jog) ----
    def _on_message(self, m):
        if m.startswith("[VER:") and "capy" in m.lower():
            self._capy = True

    def _dial(self, sign):
        """One detent. On caPy: a step in the jog session, unless the axis is
        already DIAL_LEAD_MM (or two detents) behind the wheel -- then the
        detent is dropped, so a hard spin stops when the hand stops. The
        position comes from status reports, so the allowance also covers what
        the axis travels while a report is on its way (DIAL_LAG_S)."""
        d = sign * STEPS[self.step_i]
        ax = self.axis_i
        if not self._capy:
            self.s.write_line("$J=G91 G21 %s%g F%d" % (AXES[ax], d, self.jog_feed))
            return
        # the lead check reads the position: poll at the jog rate from now on
        # (the idle schedule may be a second away)
        if diff_ms(self._poll_at, now_ms()) > JOG_POLL_MS:
            self._poll_at = now_ms()
        mpos = self.s.status.mpos
        if not self.jog.active or self._dial_target is None:
            self._dial_target = [mpos[0], mpos[1], mpos[2]]
        lead = max(DIAL_LEAD_MM, 2 * abs(d)) + self.jog_feed / 60.0 * DIAL_LAG_S
        if abs(self._dial_target[ax] + d - mpos[ax]) > lead:
            return
        self._dial_target[ax] += d
        self._dial_at = now_ms()
        self.jog.begin()
        self.jog.step(AXES[ax], d, feed=self.jog_feed)

    def _on_target(self):
        t, m = self._dial_target, self.s.status.mpos
        return t is None or all(abs(t[i] - m[i]) < 0.002 for i in range(3))

    def _machine(self, action):
        """Run `action` (a command that needs the machine) now, or -- in jog
        mode, where caPy refuses g-code -- after leaving it and reaching Idle."""
        if not self.jog.active:
            action()
            return
        self.jog.end()
        self._dial_target = None
        self._after_jog = action
        self._after_seq = self.s._status_seq
        self._after_at = now_ms()

    def _jog_service(self):
        if self._after_jog and not self.jog.active:
            fresh = self.s._status_seq > self._after_seq
            if ((fresh and self.s.status.state.startswith("Idle"))
                    or diff_ms(now_ms(), self._after_at) > AFTER_JOG_MS):
                action, self._after_jog = self._after_jog, None
                action()
        elif (self.jog.active and diff_ms(now_ms(), self._dial_at) > JOG_IDLE_MS
              and self._on_target()):
            self.jog.end()
            self._dial_target = None

    def _end_run(self):
        if self.s.state != "done" and self.s.sent_any:
            self.s.request_cancel()
        self.mode = "jog"

    def _status_sig(self):
        st = self.s.status
        wp = st.wpos
        return (self.mode, self.s.state, self.axis_i, self.step_i, self.sel,
                len(self.entries), self.cwd, self.menu_sel, self.confirm,
                self.loaded, round(wp[0], 3), round(wp[1], 3),
                round(wp[2], 3), int(st.feed), int(st.spindle), st.feed_ov,
                st.state, self.s.sent, round(self.s.progress, 2))

    # ---- rendering (128x64 mono OLED; 16 cols x 8 rows of 8px text) ----
    def _render(self):
        self.d.fill(BG)
        getattr(self, "_screen_" + self.mode)()
        self.d.show()

    def _line(self, y, left, right="", scale=1, inv=False):
        """One text row; optional right-justified field; optional inverse bar."""
        d = self.d
        cw = 8 * scale
        if inv:
            d.rect(0, y, d.width, 8 * scale, FG)
        fg = BG if inv else FG
        d.text(0, y, left, fg, scale)
        if right:
            d.text(d.width - len(right) * cw, y, right, fg, scale)

    def _screen_jog(self):
        k = self.cfg.scale   # reports are mm; convert to the display units
        self._line(0, (self.s.status.state or "-")[:8],
                   "F%d" % int(self.s.status.feed * k))
        wp = self.s.status.wpos
        for i, ax in enumerate(AXES):
            self._line(8 + i * 16, "%s%7.3f" % (ax, wp[i] * k), scale=2,
                       inv=(i == self.axis_i))
        self._line(56, "STEP %gmm" % STEPS[self.step_i], "MENU")

    def _screen_menu(self):
        self._line(0, "MENU", inv=True)
        labels = ("Files", "Home  $H", "Unlock $X", "Units " + self.cfg.units, "Reset")
        for i, lab in enumerate(labels):
            self._line(8 + i * 8, lab, inv=(i == self.menu_sel))

    def _screen_files(self):
        self._line(0, ("/" + self.cwd)[:16], inv=True)
        rows = 6
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        if not self.entries:
            self._line(24, "  (empty)")
        for i in range(self.top, min(self.top + rows, len(self.entries))):
            name, isdir = self.entries[i]
            label = (name + "/") if isdir else name
            self._line(8 + (i - self.top) * 8, label[:16], inv=(i == self.sel))

    def _screen_confirm(self):
        name = (self.confirm or "").rsplit("/", 1)[-1]
        self._line(0, "RUN FILE?", inv=True)
        self._line(18, name[:16])
        self._line(38, "click = START")
        self._line(50, "MENU  = cancel")

    def _barpct(self, x, y, w, h, frac, label):
        """Filled bar with a label centered inside it, drawn inverse over the
        filled part so it stays readable across the fill edge (1-bit safe)."""
        d = self.d
        d.rect(x, y, w, h, FG)
        d.rect(x + 1, y + 1, w - 2, h - 2, BG)
        fillw = int((w - 2) * min(max(frac, 0.0), 1.0))
        d.rect(x + 1, y + 1, fillw, h - 2, FG)
        edge = x + 1 + fillw
        tx = x + (w - len(label) * 8) // 2
        ty = y + (h - 8) // 2
        for i, ch in enumerate(label):           # per-char color by fill edge
            cx = tx + i * 8
            d.text(cx, ty, ch, BG if cx + 4 < edge else FG)

    def _screen_run(self):
        st = self.s.status
        # header (not inverted): state + feed-override % (encoder feedback).
        # Past end of file, flag it — the machine is draining buffered motion.
        head = ("EOF " + (st.state or "")) if self.s.state == FINISHING \
            else (st.state or "Run")
        self._line(0, head[:8], "%d%%" % st.feed_ov)
        self._line(10, (self.loaded or "")[:16])
        # progress bar with the % embedded
        self._barpct(2, 22, 124, 12, self.s.progress,
                     "%d%%" % int(self.s.progress * 100))
        # live feed & spindle rates
        self._line(40, "F%d" % int(st.feed * self.cfg.scale), "S%d" % int(st.spindle))
        # bottom bar: button hints
        self._line(56, "X:resume Z:stop" if self._held() else "X:hold  Z:stop", inv=True)
