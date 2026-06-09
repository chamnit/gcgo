#!/usr/bin/env python3
"""Render the local-UI (SSD1306 OLED) screens to PNGs with mock data — no board.

Implements the Display contract with PIL at the OLED's real 128x64, 1-bit, and
feeds LocalUI a fake streamer so each screen (jog / files / run) can be
screenshotted. Text is drawn on the 8px character grid (16 cols x 8 rows) the
SSD1306's 8x8 font uses, scaled up for viewing.

    python3 tools/preview_local.py     # writes /tmp/gcgo_local_*.png
"""
import os
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gcgo.frontends.localui import LocalUI

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
ON = (180, 222, 255)   # lit pixel (OLED-ish white-blue)
OFF = (0, 0, 0)


class PILDisplay:
    """Display adapter onto a PIL image at 128x64, 1-bit look. Glyphs are placed
    on the 8*scale px grid so the 16-col layout matches the real 8x8 font."""
    def __init__(self, w=128, h=64, zoom=7):
        self.width, self.height, self.zoom = w, h, zoom
        self.img = Image.new("RGB", (w * zoom, h * zoom), OFF)
        self.dr = ImageDraw.Draw(self.img)
        self._fonts = {}

    def _font(self, scale):
        px = 8 * scale * self.zoom
        if px not in self._fonts:
            self._fonts[px] = ImageFont.truetype(FONT, int(px * 0.92))
        return self._fonts[px]

    def _col(self, rgb):
        return OFF if tuple(rgb) == OFF else ON

    def fill(self, rgb):
        self.dr.rectangle([0, 0, self.img.width, self.img.height], fill=self._col(rgb))

    def rect(self, x, y, w, h, rgb):
        z = self.zoom
        self.dr.rectangle([x * z, y * z, (x + w) * z - 1, (y + h) * z - 1], fill=self._col(rgb))

    def text(self, x, y, s, rgb, scale=1):
        z = self.zoom
        f = self._font(scale)
        col = self._col(rgb)
        for j, ch in enumerate(s):          # one glyph per 8*scale cell
            self.dr.text(((x + j * 8 * scale) * z, y * z), ch, fill=col, font=f)

    def show(self):
        pass

    def save(self, path):
        self.img.save(path)


class FakeStatus:
    state = "Idle"
    feed = 0.0
    spindle = 0.0
    feed_ov = 100
    rapid_ov = 100
    spindle_ov = 100
    pins = ""
    wpos = (0.0, 0.0, 0.0)


class FakeStreamer:
    """Just enough of Streamer for LocalUI's rendering paths."""
    def __init__(self):
        self.status = FakeStatus()
        self.state = "idle"
        self.sent = 0
        self.progress = 0.0
        self.sent_any = False
        self.gc_collect = False
    def connect(self): return ""
    def write_line(self, *a): pass
    def service(self): pass
    def request_status(self): pass
    def begin(self, *a, **k): pass
    def pump(self): return self.state
    def request_stop(self): pass
    def request_cancel(self): pass
    def feed_hold(self): pass
    def cycle_start(self): pass
    def feed_override_plus10(self): pass
    def feed_override_minus10(self): pass


class Cfg:
    grbl_inch = "0"
    pos_unit = "mm"
    rate = 0.5


def make_gdir():
    d = tempfile.mkdtemp()
    os.mkdir(d + "/jobs")
    os.mkdir(d + "/tests")
    for n in ("braid.gcode", "sample.gcode", "spoilboard.gcode"):
        open(d + "/" + n, "w").close()
    return d


def render(name, setup):
    disp = PILDisplay()
    ui = LocalUI(FakeStreamer(), Cfg(), make_gdir(), disp, inp=None)
    setup(ui)
    ui._render()
    out = "/tmp/gcgo_local_%s.png" % name
    disp.save(out)
    print(out)


def jog(ui):
    ui.s.status.wpos = (12.5, -4.25, 1.0)
    ui.mode = "jog"; ui.axis_i = 0; ui.step_i = 2


def files(ui):
    ui.mode = "files"; ui._refresh_files(); ui.sel = 2


def run(ui):
    ui.s.status.state = "Run"
    ui.s.status.feed = 800
    ui.s.sent = 1024
    ui.s.progress = 0.45
    ui.mode = "run"; ui.loaded = "braid.gcode"


if __name__ == "__main__":
    render("jog", jog)
    render("files", files)
    render("run", run)
