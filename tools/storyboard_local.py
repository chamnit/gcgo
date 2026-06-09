#!/usr/bin/env python3
"""Storyboard of the local-UI encoder interaction: drive the REAL LocalUI state
machine with a scripted sequence of encoder/button events and tile the snapshots
with captions, so the navigation flow is visible end to end. No board needed.

    python3 tools/storyboard_local.py     # writes /tmp/gcgo_story_*.png
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gcgo.frontends.localui import LocalUI
from gcgo.core.protocol import RUNNING
from tools.preview_local import PILDisplay, Cfg, make_gdir, FONT

CAP = ImageFont.truetype(FONT, 18)
ACCENT = (136, 192, 208)
MUT = (150, 160, 175)
BG = (8, 10, 13)
CELL_ZOOM = 4   # 128x64 -> 512x256 per storyboard cell


class SimStatus:
    state = "Idle"
    feed = 0.0
    spindle = 0.0
    feed_ov = 100
    rapid_ov = 100
    spindle_ov = 100
    pins = ""
    def __init__(self):
        self.wpos = [0.0, 0.0, 0.0]


class SimStreamer:
    """Fake streamer that *interprets* jog/zero so the DRO reflects actions."""
    def __init__(self):
        self.status = SimStatus()
        self.state = "idle"
        self.sent = 0
        self.progress = 0.0
        self.sent_any = False
        self.gc_collect = False
        self._stop = False

    def connect(self): return ""
    def service(self): pass
    def request_status(self): pass

    def write_line(self, line):
        line = line.strip()
        if line.startswith("$J="):                       # jog: find axis token
            for tok in line.split():
                if tok and tok[0] in "XYZ" and tok[1:].replace("-", "").replace(".", "").isdigit():
                    i = "XYZ".index(tok[0])
                    self.status.wpos[i] += float(tok[1:])
        elif line.startswith("G10"):                     # zero active axis
            for tok in line.split():
                if tok and tok[0] in "XYZ":
                    self.status.wpos["XYZ".index(tok[0])] = 0.0

    def begin(self, *a, **k):
        self.state = RUNNING
        self.status.state = "Run"
        self.status.feed = 800
        self.progress = 0.45
        self.sent = 1024
        self.sent_any = True

    def pump(self):
        if self._stop:
            self.state = "stopped"
        return self.state

    def request_stop(self): self._stop = True
    def request_cancel(self):
        self.status.state = "Idle"
    def feed_hold(self): self.status.state = "Hold"
    def cycle_start(self): self.status.state = "Run"
    def feed_override_plus10(self): self.status.feed_ov += 10
    def feed_override_minus10(self): self.status.feed_ov -= 10


class ScriptedInput:
    def __init__(self): self.q = []
    def poll(self):
        e, self.q = self.q, []
        return e


def run_story(steps, out, cols=3):
    inp = ScriptedInput()
    ui = LocalUI(SimStreamer(), Cfg(), make_gdir(), PILDisplay(zoom=CELL_ZOOM), inp)
    ui._refresh_files()
    frames = []
    for ev, cap in steps:
        if ev:
            inp.q = [ev]
        ui.tick()
        frames.append((ui.d.img.copy(), cap))

    cw, ch = frames[0][0].size
    caph, pad = 34, 16
    rows = (len(frames) + cols - 1) // cols
    W = cols * cw + (cols + 1) * pad
    H = rows * (ch + caph) + (rows + 1) * pad
    canvas = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(canvas)
    for i, (img, cap) in enumerate(frames):
        r, c = divmod(i, cols)
        x = pad + c * (cw + pad)
        y = pad + r * (ch + caph + pad)
        # caption (token before ':' in accent, rest muted)
        if ":" in cap:
            head, tail = cap.split(":", 1)
            dr.text((x, y), head + ":", font=CAP, fill=ACCENT)
            hw = dr.textlength(head + ":", font=CAP)
            dr.text((x + hw, y), tail, font=CAP, fill=MUT)
        else:
            dr.text((x, y), cap, font=CAP, fill=MUT)
        canvas.paste(img, (x, y + caph - 6))
        dr.rectangle([x, y + caph - 6, x + cw - 1, y + caph - 6 + ch - 1],
                     outline=(40, 47, 58))
    canvas.save(out)
    print(out)


PART1 = [
    (None,   "Start: live DRO. Encoder jogs active axis (X)"),
    ("cw",   "Rotate CW: jog X +1.00 (1 detent = step)"),
    ("a",    "Button A: step 1 -> 10 mm"),
    ("cw",   "Rotate CW: jog X +10.00 (10 mm steps now)"),
    ("click","Click: next axis  X -> Y"),
    ("b",    "Button B: zero active axis (Y -> 0)"),
]

PART2 = [
    ("c",    "Button C: open file browser"),
    ("cw",   "Rotate: scroll list"),
    ("cw",   "Rotate: scroll list"),
    ("click","Click: run highlighted file"),
    ("ccw",  "Rotate: feed override -10%"),
    ("b",    "Button B: stop stream"),
    (None,   "Back to HOME, ready to jog"),
]

if __name__ == "__main__":
    run_story(PART1, "/tmp/gcgo_story_1.png", cols=3)
    run_story(PART2, "/tmp/gcgo_story_2.png", cols=3)
