"""Linux gamepad reader over the kernel joystick API (/dev/input/js*).

Standard library only: each event is 8 bytes (time ms, value, type, number).
The joydev driver numbers axes and buttons in a fixed order, so most pads
(Xbox via xpad, 8BitDo and others in X-input mode) share one layout; LAYOUT
maps those numbers to the control names core/gamepad.py uses. Pass another
layout for a pad that numbers differently.

The d-pad (hat axes on most pads) is also reported as the buttons dleft /
dright / dup / ddown. Every press since the last read is latched in
"pressed", so a tap shorter than the poll interval is never lost.

Non-blocking: state() drains whatever events have arrived and returns the
current snapshot, or None while no pad is connected (it retries the device
every RETRY_MS, so a pad can be plugged in or replaced while gcgo runs).
"""

import glob
import os
import struct

from gcgo.core.clock import diff_ms, now_ms

_EV = struct.Struct("IhBB")
_BUTTON, _AXIS, _INIT = 0x01, 0x02, 0x80
RETRY_MS = 1000

# joydev numbering for an Xbox-style pad (xpad and most X-input pads)
LAYOUT = {
    "axes": {0: "lx", 1: "ly", 2: "lt", 3: "rx", 4: "ry", 5: "rt", 6: "hatx", 7: "haty"},
    "buttons": {0: "a", 1: "b", 2: "x", 3: "y", 4: "lb", 5: "rb", 6: "back",
                7: "start", 8: "mode", 9: "ls", 10: "rs"},
}


_HAT = {"hatx": ("dleft", "dright"), "haty": ("dup", "ddown")}


def find():
    """The first joystick device, or None."""
    devs = sorted(glob.glob("/dev/input/js*"))
    return devs[0] if devs else None


class LinuxGamepad:
    def __init__(self, path=None, layout=LAYOUT):
        self.path = path                  # None = the first /dev/input/js*
        self.layout = layout
        self._fd = None
        self._retry_at = 0
        self._axes = {}
        self._buttons = set()
        self._pressed = []
        self.name = ""

    def state(self):
        """{"axes": {name: -1..1}, "buttons": (names held),
        "pressed": (names pressed since the last call)} or None."""
        if self._fd is None and not self._open():
            return None
        try:
            while True:
                data = os.read(self._fd, _EV.size * 32)
                if not data:
                    break
                for i in range(0, len(data) - _EV.size + 1, _EV.size):
                    self._event(*_EV.unpack_from(data, i))
        except BlockingIOError:
            pass
        except OSError:                   # unplugged
            self._close()
            return None
        pressed, self._pressed = tuple(self._pressed), []
        return {"axes": dict(self._axes), "buttons": tuple(self._buttons),
                "pressed": pressed}

    def _event(self, _t, value, kind, number):
        init = kind & _INIT               # the state at open, not a press
        kind &= ~_INIT
        if kind == _AXIS:
            name = self.layout["axes"].get(number)
            if name:
                self._axes[name] = max(-1.0, value / 32767.0)
                if name in _HAT:
                    neg, pos = _HAT[name]
                    self._set(neg, value < 0, init)
                    self._set(pos, value > 0, init)
        elif kind == _BUTTON:
            name = self.layout["buttons"].get(number)
            if name:
                self._set(name, value != 0, init)

    def _set(self, name, down, init):
        if down and name not in self._buttons:
            self._buttons.add(name)
            if not init:
                self._pressed.append(name)
        elif not down:
            self._buttons.discard(name)

    def _open(self):
        if diff_ms(now_ms(), self._retry_at) < 0:
            return False
        self._retry_at = now_ms() + RETRY_MS
        path = self.path or find()
        if not path:
            return False
        try:
            self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return False
        self._axes, self._buttons, self._pressed = {}, set(), []
        try:
            import fcntl
            buf = bytearray(128)
            fcntl.ioctl(self._fd, 0x80806A13, buf)   # JSIOCGNAME(128)
            self.name = buf.split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            self.name = path
        return True

    def _close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        self._axes, self._buttons, self._pressed = {}, set(), []
