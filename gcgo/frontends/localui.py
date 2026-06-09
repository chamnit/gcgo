"""Portable local-UI front-end: a small color TFT + rotary-encoder pendant.

Drives the same Streamer core as the terminal/web front-ends through the
non-blocking pump(), rendering to a Display adapter and consuming high-level
events from an Input adapter (see gcgo/ports/base.py). It has no platform
imports beyond os (file listing), so it runs on MicroPython on the board and
on CPython for the PNG preview (tools/preview_local.py).

Interaction (rotary encoder + 3 buttons; a touchscreen can emit the same
events later):

  JOG screen    rotate = jog selected axis by step;  click = next axis
                A = next step size;  B = zero axis;   C = file browser
  FILES screen  rotate = scroll;  click = open dir / run file
                B = up a directory;  C = back to jog
  RUN screen    A = hold/resume;  B = stop;  rotate = feed override +/-10%
"""

import os

from gcgo.core.clock import now_ms, diff_ms
from gcgo.core.gcode import validate_gcode
from gcgo.core.protocol import RUNNING

# palette (r, g, b)
BG = (15, 18, 22)
PANEL = (29, 34, 43)
LINE = (42, 49, 60)
TXT = (227, 232, 240)
MUT = (136, 147, 163)
ACCENT = (136, 192, 208)
GRN = (108, 160, 90)
RED = (160, 70, 78)
YEL = (235, 203, 139)

STATE_COLOR = {
    "Idle": (44, 66, 86), "Run": (51, 64, 31), "Hold": (74, 58, 24),
    "Alarm": (74, 34, 38), "Jog": (44, 66, 86), "Home": (44, 66, 86),
}

