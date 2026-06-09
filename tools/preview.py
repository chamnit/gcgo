#!/usr/bin/env python3
"""Render the web UI to a PNG with mocked data — no board, no browser needed.

Builds a standalone HTML from the real static/index.html + app.js, replacing
the live WebSocket with a fake that pushes scripted status/file messages, then
screenshots it with headless Chromium. Re-run after any edit to see the result.

    python3 tools/preview.py                 # default mock (running job)
    python3 tools/preview.py --state Idle     # idle, no job
    python3 tools/preview.py -o /tmp/x.png
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "..", "gcgo", "frontends", "web", "static")

# --- mock payloads the page's handle() expects ---
def mock_messages(state, nfiles=5):
    running = state == "Run"
    status = {
        "type": "status",
        "state": state,
        "units": "mm",
        "wpos": [12.500, -4.250, 1.000],
        "mpos": [212.500, 195.750, -3.000],
        "feed": 800 if running else 0,
        "spindle": 12000 if running else 0,
        "ov": [110, 50, 90] if running else [100, 100, 100],
        "pins": "",
        "stream": {
            "state": "running" if running else "idle",
            "progress": 0.45 if running else 0,
            "sent": 1024 if running else 0,
        },
        "loaded": "braid.gcode" if running else None,
    }
    entries = [{"n": "jobs", "d": True}, {"n": "tests", "d": True},
               {"n": "braid.gcode", "d": False}, {"n": "sample.gcode", "d": False},
               {"n": "spoilboard_surface.gcode", "d": False}]
    for i in range(len(entries), nfiles):
        entries.append({"n": "job_%02d.gcode" % i, "d": False})
    files = {"type": "files", "dir": "", "entries": entries[:max(nfiles, 1)]}
    settings = {"type": "settings", "units": "mm", "rate": 0.5, "after": "clear",
                "overrides": {"feed": True, "rapid": True, "spindle": True, "toggles": False}}
    msgs = [settings, files, status]
    if running:
        msgs.append({"type": "msg", "line": "streaming braid.gcode (2361 lines)"})
    return msgs


# scripted console history shown in the preview (tx/rx coloring)
CONSOLE_SEED = [
    ["> $H", "tx"], ["[MSG:Homing complete]", "rx"], ["ok", "rx"],
    ["> G10 L20 P0 X0 Y0 Z0", "tx"], ["ok", "rx"],
    ["> $J=G91 G21 X10 F1000", "tx"], ["ok", "rx"],
]


def build_html(state, nfiles=5):
    with open(os.path.join(STATIC, "index.html")) as f:
        html = f.read()
    with open(os.path.join(STATIC, "app.js")) as f:
        appjs = f.read()

    msgs = json.dumps(mock_messages(state, nfiles))
    shim = """
<script>
// --- preview shim: replace WebSocket with a scripted fake ---
class MockWS {
  constructor() { this.readyState = 1; MockWS.last = this;
    setTimeout(() => { if (this.onopen) this.onopen();
      for (const m of %s) this.onmessage({ data: JSON.stringify(m) }); }, 0); }
  send(_) {}
  close() {}
}
window.WebSocket = MockWS;
</script>
<script>
%s
</script>
<script>
// seed console with scripted tx/rx history for the preview
setTimeout(() => { for (const [l, c] of %s) log(l, c); }, 5);
</script>
""" % (msgs, appjs, json.dumps(CONSOLE_SEED))

    # drop the external script tag; inline our shimmed version instead
    html = html.replace('<script src="/app.js"></script>', shim)
    return html


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="Run", help="GRBL state to mock (Run/Idle/Hold/Alarm)")
    p.add_argument("--files", type=int, default=5, help="number of mock file entries")
    p.add_argument("-o", "--out", default="/tmp/gcgo_preview.png")
    p.add_argument("--width", type=int, default=820)
    p.add_argument("--height", type=int, default=1180)
    args = p.parse_args()

    html = build_html(args.state, args.files)
    html_path = "/tmp/gcgo_preview.html"
    with open(html_path, "w") as f:
        f.write(html)

    cmd = [
        "chromium", "--headless", "--no-sandbox", "--hide-scrollbars",
        "--force-color-profile=srgb",
        "--window-size=%d,%d" % (args.width, args.height),
        "--screenshot=" + args.out,
        "file://" + html_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.exists(args.out):
        sys.stderr.write(r.stderr)
        sys.exit("screenshot failed")
    print(args.out)


if __name__ == "__main__":
    main()
