#!/usr/bin/env python3
"""Precompile gcgo for a MicroPython board (build/mpy/gcgo).

A board that imports .py source compiles it in RAM, and the web server's
modules peak well above what they keep: from source the web pendant needs a
heap of about 190 KB, from .mpy about 115 KB (measured on the unix port,
64-bit). A Pico W with WiFi up has roughly 160-170 KB, so put .mpy on it.

    pip install mpy-cross==<the board's MicroPython version>
    python3 tools/build_mpy.py              # or: --mpy-cross path/to/mpy-cross
    mpremote connect /dev/ttyACM0 cp -r build/mpy/gcgo :

mpy-cross must match the board's .mpy format (MicroPython 1.2x: format 6;
`import sys; sys.implementation._mpy` on the board). __init__.py files are
copied as source (MicroPython needs them as packages), and the web UI's
static files as they are. Remove an old gcgo/ from the board first if it has
.py files: a stale .py next to its .mpy wins the import.
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = ("desktop",)        # CPython-only adapters (pyserial, termios, joystick)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--mpy-cross", default="mpy-cross", help="the mpy-cross to use")
    p.add_argument("--out", default=os.path.join(ROOT, "build", "mpy"))
    a = p.parse_args()

    src = os.path.join(ROOT, "gcgo")
    dst = os.path.join(a.out, "gcgo")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    n = 0
    for d, dirs, files in os.walk(src):
        rel = os.path.relpath(d, src)
        dirs[:] = [x for x in dirs if x not in SKIP and x != "__pycache__"]
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            s = os.path.join(d, f)
            t = os.path.join(dst, rel, f)
            if f == "__init__.py" or not f.endswith(".py"):
                shutil.copy(s, t)                  # packages, static files
                continue
            if rel == "frontends" and f == "terminal.py" or rel == "core" and f == "codes.py":
                continue                           # desktop only
            subprocess.run([a.mpy_cross, "-o", t[:-3] + ".mpy", s], check=True)
            n += 1
    print("%d modules -> %s" % (n, dst))
    print("then: mpremote connect <port> cp -r %s :" % os.path.relpath(dst))


if __name__ == "__main__":
    sys.exit(main())