AXES = ("X", "Y", "Z")
STEPS = (0.1, 1.0, 10.0, 100.0)
_GCODE_EXT = (".gcode", ".nc", ".g", ".gc", ".ngc")


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
    def __init__(self, streamer, cfg, gdir, display, inp, jog_feed=800):
        self.s = streamer
        self.cfg = cfg
        self.gdir = gdir.rstrip("/")
        self.d = display
        self.inp = inp
        self.jog_feed = jog_feed

        self.mode = "jog"
        self.axis_i = 0
        self.step_i = 1
        self.cwd = ""
        self.entries = []
        self.sel = 0
        self.top = 0          # scroll offset in file list
        self.held = False
        self.loaded = None

        self._sig = None
        self._dirty = True
        self._poll_at = 0
        self.s.gc_collect = True

    # ---- lifecycle ----
    def begin(self):
        self.s.connect()
        self.s.write_line("$13=" + self.cfg.grbl_inch)
        self._refresh_files()
        self._dirty = True

    def tick(self):
        """Call repeatedly from the main loop. Non-blocking."""
        if self.s.state == RUNNING:
            if self.s.pump() != RUNNING:
                self._end_run()
        else:
            self.s.service()
            if diff_ms(now_ms(), self._poll_at) >= 0:
                self.s.request_status()
                self._poll_at = now_ms() + int((self.cfg.rate or 0.5) * 1000)
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
        if ev in ("cw", "ccw"):
            sign = "" if ev == "cw" else "-"
            self.s.write_line("$J=G91 G21 %s%s%g F%d" % (
                AXES[self.axis_i], sign, STEPS[self.step_i], self.jog_feed))
        elif ev == "click":
            self.axis_i = (self.axis_i + 1) % 3
        elif ev == "a":
            self.step_i = (self.step_i + 1) % len(STEPS)
        elif ev == "b":
            self.s.write_line("G10 L20 P0 %s0" % AXES[self.axis_i])
        elif ev == "c":
            self._refresh_files()
            self.mode = "files"

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
            if isdir:
                self.cwd = (self.cwd + "/" + name) if self.cwd else name
                self._refresh_files()
            else:
                self._start_run(name)
        elif ev == "b":
            if self.cwd:
                self.cwd = self.cwd.rsplit("/", 1)[0] if "/" in self.cwd else ""
                self._refresh_files()
        elif ev == "c":
            self.mode = "jog"

    def _ev_run(self, ev):
        if ev == "a":
            if self.held:
                self.s.cycle_start()
            else:
                self.s.feed_hold()
            self.held = not self.held
        elif ev == "b":
            self.s.request_stop()
        elif ev == "cw":
            self.s.feed_override_plus10()
        elif ev == "ccw":
            self.s.feed_override_minus10()

    # ---- helpers ----
    def _refresh_files(self):
        path = self.gdir + ("/" + self.cwd if self.cwd else "")
        self.entries = _list(path)
        self.sel = 0
        self.top = 0

    def _start_run(self, name):
        rel = (self.cwd + "/" + name) if self.cwd else name
        path = self.gdir + "/" + rel
        try:
            validate_gcode(path)
        except (OSError, ValueError):
            return
        self.loaded = rel
        self.held = False
        self.s.begin(path, status_interval=self.cfg.rate)
        self.mode = "run"

    def _end_run(self):
        if self.s.state != "done" and self.s.sent_any:
            self.s.request_cancel()
        self.mode = "jog"

    def _status_sig(self):
        st = self.s.status
        wp = st.wpos
        return (self.mode, self.s.state, self.axis_i, self.step_i, self.sel,
                len(self.entries), self.cwd, self.loaded, self.held,
                round(wp[0], 3), round(wp[1], 3), round(wp[2], 3),
                int(st.feed), st.feed_ov,
                self.s.sent, round(self.s.progress, 2))

    # ---- rendering ----
    def _render(self):
        d = self.d
        d.fill(BG)
        self._header()
        if self.mode == "jog":
            self._screen_jog()
        elif self.mode == "files":
            self._screen_files()
        else:
            self._screen_run()
        self._footer()
        d.show()

    def _header(self):
        d = self.d
        st = self.s.status.state or "—"
        d.rect(0, 0, d.width, 26, PANEL)
        d.rect(6, 5, 70, 17, STATE_COLOR.get(st, LINE))
        d.text(12, 9, st[:8], TXT, 1)
        d.text(d.width - 96, 9, "%s F%d" % (self.cfg.pos_unit, int(self.s.status.feed)), MUT, 1)

    def _dro(self, y):
        d = self.d
        wp = self.s.status.wpos
        for i, ax in enumerate(AXES):
            yy = y + i * 28
            hot = (self.mode == "jog" and i == self.axis_i)
            d.text(10, yy + 4, ax, ACCENT if hot else MUT, 2)
            d.text(40, yy, "%9.3f" % wp[i], TXT, 2)
            if hot:
                d.rect(4, yy, 3, 22, ACCENT)
        return y + 3 * 28

    def _screen_jog(self):
        d = self.d
        y = self._dro(34)
        d.rect(0, y + 4, d.width, 1, LINE)
        d.text(10, y + 12, "STEP", MUT, 1)
        d.text(60, y + 10, "%g mm" % STEPS[self.step_i], YEL, 2)
        d.text(10, y + 36, "FEED", MUT, 1)
        d.text(60, y + 34, "%d mm/min" % self.jog_feed, TXT, 2)

    def _screen_files(self):
        d = self.d
        d.text(10, 32, "/" + self.cwd, ACCENT, 1)
        rows = 6
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        y = 46
        if not self.entries:
            d.text(14, y, "(empty)", MUT, 1)
        for i in range(self.top, min(self.top + rows, len(self.entries))):
            name, isdir = self.entries[i]
            yy = y + (i - self.top) * 24
            if i == self.sel:
                d.rect(6, yy - 3, d.width - 12, 22, PANEL)
            d.text(14, yy, ("[" + name + "]") if isdir else name,
                   ACCENT if isdir else TXT, 1)

    def _screen_run(self):
        d = self.d
        self._dro(34)
        y = 122
        pct = int(self.s.progress * 100)
        d.rect(10, y, d.width - 20, 16, PANEL)
        d.rect(10, y, int((d.width - 20) * self.s.progress), 16, GRN)
        d.text(10, y + 24, "%s  %d sent  %d%%" % (self.loaded or "", self.s.sent, pct), TXT, 1)
        if self.held:
            d.text(10, y + 44, "HOLD", YEL, 2)

    def _footer(self):
        d = self.d
        y = d.height - 20
        d.rect(0, y - 2, d.width, 22, PANEL)
        hints = {
            "jog": "A:step  B:zero  C:files",
            "files": "click:open  B:up  C:jog",
            "run": "A:hold  B:stop  rot:feed",
        }
        d.text(8, y + 4, hints.get(self.mode, ""), MUT, 1)
