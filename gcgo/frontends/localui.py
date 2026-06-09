"""Portable local-UI front-end: SSD1306 OLED + rotary encoder + 4 buttons.

A standalone pendant (no WiFi, no host). Drives the same Streamer core as the
terminal/web front-ends through the non-blocking pump(), rendering to a Display
adapter and consuming high-level events from an Input adapter (ports/base.py).
No platform imports beyond os (file listing), so it runs on MicroPython on the
board and on CPython for the PNG preview (tools/preview_local.py).

Controls -- 4 buttons (X, Y, Z, Menu) + rotary encoder (turn + click):

  JOG (home)   X/Y/Z = pick axis;  long-press axis = zero it
               turn = jog active axis by step;  click = cycle step size
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
from gcgo.core.gcode import validate_gcode
from gcgo.core.protocol import RUNNING

# 1-bit OLED palette: off (background) and on (lit pixel). Highlight = an
# inverse bar (fill FG, draw text in BG) since there's no color to work with.
BG = (0, 0, 0)
FG = (255, 255, 255)

AXES = ("X", "Y", "Z")
STEPS = (0.1, 1.0, 10.0, 100.0)
MENU = ("files", "home", "unlock", "units", "reset")
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
        self.held = False
        self.loaded = None
        self.total = 0        # lines in the running job

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
        if ev in ("x", "y", "z"):
            self.axis_i = "xyz".index(ev)
        elif ev in ("x_hold", "y_hold", "z_hold"):
            self.s.write_line("G10 L20 P0 %s0" % ev[0].upper())
        elif ev in ("cw", "ccw"):
            sign = "" if ev == "cw" else "-"
            self.s.write_line("$J=G91 G21 %s%s%g F%d" % (
                AXES[self.axis_i], sign, STEPS[self.step_i], self.jog_feed))
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
            (self.s.cycle_start if self.held else self.s.feed_hold)()
            self.held = not self.held
        elif ev == "z":
            self.s.request_stop()

    # ---- helpers ----
    def _menu_act(self, key):
        if key == "files":
            self._refresh_files()
            self.mode = "files"
        elif key == "home":
            self.s.write_line("$H")
            self.mode = "jog"
        elif key == "unlock":
            self.s.write_line("$X")
            self.mode = "jog"
        elif key == "units":
            self.cfg.units = "inch" if self.cfg.units == "mm" else "mm"
            self.s.write_line("$13=" + self.cfg.grbl_inch)
            if self.config_file:
                try:
                    self.cfg.save(self.config_file)
                except OSError:
                    pass
        elif key == "reset":
            self.s.request_reset()
            self.mode = "jog"

    def _refresh_files(self):
        path = self.gdir + ("/" + self.cwd if self.cwd else "")
        self.entries = ([("..", True)] if self.cwd else []) + _list(path)
        self.sel = 0
        self.top = 0

    def _do_run(self, rel):
        path = self.gdir + "/" + rel
        try:
            self.total = validate_gcode(path)
        except (OSError, ValueError):
            self.mode = "files"
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
                len(self.entries), self.cwd, self.menu_sel, self.confirm,
                self.loaded, self.held, round(wp[0], 3), round(wp[1], 3),
                round(wp[2], 3), int(st.feed), st.feed_ov,
                self.s.sent, round(self.s.progress, 2))

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
        self._line(0, (self.s.status.state or "-")[:8],
                   "F%d" % int(self.s.status.feed))
        wp = self.s.status.wpos
        for i, ax in enumerate(AXES):
            self._line(8 + i * 16, "%s%7.3f" % (ax, wp[i]), scale=2,
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
        d = self.d
        # top bar: state + lines sent/total
        self._line(0, (self.s.status.state or "Run")[:8],
                   "%d/%d" % (self.s.sent, self.total), inv=True)
        self._line(10, (self.loaded or "")[:16])
        # progress bar with the % embedded
        self._barpct(2, 20, 124, 12, self.s.progress,
                     "%d%%" % int(self.s.progress * 100))
        # feed override: value + a bar with a tick at 100% (turn knob to change)
        ov = self.s.status.feed_ov
        self._line(36, "FEED", "%d%%" % ov)
        d.rect(2, 47, 124, 5, FG); d.rect(3, 48, 122, 3, BG)
        d.rect(3, 48, int(122 * min(max(ov, 0), 200) / 200), 3, FG)
        d.rect(2 + 61, 46, 1, 7, FG)             # 100% tick
        # bottom bar: button hints
        self._line(56, "X:resume Z:stop" if self.held else "X:hold  Z:stop", inv=True)
